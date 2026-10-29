"""图的灵魂:生产节点的统一执行器、审批门(interrupt)、批评环、review 分诊。

设计要点:
- 生产节点共用 _produce 模板:记账(DB)+ 产出(agents 生产者)+ state 增量;
  生产者是纯函数(project, settings) → {content键: 值, summary},Day6 换真实 LLM
  时接口不变,图与节点零改动。
- 审批门是 interrupt() 的世界:auto_mode 直接通过;人工裁决经 resume 值
  归一成 feedback —— 有反馈 → review(分诊),大纲特例直接回 plan_outline。
- 批评失败回自己的渲染段(render_characters/render_shots 各自闭环),经审批门
  再走一轮;带轮数上限,超限强制放行(记录分数,不阻塞流程)。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select

from app import agents
from app.config import get_settings
from app.db.session import async_session_maker
from app.models import agent_run as ar
from app.models.message import Message
from app.models.project import ComicProject
from app.orchestration.state import (
    _APPROVAL_TO_PRODUCED,
    PRODUCTION_STAGE_SEQUENCE,
    RunState,
    next_production_stage,
    workflow_progress_for_stage,
)
from app.services.config_service import compose_blocked_reason

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _lineage_key(stage: str) -> str:
    return f"stage:{stage}"


def _normalize_feedback(value: Any) -> str:
    """interrupt resume 值归一成 feedback 字符串。

    None/空 → ""(视为通过);str → 原文;dict → feedback/action 解析。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        fb = value.get("feedback")
        if isinstance(fb, str) and fb.strip():
            return fb.strip()
        action = value.get("action")
        if action == "approve":
            return ""
        if action == "reject":
            reason = value.get("reason")
            return reason.strip() if isinstance(reason, str) else "reject"
        text = value.get("text")
        return text.strip() if isinstance(text, str) else ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# 生产节点
# ---------------------------------------------------------------------------

# 生产者注册表:阶段 → 纯函数(project, settings) → {content键: 值, "summary": str}
_PRODUCERS: dict[str, Callable[[ComicProject, Any], dict[str, Any]]] = {
    "plan_outline": agents.produce_outline,
    "plan_characters": agents.produce_characters,
    "plan_shots": agents.produce_shots,
    "render_characters": agents.render_character_images,
    "render_shots": agents.render_shot_images,
    "compose": agents.produce_video,
}


def production_node_for(stage: str) -> Callable[[RunState], Any]:
    async def node(state: RunState) -> dict[str, Any]:
        return await _produce(state, stage=stage)

    return node


