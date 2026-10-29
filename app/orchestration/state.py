"""阶段词表:15 个阶段,四分类 + 三个换算函数。

分类:
- 生产阶段(6):真正产出内容的阶段
- 审批门(6):interrupt 等人裁决
- 批评节点(2):机器质检,低分回炉
- review(1):反馈分诊,决定从哪个阶段重来

换算函数让"任意阶段 → 生产锚点 → 进度/下一站"成为 O(1),是
进度条(WS)、审批消息、恢复定位共用的坐标系。
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Literal, TypedDict

ComicStage = Literal[
    "plan_outline",
    "outline_approval",
    "plan_characters",
    "characters_approval",
    "plan_shots",
    "shots_approval",
    "render_characters",
    "character_images_approval",
    "critique_character_images",
    "render_shots",
    "shot_images_approval",
    "critique_shot_images",
    "compose",
    "compose_approval",
    "review",
]

# 生产阶段的有序序列(进度条的分母)
PRODUCTION_STAGE_SEQUENCE: tuple[str, ...] = (
    "plan_outline",
    "plan_characters",
    "plan_shots",
    "render_characters",
    "render_shots",
    "compose",
)

_APPROVAL_TO_PRODUCED: dict[str, str] = {
    "outline_approval": "plan_outline",
    "characters_approval": "plan_characters",
    "shots_approval": "plan_shots",
    "character_images_approval": "render_characters",
    "shot_images_approval": "render_shots",
    "compose_approval": "compose",
}

_CRITIQUE_TO_PRODUCED: dict[str, str] = {
    "critique_character_images": "render_characters",
    "critique_shot_images": "render_shots",
}


def _resolve_base_stage(stage: str | None) -> str | None:
    """任何阶段(生产/审批/批评)→ 归位到它的生产锚点。"""
    if stage in PRODUCTION_STAGE_SEQUENCE:
        return stage
    base = _APPROVAL_TO_PRODUCED.get(stage or "")
    if base is not None:
        return base
    return _CRITIQUE_TO_PRODUCED.get(stage or "")


def next_production_stage(stage: str | None) -> str | None:
    base = _resolve_base_stage(stage)
    if base is None:
        return None
    idx = PRODUCTION_STAGE_SEQUENCE.index(base) + 1
    if idx >= len(PRODUCTION_STAGE_SEQUENCE):
        return None
    return PRODUCTION_STAGE_SEQUENCE[idx]


def workflow_progress_for_stage(stage: str, *, within_stage: float = 0.0) -> float:
    """进度 = (生产锚点下标 + 阶段内进度) / 生产阶段总数。"""
    base = _resolve_base_stage(stage)
    if base is None:
        return 0.0
    idx = PRODUCTION_STAGE_SEQUENCE.index(base)
    total = len(PRODUCTION_STAGE_SEQUENCE)
    clamped = max(0.0, min(within_stage, 1.0))
    return min((idx + clamped) / total, 1.0)


class RunState(TypedDict, total=False):
    """图的公共账本(TypedDict:运行期是普通 dict)。

    total=False:恢复/定点重绘可从任意阶段切入,键不必齐全。
    reducer 字段(add)自动累加,是"跳过已完成阶段"与审计的基础。
    """

    project_id: int
    run_id: int
    auto_mode: bool
    current_stage: str
    next_stage: str
    stage_history: Annotated[list[str], add]
    approval_history: Annotated[list[str], add]
    artifact_lineage: Annotated[list[str], add]
    approval_feedback: str
    review_requested: bool
    route_stage: str
    # 批评轮次按资产类型独立计数(两段渲染各自闭环):{assets_key: round}
    critique_rounds: dict
    critique_scores: dict
    force_rerun: list[str]  # review 回炉时指定强制重跑的生产阶段(绕过 lineage 跳过)
    video_pending: bool  # compose 被跳过(视频服务未就绪)→ 路由直达 END,不打审批门
    review_target_index: int  # review 分诊点名的角色下标(render_characters 精准重画一个角色)