async def _produce(state: RunState, *, stage: str) -> dict[str, Any]:
    run_id = state["run_id"]
    key = _lineage_key(stage)
    lineage = state.get("artifact_lineage") or []
    force = state.get("force_rerun") or []

    # 跳过已完成阶段(恢复/重入);review 指定的 force_rerun 目标除外
    if key in lineage and stage not in force:
        return {"current_stage": stage, "stage_history": [stage]}

    producer = _PRODUCERS[stage]
    progress = workflow_progress_for_stage(stage)

    async with async_session_maker() as session:
        run = await session.get(ar.AgentRun, run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} not found")
        project = await session.get(ComicProject, run.project_id)
        if project is None:
            raise RuntimeError(f"project {run.project_id} not found")

        # 成片跳过语义:ark 已选但服务未就绪 → 不硬失败、不烧钱,
        # 本次 run 成功结束并打 video_pending,配置完善后前端可『补生成成片』。
        if stage == "compose":
            reason = compose_blocked_reason(get_settings(), project.get_content("shot_images"))
            if reason is not None:
                run.status = "running"
                run.current_stage = stage
                run.progress = progress
                run.video_pending = True
                session.add(run)
                await _push_message_once(
                    session,
                    project_id=project.id,
                    run_id=run_id,
                    agent=stage,
                    role="assistant",
                    content=f"跳过成片合成:{reason}。完善『设置』页后,可点『补生成成片』。",
                    superstep=len(state.get("stage_history") or []),
                )
                await session.commit()
                return {
                    "current_stage": stage,
                    "stage_history": [stage],
                    "artifact_lineage": [key],
                    "video_pending": True,
                }

        # 回炉反馈注入:把最近一条用户反馈带给生产者 —— plan_* 三段(定向修改,
        # 未点名内容原样保留)、render 两段(单实体注入);无反馈/“通过” 时为空。
        feedback = ""
        if stage in (
            "plan_outline", "plan_characters", "plan_shots",
            "render_characters", "render_shots",
        ):
            last_user = (
                await session.execute(
                    select(Message)
                    .where(Message.run_id == run_id, Message.role == "user")
                    .order_by(Message.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            if last_user is not None and last_user.content.strip() not in ("", "通过"):
                feedback = last_user.content.strip()[:400]
            logger.debug("[nodes] %s feedback_len=%d", stage, len(feedback))
        if stage in ("plan_outline", "plan_characters", "plan_shots"):
            payload = await producer(project, get_settings(), feedback=feedback)
        elif stage in ("render_characters", "render_shots"):
            payload = await producer(
                project, get_settings(), feedback=feedback,
                target_index=state.get("review_target_index"),
            )
        else:
            payload = await producer(project, get_settings())
        summary = payload.pop("summary", f"{stage} 完成")

        for content_key, value in payload.items():
            project.set_content(content_key, value)

        run.status = "running"
        run.current_stage = stage
        run.progress = progress
        session.add(run)
        session.add(project)
        await _push_message_once(
            session,
            project_id=project.id,
            run_id=run_id,
            agent=stage,
            role="assistant",
            content=summary,
            superstep=len(state.get("stage_history") or []),
        )
        await session.commit()

    return {
        "current_stage": stage,
        "next_stage": next_production_stage(stage),
        "stage_history": [stage],
        "artifact_lineage": [key],
    }


# ---------------------------------------------------------------------------
# 审批门
# ---------------------------------------------------------------------------

# 门 → (message 文案, 通过后的下一站)
_GATES: dict[str, dict[str, str]] = {
    "outline_approval": {
        "next": "plan_characters",
        "message": "故事大纲已生成。确认后开始角色设计;有修改意见可直接输入。",
    },
    "characters_approval": {
        "next": "plan_shots",
        "message": "角色设定已生成。确认后开始分镜脚本;有修改意见可直接输入。",
    },
    "shots_approval": {
        "next": "render_characters",
        "message": "分镜脚本已生成。确认后进入画面渲染;有修改意见可直接输入。",
    },
    "character_images_approval": {
        "next": "render_shots",  # 机器质检在前:能到这道门的图已 machine-pass
        "message": "角色形象图已通过机器质检。请确认形象设计;有修改意见可直接输入。",
    },
    "shot_images_approval": {
        "next": "compose",
        "message": "分镜画面已通过机器质检。请确认;有修改意见可直接输入。",
    },
    "compose_approval": {
        "next": "__end__",
        "message": "漫剧成片已合成。确认即完成;有修改意见可直接输入。",
    },
}


def approval_node_for(gate: str) -> Callable[[RunState], Any]:
    async def node(state: RunState) -> dict[str, Any]:
        return await _approve(state, gate=gate)

    return node


async def _approve(state: RunState, *, gate: str) -> dict[str, Any]:
    run_id = state["run_id"]
    cfg = _GATES[gate]
    next_stage = cfg["next"]

    async with async_session_maker() as session:
        run = await session.get(ar.AgentRun, run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} not found")
        run.current_stage = gate
        run.progress = workflow_progress_for_stage(gate)
        session.add(run)
        # 幂等写:动态 interrupt 的 resume 会重放本节点,interrupt 前的落库副作用
        # 会执行两遍 → 同 run 同门已写过提示就不再插(观察:每道门消息重复两条)
        existing = (
            await session.execute(
                select(Message).where(
                    Message.run_id == run_id,
                    Message.agent == gate,
                    Message.role == "assistant",
                )
            )
        ).scalars().first()
        if existing is None:
            session.add(
                Message(
                    project_id=run.project_id,
                    run_id=run_id,
                    agent=gate,
                    role="assistant",
                    content=cfg["message"],
                )
            )
        await session.commit()

    auto = bool(state.get("auto_mode"))
    feedback = "" if auto else _normalize_feedback(interrupt({"gate": gate, "message": cfg["message"]}))

    if not feedback:
        route = next_stage
        review_requested = False
    elif gate == "outline_approval":
        # 大纲反馈:直接回写大纲(无需 AI 分诊,机械回退更快);
        # 但必须强制 plan_outline 重跑——否则 lineage 会让它被跳过,只重问一遍门
        route = "plan_outline"
        review_requested = False
        return {
            "current_stage": gate,
            "approval_history": [gate],
            "approval_feedback": feedback,
            "review_requested": False,
            "route_stage": route,
            "force_rerun": [route],
        }
    else:
        route = "review"
        review_requested = True

    return {
        "current_stage": gate,
        "approval_history": [gate],
        "approval_feedback": feedback,
        "review_requested": review_requested,
        "route_stage": route,
    }


# ---------------------------------------------------------------------------
# 批评节点(质量闭环)
# ---------------------------------------------------------------------------

# 批评节点 → (检查的 content 键, 通过后的去向(审批门,人做最终把关),
#              失败回炉的渲染段)
_CRITIQUES: dict[str, dict[str, str]] = {
    "critique_character_images": {
        "assets_key": "character_images",
        "pass_next": "character_images_approval",
        "rerun_stage": "render_characters",
    },
    "critique_shot_images": {
        "assets_key": "shot_images",
        "pass_next": "shot_images_approval",
        "rerun_stage": "render_shots",
    },
}


def critique_node_for(kind: str) -> Callable[[RunState], Any]:
    name = f"critique_{kind}"

    async def node(state: RunState) -> dict[str, Any]:
        return await _critique(state, name=name)

    return node


async def _critique(state: RunState, *, name: str) -> dict[str, Any]:
    cfg = _CRITIQUES[name]
    settings = get_settings()
    assets_key = cfg["assets_key"]
    # 轮次按资产类型独立计数:角色图烧完上限不影响分镜图自己的回炉机会
    base_rounds = dict(state.get("critique_rounds") or {})
    critique_round = int(base_rounds.get(assets_key, 0))

    # 闸 1:轮数已耗尽(>= 上限)→ 不再调用 critic,直接放行
    if not settings.critique_enabled or critique_round >= settings.critique_max_rounds:
        return {
            "current_stage": name,
            "stage_history": [name],
            "critique_scores": state.get("critique_scores", {}),
            "critique_rounds": base_rounds,
            "route_stage": cfg["pass_next"],
        }

    async with async_session_maker() as session:
        run = await session.get(ar.AgentRun, state["run_id"])
        project = await session.get(ComicProject, run.project_id) if run else None
        run_id = state["run_id"]
        if run is not None:
            run.current_stage = name
            run.progress = workflow_progress_for_stage(name)
            session.add(run)
        project_id = project.id if project else None
        if project is None:
            raise RuntimeError(f"run {run_id}: project missing")
        # 必须 commit:不落库则退出 with 即回滚 —— 批评期间前端会把 run 停在
        # 上一道门,误以为还在等人确认(曾静默吞掉批评期的误确认)
        await session.commit()

    assess = await agents.assess_images(project, settings, assets_key=assets_key)
    should_regenerate = bool(assess.get("should_regenerate"))
    round_ = int(critique_round)

    # 闸 2:批评之后 —— 不达标且还有轮次 → 回炉渲染(经其审批门再走一轮)。
    # 回炉必须带 force_rerun:否则渲染段看到自己的 lineage 记录会被跳过,
    # "批评→重渲"循环就名存实亡(与 review 同款缺陷,见 test_critique)。
    if should_regenerate and round_ < settings.critique_max_rounds - 1:
        route = cfg["rerun_stage"]
        new_round = round_ + 1
        force_rerun = [cfg["rerun_stage"]]
    else:
        route = cfg["pass_next"]
        new_round = round_
        force_rerun = []

    rounds = dict(base_rounds)
    rounds[assets_key] = new_round

    if should_regenerate:
        # 轮次号入文案:每轮失败是新信号(必须新增消息);同轮重放(崩溃恢复)
        # 文案+超步代次不变 → 幂等键塌缩,不重复插行
        if force_rerun:
            text = f"质量不达标(第 {round_ + 1} 轮),退回 {cfg['rerun_stage']} 重做"
        else:
            text = f"质量不达标(第 {round_ + 1} 轮),已达轮数上限,强制放行"
        async with async_session_maker() as session:
            await _push_message_once(
                session,
                project_id=project_id,
                run_id=run_id,
                agent=name,
                role="assistant",
                content=text,
                superstep=len(state.get("stage_history") or []),
            )
            await session.commit()
    outcome: dict[str, Any] = {
        "current_stage": name,
        "stage_history": [name],
        "critique_scores": assess.get("scores", {}),
        "critique_rounds": rounds,
        "route_stage": route,
    }
    if force_rerun:
        outcome["force_rerun"] = force_rerun
    return outcome


# ---------------------------------------------------------------------------
# Review(反馈分诊)
# ---------------------------------------------------------------------------

# 各生产阶段负责的 content 键顺序 = 清场顺序
_STAGE_FIELD_ORDER: list[tuple[str, str]] = [
    ("plan_outline", "outline"),
    ("plan_characters", "characters"),
    ("plan_shots", "shots"),
    ("render_characters", "character_images"),
    ("render_shots", "shot_images"),
    ("compose", "video"),
]

_ALLOWED_RERUN: set[str] = {s for s, _ in _STAGE_FIELD_ORDER[:-1]}  # review 可回炉的生产段(不含 compose)


def _rerun_allowlist_for_gate(gate: str | None) -> set[str]:
    """路由护栏:review 只能回退到【当前门及其之前已发生过的】生产段。

    图是线性推进的,角色门反馈时 render_* 还没发生过 —— 路由到未来阶段会
    跳过 plan_shots 等未跑环节(实例:run4 角色门改瞳色被路由到 render_characters,
    直接跳过整个分镜阶段)。allowlist = 生产序列里 下标 ≤ 当前门锚点 的阶段 ∩ 常规白名单。
    """
    base = gate if gate in PRODUCTION_STAGE_SEQUENCE else _APPROVAL_TO_PRODUCED.get(gate or "")
    if base is None or base not in PRODUCTION_STAGE_SEQUENCE:
        return set(_ALLOWED_RERUN)  # 未知门:退回全白名单兜底
    anchor = PRODUCTION_STAGE_SEQUENCE.index(base)
    return {s for s in _ALLOWED_RERUN if PRODUCTION_STAGE_SEQUENCE.index(s) <= anchor}


async def review_node(state: RunState) -> dict[str, Any]:
    """拿着用户反馈做"分诊":决定从哪个生产阶段回炉,并清空下游产物。

    分诊双路(agents.decide_review_target):fake provider 走关键词规则;
    openai provider 走 LLM 分诊 + 白名单校验,失败降级规则 —— 永不阻塞。
    分诊后清理该阶段之后的所有 content 字段,并把目标及下游阶段放进
    force_rerun,确保它们真正重跑(而非被 lineage 跳过)。
    """
    run_id = state["run_id"]
    feedback = state.get("approval_feedback", "") or ""

    async with async_session_maker() as session:
        run = await session.get(ar.AgentRun, run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} not found")
        project = await session.get(ComicProject, run.project_id)
        if project is None:
            raise RuntimeError(f"project {run.project_id} not found")

        target, char_target_index = await agents.decide_review_target(
            project,
            feedback,
            get_settings(),
            allowed=_rerun_allowlist_for_gate(state.get("current_stage")),
        )

        # 清空 target 之后(含)不再需要的内容;target 自己的字段由生产者重写
        # (注意:char_target_index 是分诊点名的【角色下标】,仅供 render_characters
        # 精准重画,与这里的清场下标是两回事,绝不能互相覆盖 —— 曾因此让
        # "点名重画某角色"静默失效,见 test_review_target_index_passthrough)
        fields = [f for s, f in _STAGE_FIELD_ORDER]
        cleanup_index = next(i for i, (s, _) in enumerate(_STAGE_FIELD_ORDER) if s == target)
        for f in fields[cleanup_index + 1 :]:
            project.set_content(f, None if f != "video" else {})

        session.add(project)
        await _push_message_once(
            session,
            project_id=project.id,
            run_id=run_id,
            agent="review",
            role="assistant",
            content=f"收到反馈:{feedback or '(无)'} → 从 {target} 回炉重做",
            superstep=len(state.get("stage_history") or []),
        )
        await session.commit()

    # 强制重跑目标及之后全部生产阶段:review 已清空下游产物,若不强制,
    # 下游阶段会被 artifact_lineage 跳过,导致产物缺失(见 test_orchestration
    # 的终审反馈用例,该用例正是为此缺陷而生)。
    prod_seq = PRODUCTION_STAGE_SEQUENCE
    stage_seq_index = prod_seq.index(target)
    force_rerun = list(prod_seq[stage_seq_index:])

    outcome: dict[str, Any] = {
        "current_stage": "review",
        "route_stage": target,
        "route_mode": "in_run",
        "approval_feedback": "",
        "review_requested": False,
        "force_rerun": force_rerun,
        "approval_history": [f"review:{target}"],
    }
    # 角色下标只在目标为 render_characters 时透传(渲染器按下标精准重画一个角色);
    # 其它阶段没有"角色下标"概念,写 None 让消费端走名字匹配兜底
    outcome["review_target_index"] = char_target_index if target == "render_characters" else None
    return outcome


# ---------------------------------------------------------------------------
# 路由函数(读 state.route_stage,兜底默认)
# ---------------------------------------------------------------------------

def route_from_start(state: RunState) -> str:
    return state.get("current_stage") or "plan_outline"


def route_after_approval(state: RunState) -> str:
    return state.get("route_stage") or "plan_characters"


def route_after_compose(state: RunState) -> str:
    """compose 之后:成片被跳过(video_pending)→ 直达 END;正常合成 → 进人工终审。"""
    return "__end__" if state.get("video_pending") else "compose_approval"


def route_after_critique(state: RunState) -> str:
    return state.get("route_stage") or "compose"


def route_after_review(state: RunState) -> str:
    route = state.get("route_stage")
    return route if route in _ALLOWED_RERUN else "plan_characters"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

async def _push_message_once(
    session: Any,
    *,
    project_id: int,
    run_id: int,
    agent: str,
    role: str,
    content: str,
    superstep: int,
) -> None:
    """幂等落一条消息(调用方负责 commit)。

    动态 interrupt 的 resume / 崩溃恢复会重放节点:消息行若已 commit 而
    checkpoint 未落,重放会重复插行(UI 双行)。幂等键 = (run, agent, role,
    content, superstep),superstep = 当前 state.stage_history 长度:

    - 同一次执行的重放:stage_history 长度与崩溃前相同 → 键相同 → 塌缩 ✓;
    - 新一轮合法执行(review/批评回炉重跑同一节点,文案可能与上轮完全
      相同):其间必经门/批评/review 超步,stage_history 已增长 → 键不同 →
      正常新增(消息流如实呈现"又跑了一轮")✓。
    """
    existing = (
        await session.execute(
            select(Message.id)
            .where(
                Message.run_id == run_id,
                Message.agent == agent,
                Message.role == role,
                Message.content == content,
                Message.superstep == superstep,
            )
            .limit(1)
        )
    ).scalars().first()
    if existing is None:
        session.add(
            Message(
                project_id=project_id,
                run_id=run_id,
                agent=agent,
                role=role,
                content=content,
                superstep=superstep,
            )
        )
