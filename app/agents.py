"""各生产阶段的"生产者"(Agent 的实现载体)。

生产函数签名(节点调用约定):
    (project, settings) → {content键: 值, "summary": str}

双实现:
- TEXT_PROVIDER=fake:确定性模板(测试/演示,零 API Key);
- TEXT_PROVIDER=openai:真实 LLM(提示词在 app/prompts/,与逻辑解耦)。

LLM 提示词写作纪律(哲学来源:逐 agent 单职责 + Schema 即契约):
- 七段骨架:Role(含职权边界)/ Context / CRITICAL(最高优先级规则)/
  风格锁定 / Output Rules / Required Schema(注释即字段语义)/ Quality Bar;
- "为什么"进 prompt(三幕给叙事职责),可自判的数量给区间而非死数字;
- Schema 与代码归一化是同一份契约:LLM 只输出 JSON,解析零歧义;
- 自然语言中文、键名英文;LLM 输出 user_message 作为面向用户的消息。
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)
from app.errors import user_facing
from app.models.project import ComicProject
from app.prompts import (
    CHARACTERS_SYSTEM_PROMPT,
    OUTLINE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    SHOTS_SYSTEM_PROMPT,
)
from app.prompts.critic import build_critic_system
from app.prompts.image_prompts import (
    build_character_image_prompt,
    build_shot_image_prompt,
    build_video_prompt,
    character_prompt_attempts,
    resolve_shot_image_prompt,
    resolve_video_prompt,
)
from app.services.llm import LLMOutputError, get_llm
from app.services.media import (
    STATIC_ROOT,
    MediaError,
    disk_path,
    download_to_static,
    get_image_client,
    get_video_client,
    store_asset,
)
from app.services.tts import get_tts_client, tts_blocked_reason
from app.services.video_merger import (
    build_voiceover,
    merge_videos,
    mux_with_audio,
    probe_duration,
)

# ===========================================================================
# fake 实现(确定性模板)
# ===========================================================================

_TITLE_POOL = ["夜行者的约定", "雾都便利店", "纸上的少年", "赛博花店"]


def _fake_title(topic: str) -> str:
    # 用主题做种子,确定性选标题,保证同主题结果稳定
    seed = sum(ord(c) for c in topic)
    return _TITLE_POOL[seed % len(_TITLE_POOL)]


async def _fake_outline(project: ComicProject, settings: Settings) -> dict[str, Any]:
    topic = project.topic
    title = _fake_title(topic)
    outline = {
        "title": title,
        "logline": f"围绕「{topic}」展开的漫剧:相遇 → 冲突 → 和解。",
        "genre": ["都市", "奇幻"],
        "themes": ["选择", "羁绊"],
        "setting": "现代都市,一间只在午夜出现的便利店",
        "tone": "温暖中带悬疑",
        "style_note": project.style,
        "acts": [
            {"title": "第一幕·偶然", "plot": f"主角在日常中意外卷入与「{topic}」相关的事件。"},
            {"title": "第二幕·抉择", "plot": "真相浮出,主角必须在原则与感情之间做出选择。"},
            {"title": "第三幕·回响", "plot": "风波落定,留下一个开放式结局与彩蛋。"},
        ],
    }
    return {
        "outline": outline,
        "summary": f"大纲完成:三幕式结构,标题暂定《{title}》",
    }


async def _fake_characters(project: ComicProject, settings: Settings) -> dict[str, Any]:
    characters = [
        {
            "name": "林小满",
            "personality": "外冷内热、观察力强",
            "appearance": "黑色齐肩发,琥珀色眼睛,常穿灰色长风衣",
            "quirks": "紧张时会转硬币",
        },
        {
            "name": "阿澈",
            "personality": "开朗冒失、重情义",
            "appearance": "银白寸头,右眉一道疤,oversize 卫衣",
            "quirks": "说话总带口头禅“包在我身上”",
        },
        {
            "name": "神秘店主",
            "personality": "神秘莫测、亦正亦邪",
            "appearance": "兜帽遮脸,只露出一双异色瞳",
            "quirks": "从不出店门,却什么都知道",
        },
    ]
    return {"characters": characters, "summary": "角色设定完成:3 位核心角色"}


async def _fake_shots(project: ComicProject, settings: Settings) -> dict[str, Any]:
    outline = project.get_content("outline") or {}
    title = outline.get("title", "无题")
    characters = project.get_content("characters") or []
    n_char = len(characters)
    shots = []
    plans = [
        ("夜街全景", "缓慢推进", "林小满独自走在路灯下,听到身后传来脚步声"),
        ("中景对话", "正反打", "阿澈追上她,气喘吁吁地说出关键情报"),
        ("特写", "急推", "两人发现那家本不该存在的店铺亮起了灯"),
        ("店门", "低角度仰拍", "神秘店主推门而出,异色瞳直视镜头"),
        ("夜街全景", "环绕一周", "大雨骤降,三人站在雨中,决定同行"),
    ]
    for i, (scene, camera, action) in enumerate(plans, start=1):
        shot = {
            "index": i,
            "scene": scene,
            "camera": camera,
            "action": action,
            "dialogue": f"第 {i} 镜台词(围绕《{title}》主题)",
            "duration": 4.0,
            "character_ids": [0, 1, 2][: min(3, n_char)] or [0],
        }
        # fake 的媒体提示词 = 确定性拼装样板(与 LLM 漏写时的兜底同一函数)
        shot["image_prompt"] = build_shot_image_prompt(
            project.style, shot, characters=characters
        )
        shot["video_prompt"] = build_video_prompt(shot)
        shots.append(shot)
    return {"shots": shots, "summary": f"分镜完成:共 {len(shots)} 个镜头"}


# ===========================================================================
# LLM 实现(context 组装 + 归一化;提示词本体在 app/prompts/)
# ===========================================================================

def _outline_user(project: ComicProject, feedback: str | None = None) -> str:
    lines = [
        f"故事想法:{project.topic}",
        f"视觉风格:{project.style}",
    ]
    if feedback:
        current = project.get_content("outline") or {}
        acts = " / ".join(
            f"{a.get('title', '')}:{a.get('plot', '')}" for a in (current.get("acts") or [])
        )[:600]
        lines.append(
            f"现有大纲(未点名部分尽量保持):\n"
            f"标题:{current.get('title', '')}\n"
            f"logline:{current.get('logline', '')}\n"
            f"世界观:{current.get('setting', '')} | 基调:{current.get('tone', '')}\n"
            f"逐幕:{acts}"
        )
        lines.append(f"用户反馈:{feedback}")
        lines.append("请按上述 schema 输出【定向修改后】的完整大纲。")
    else:
        lines.append("请按上述 schema 输出大纲。")
    return "\n".join(lines)


def _characters_user(project: ComicProject, feedback: str | None = None) -> str:
    outline = project.get_content("outline") or {}
    acts = outline.get("acts", [])
    plot = " / ".join(a.get("plot", "") for a in acts[:4])[:800]
    lines = [
        f"大纲标题:《{outline.get('title', '')}》",
        f"梗概:{outline.get('logline', '')}",
        f"基调:{outline.get('tone', '')}",
        f"逐幕情节:{plot}",
        f"视觉风格:{project.style}",
    ]
    cast = project.get_content("cast") or []
    if cast:
        lines.append(f"必须保留的角色库阵容（名字与外貌不可改动）:{cast}")
    if feedback:
        # 定向修改轮:现有阵容必须原样出现在上下文里,LLM 才能"只动点名处"
        existing = project.get_content("characters") or []
        char_lines = "\n".join(
            f"{i}. {c.get('name', '?')}(性格:{c.get('personality', '')};"
            f"外貌:{c.get('appearance', '')})"
            for i, c in enumerate(existing)
        )
        lines.append(f"现有角色(0 起,未点名角色必须原样保留):\n{char_lines}")
        lines.append(f"用户反馈:{feedback}")
        lines.append("请按上述 schema 输出【定向修改后】的完整角色数组。")
    else:
        lines.append("请按上述 schema 设计出场角色。")
    return "\n".join(lines)


def _shots_user(project: ComicProject, feedback: str | None = None) -> str:
    outline = project.get_content("outline") or {}
    characters = project.get_content("characters") or []
    char_lines = "\n".join(
        f"{i}. {c.get('name', '')}(性格:{c.get('personality', '')};外貌:{c.get('appearance', '')})"
        for i, c in enumerate(characters)
    )
    acts = outline.get("acts", [])
    plot = " / ".join(a.get("plot", "") for a in acts[:4])[:800]
    lines = [
        f"大纲标题:《{outline.get('title', '')}》",
        f"梗概:{outline.get('logline', '')}",
        f"世界观:{outline.get('setting', '')} | 基调:{outline.get('tone', '')}",
        f"逐幕情节:{plot}",
        f"角色索引:\n{char_lines}",
        f"视觉风格:{project.style}",
    ]
    if feedback:
        # 定向修改轮:现有分镜逐字带进上下文,LLM 才能只动点名那一镜
        existing = project.get_content("shots") or []
        shot_lines = "\n".join(
            f"#{s.get('index', i)} scene:{s.get('scene', '')} | camera:{s.get('camera', '')} | "
            f"action:{s.get('action', '')} | dialogue:{s.get('dialogue', '')} | "
            f"character_ids:{s.get('character_ids')} | duration:{s.get('duration')} | "
            f"image_prompt:{s.get('image_prompt', '')} | video_prompt:{s.get('video_prompt', '')}"
            for i, s in enumerate(existing)
        )
        lines.append(f"现有分镜(未点名镜头必须逐字保留):\n{shot_lines}")
        lines.append(f"用户反馈:{feedback}")
        lines.append("请按上述 schema 输出【定向修改后】的完整分镜数组(镜头数与顺序不变)。")
    else:
        lines.append("请按上述 schema 输出分镜脚本。")
    return "\n".join(lines)


def _normalize_outline(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise LLMOutputError(f"outline 输出不是对象: {data!r}")
    acts = data.get("acts")
    if not isinstance(acts, list) or not acts:
        raise LLMOutputError("outline.acts 必须是非空数组")
    acts = [
        {
            "title": str(a.get("title", ""))[:40] if isinstance(a, dict) else "",
            "plot": str(a.get("plot", ""))[:500] if isinstance(a, dict) else "",
        }
        for a in acts
    ]
    return {
        "title": str(data.get("title", "未命名"))[:40],
        "logline": str(data.get("logline", ""))[:200],
        "genre": _norm_str_list(data.get("genre"), limit=4),
        "themes": _norm_str_list(data.get("themes"), limit=4),
        "setting": str(data.get("setting", ""))[:200],
        "tone": str(data.get("tone", ""))[:100],
        "style_note": str(data.get("style_note", ""))[:200],
        "acts": acts,
    }


def _normalize_characters(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise LLMOutputError(f"characters 输出不是对象: {data!r}")
    chars = data.get("characters")
    if not isinstance(chars, list):
        raise LLMOutputError("characters.characters 必须是数组")
    if len(chars) < 2 or len(chars) > 6:
        raise LLMOutputError(
            f"characters 数量应在 2-6 之间(服务剧情需要),实际 {len(chars)}"
        )
    normalized = []
    for c in chars:
        if not isinstance(c, dict):
            raise LLMOutputError(f"角色项不是对象: {c!r}")
        name = str(c.get("name", "")).strip()
        if not name:
            raise LLMOutputError("角色缺少 name")
        normalized.append(
            {
                "name": name[:20],
                "personality": str(c.get("personality", ""))[:200],
                "appearance": str(c.get("appearance", ""))[:300],
                "quirks": str(c.get("quirks", ""))[:100],
            }
        )
    return normalized


def _normalize_shots(data: Any, n_char: int) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise LLMOutputError(f"shots 输出不是对象: {data!r}")
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        raise LLMOutputError("shots.shots 必须是非空数组")
    if len(shots) > 12:
        raise LLMOutputError(f"shots 数量超出上限(12),实际 {len(shots)}")
    normalized = []
    for i, s in enumerate(shots, start=1):
        if not isinstance(s, dict):
            raise LLMOutputError(f"镜头项不是对象: {s!r}")
        ids = s.get("character_ids")
        if isinstance(ids, list):
            ids = [int(x) for x in ids if isinstance(x, int) or str(x).isdigit()]
            ids = [x for x in ids if 0 <= x < n_char]
        if not ids:
            ids = [0] if n_char > 0 else []
        try:
            duration = float(s.get("duration", 4.0))
        except (TypeError, ValueError):
            duration = 4.0
        shot_item: dict[str, Any] = {
            "index": i,
            "scene": str(s.get("scene", ""))[:60],
            "camera": str(s.get("camera", ""))[:40],
            "action": str(s.get("action", ""))[:400],
            "dialogue": str(s.get("dialogue", ""))[:200],
            "duration": max(1.0, min(20.0, duration)),
            "character_ids": ids,
        }
        # 媒体提示词只在 LLM 真写了时才落库(缺键 = 渲染时走确定性拼装兜底)
        image_prompt = str(s.get("image_prompt") or "").strip()[:600]
        if image_prompt:
            shot_item["image_prompt"] = image_prompt
        video_prompt = str(s.get("video_prompt") or "").strip()[:500]
        if video_prompt:
            shot_item["video_prompt"] = video_prompt
        normalized.append(shot_item)
    return normalized


def _norm_str_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v)[:50] for v in value[:limit] if isinstance(v, str) and v.strip()]


def _norm_msg(data: Any) -> str:
    """提取 LLM 输出的 user_message(面向用户叙述,将作为消息流 summary)。"""
    if isinstance(data, dict):
        msg = data.get("user_message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


# ===========================================================================
# 生产入口(fake 与 LLM 双路,输出契约一致:{content键, summary})
# ===========================================================================

async def produce_outline(
    project: ComicProject, settings: Settings, *, feedback: str | None = None
) -> dict[str, Any]:
    if settings.text_provider == "fake":
        return await _fake_outline(project, settings)
    data = await get_llm(settings).chat_json(
        system=OUTLINE_SYSTEM_PROMPT,
        user=_outline_user(project, feedback=feedback),
        max_tokens=2500,
    )
    outline = _normalize_outline(data.get("outline") if isinstance(data, dict) else None)
    user_message = _norm_msg(data)
    return {
        "outline": outline,
        "summary": user_message or f"大纲完成(LLM):《{outline['title']}》,{len(outline['acts'])} 幕",
    }


async def produce_characters(
    project: ComicProject, settings: Settings, *, feedback: str | None = None
) -> dict[str, Any]:
    if settings.text_provider == "fake":
        result = await _fake_characters(project, settings)
        result["characters"] = _with_cast(project, result["characters"])
        return result
    data = await get_llm(settings).chat_json(
        system=CHARACTERS_SYSTEM_PROMPT,
        user=_characters_user(project, feedback=feedback),
        max_tokens=2500,
    )
    characters = _with_cast(project, _normalize_characters(data))
    user_message = _norm_msg(data)
    return {
        "characters": characters,
        "summary": user_message
        or f"角色设定完成(LLM):{', '.join(c['name'] for c in characters)}",
    }


async def produce_shots(
    project: ComicProject, settings: Settings, *, feedback: str | None = None
) -> dict[str, Any]:
    if settings.text_provider == "fake":
        return await _fake_shots(project, settings)

    existing = project.get_content("shots") or []
    named = _named_shot_indices(feedback)
    global_rewrite = bool(feedback) and _is_global_rewrite(feedback)

    if feedback and not named and not global_rewrite:
        # 反馈没点名任何镜头(如级联里的"发色改黑")→ 分镜与本次改动无关,
        # 代码级保留:LLM 不参与,避免"逐字保留"被模型违背导致重写漂移。
        return {
            "shots": existing,
            "summary": f"分镜与本次修改无关,已原样保留(共 {len(existing)} 镜)",
        }

    n_char = len(project.get_content("characters") or [])
    data = await get_llm(settings).chat_json(
        system=SHOTS_SYSTEM_PROMPT,
        user=_shots_user(project, feedback=feedback),
        max_tokens=3000,
    )
    new_shots = _normalize_shots(data, n_char=n_char)

    if feedback and named and existing:
        # 索引合并:未点名镜头沿用旧数据(LLM 结果只采信点名的那几镜)
        by_index = {s.get("index"): s for s in new_shots}
        merged = [
            by_index.get(s.get("index"), s) if s.get("index") in named else s
            for s in existing
        ]
        new_shots = merged

    user_message = _norm_msg(data)
    return {"shots": new_shots, "summary": user_message or f"分镜完成(LLM):共 {len(new_shots)} 个镜头"}


# ===========================================================================
# 媒体生产(双实现:fake 占位 / 真实厂商)
# - render_character_images / render_shot_images:IMAGE_PROVIDER=siliconflow 时
#   逐张调 SiliconFlow 图像接口并落盘 /static;否则返回 fake:// 占位 URL。
#   prompt 取 resolve_*(LLM 自写优先,空则拼装兜底),fake 与真实两路同源。
# - produce_video:VIDEO_PROVIDER=ark 时逐镜调火山方舟 seedance 图生视频,
#   再用 ffmpeg 拼成一部成片;否则返回 fake 预览。
#   注意:视频服务未就绪的"跳过"决策在 orchestration 层(见 nodes._produce),
#   本函数只负责"能跑就跑"。
# ===========================================================================

def _with_cast(project: ComicProject, characters: list[dict]) -> list[dict]:
    cast = project.get_content("cast") or []
    names = {ch["name"] for ch in cast}
    return [*cast, *(ch for ch in characters if ch["name"] not in names)]


def _uploaded_refs(project: ComicProject, kind: str, index: int | None = None) -> list[str]:
    chars = project.get_content("characters") or []
    name = chars[index]["name"] if kind == "character" and index is not None and index < len(chars) else None
    return [r["url"] for r in project.get_content("reference_images") or []
            if r.get("kind") == kind and (
                r.get("target_name") == name if kind == "character" and r.get("target_name")
                else r.get("target_index") == index
            )]


def _require_reference_model(settings: Settings, refs: list[str]) -> None:
    if refs and not settings.image_consistency_model:
        raise MediaError("Reference model missing", user_message=
                         "使用参考图需要在『设置』页配置图像一致性编辑模型")


async def render_character_images(
    project: ComicProject,
    settings: Settings,
    *,
    feedback: str | None = None,
    target_index: int | None = None,
) -> dict[str, Any]:
    """角色立绘渲染。

    - 反馈回炉(review 分诊):target_index 精准点名 → 只给该角色注入反馈并重画,
      其余角色【复用旧图】(同 prompt 同种子重画纯烧钱);
    - 无下标时退化为名字匹配兜底(_targeted_feedback);
    - 无反馈(首渲/机器质检回炉):全部重新生成。
    """
    characters = project.get_content("characters") or []
    real = settings.image_provider == "siliconflow"
    previous = {
        im.get("character_name"): im
        for im in (project.get_content("character_images") or [])
    }
    images = []
    for i, ch in enumerate(characters):
        name = ch.get("name", f"角色{i}")
        if ch.get("reference_url") and not feedback:
            images.append({"character_name": name, "url": ch["reference_url"], "prompt": "角色库形象"})
            continue
        if feedback:
            if target_index is not None:
                fb = feedback.strip() if target_index == i else ""
            else:
                fb = _targeted_feedback(feedback, name)
        else:
            fb = ""

        if real:
            prev = previous.get(name)
            # 反馈只点了别人:复用旧图,不为未点名角色重复烧钱
            if fb == "" and feedback and prev:
                images.append({"character_name": name, "url": prev.get("url"), "prompt": prev.get("prompt")})
                continue
            # 变化性重试:20021 内容审核抖动 → 降级措辞(完整版 → 去构图尾句 → 精简版)
            attempts = character_prompt_attempts(project.style, ch)
            if fb:
                attempts = [f"{a}。用户反馈:{fb}" for a in attempts]
            refs = (_uploaded_refs(project, "character", i) + _uploaded_refs(project, "style"))[:3]
            _require_reference_model(settings, refs)
            if refs:
                prompt = attempts[0]
                remote_url = await get_image_client(settings).generate(prompt, reference_urls=refs)
            else:
                remote_url, prompt = await _generate_with_moderation_variants(settings, attempts)
            url = await download_to_static(remote_url, subdir="images")
            logger.info("角色形象图完成:%s → %s", name, url)
        else:
            url = f"fake://character/{i}/v1.png"
            prompt = build_character_image_prompt(project.style, ch)
        images.append({"character_name": name, "url": url, "prompt": prompt})
    return {"character_images": images, "summary": f"角色形象图渲染完成:{len(images)} 张"}


async def render_shot_images(
    project: ComicProject,
    settings: Settings,
    *,
    feedback: str | None = None,
    target_index: int | None = None,
    shot_indices: set[int] | None = None,
) -> dict[str, Any]:
    """分镜画面渲染。

    - 反馈回炉:点名(第N镜/分镜N)的那一镜注入反馈并重画,其余镜头复用旧图;
    - 无反馈(首渲/机器质检回炉):全部重新生成。
    target_index 仅供角色渲染使用,分镜走"第N镜"文本点名。
    """
    shots = project.get_content("shots") or []
    characters = project.get_content("characters") or []
    real = settings.image_provider == "siliconflow"
    previous = {
        im.get("shot_index"): im
        for im in (project.get_content("shot_images") or [])
    }
    named = _named_shot_indices(feedback)
    global_rewrite = bool(feedback) and _is_global_rewrite(feedback)
    portraits = {
        im.get("character_name"): im.get("url")
        for im in (project.get_content("character_images") or [])
        if str(im.get("url", "")).startswith("/static/")
    }
    images = []
    for shot in shots:
        idx = shot.get("index", 0)
        if shot_indices is not None and idx not in shot_indices:
            if idx in previous:
                images.append(previous[idx])
            continue
        if feedback and idx in named and not global_rewrite:
            # 每镜只拿"属于它的句子"(第2镜的反馈不会串到第5镜)
            fb = _shot_specific_feedback(feedback, idx)
            if not fb and len(named) == 1:
                fb = feedback.strip()  # 唯一点名镜:整段反馈都是它的
        else:
            fb = ""
        if real:
            prev = previous.get(idx)
            # 全局重渲(如身份锚启用后整组重画)不进入复用
            if fb == "" and feedback and prev and not global_rewrite:
                images.append({"shot_index": idx, "url": prev.get("url"), "prompt": prev.get("prompt")})
                continue
            prompt = resolve_shot_image_prompt(project.style, shot, characters=characters)
            if fb:
                # 单镜画面重写轮:同一 shot agent 按构图纪律重写 image_prompt
                # (用户只给自然语言,scene/action 等剧情文本不动);失败则退回旧尾巴。
                rewritten = await _author_rewrite_shot_image(
                    settings, shot, characters, feedback=fb
                )
                prompt = rewritten or f"{prompt}。用户反馈:{fb}"
            # 跨镜身份锚:本镜在场角色的立绘 → 一致性模型(参考图,像素级锁脸)
            uploaded = _uploaded_refs(project, "shot", idx)
            for char_id in shot.get("character_ids") or []:
                uploaded += _uploaded_refs(project, "character", char_id)
            uploaded += _uploaded_refs(project, "style")
            _require_reference_model(settings, uploaded)
            refs = list(dict.fromkeys([*uploaded, *_shot_portrait_refs(shot, characters, portraits)]))[:3]
            if refs and settings.image_consistency_model:
                remote_url = await get_image_client(settings).generate(
                    prompt, reference_urls=refs
                )
                prompt_used = prompt
            else:
                remote_url, prompt_used = await _generate_with_moderation_variants(
                    settings, [prompt]
                )
            url = await download_to_static(remote_url, subdir="images")
            prompt = prompt_used
            logger.info("分镜画面完成:第 %s 镜 → %s (refs=%d)", idx, url, len(refs))
        else:
            version = uuid.uuid4().hex[:8] if shot_indices is not None else "1"
            url = f"fake://shot/{idx}/v{version}.png"
            prompt = resolve_shot_image_prompt(project.style, shot, characters=characters)
        images.append({"shot_index": idx, "url": url, "prompt": prompt})
    return {"shot_images": images, "summary": f"分镜画面渲染完成:{len(images)} 张"}


def _shot_specific_feedback(feedback: str, idx: int) -> str:
    """把多镜反馈按句拆开,只留点名本镜的句子(多镜多细节互不串扰)。"""
    own = [
        s.strip()
        for s in re.split(r"[。；;\n]", feedback)
        if idx in _named_shot_indices(s) and s.strip()
    ]
    return "。".join(own)


def _single_shot_rewrite_user(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    feedback: str,
) -> str:
    """单镜重写轮上下文:该镜原文 + 角色锚 + 用户画面反馈(见 prompts/shots.py 同款纪律)。"""
    present = _characters_snippet(shot, characters)
    return (
        f"待重写镜头(仅此一镜):\n"
        f"scene:{shot.get('scene', '')} | camera:{shot.get('camera', '')} | "
        f"action:{shot.get('action', '')} | dialogue:{shot.get('dialogue', '')}\n"
        f"原 image_prompt:{shot.get('image_prompt', '')}\n"
        f"原 video_prompt:{shot.get('video_prompt', '')}\n"
        f"出场角色:{present or '(无)'}\n"
        f"用户画面反馈:{feedback}\n"
        "请按 schema 只输出这一镜;scene/camera/action/dialogue/character_ids/"
        "duration/video_prompt 与原文完全一致,只按反馈+构图纪律重写 image_prompt。"
    )


async def _author_rewrite_shot_image(
    settings: Settings,
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    *,
    feedback: str,
) -> str | None:
    """单镜画面重写:同一 shot agent(StoryboardArtist)重写 image_prompt。

    失败(配置/解析)返回 None,由调用方降级为"旧 prompt + 反馈尾巴"。
    """
    if settings.text_provider != "openai":
        return None
    try:
        data = await get_llm(settings).chat_json(
            system=SHOTS_SYSTEM_PROMPT,
            user=_single_shot_rewrite_user(shot, characters, feedback),
            temperature=0.4,
            max_tokens=1200,
        )
        items = data.get("shots") if isinstance(data, dict) else None
        if not isinstance(items, list) or not isinstance(items[0], dict):
            return None
        new_prompt = str(items[0].get("image_prompt") or "").strip()
        return new_prompt[:600] if new_prompt else None
    except LLMOutputError:
        logger.warning("单镜画面重写轮失败,降级旧 prompt+反馈尾巴", exc_info=True)
        return None


async def _generate_with_moderation_variants(
    settings: Settings, attempts: list[str]
) -> tuple[str, str]:
    """按序尝试措辞变体,返回 (图片 URL, 最终生效的 prompt)。

    SiliconFlow 20021 内容审核对完整 prompt 的判定不稳定(同语义变体就放行);
    命中时降级到下一变体;全部被拦才抛 MediaError(用户语明确,不黑箱重试)。
    """
    last_exc: MediaError | None = None
    for index, attempt in enumerate(attempts):
        try:
            remote = await get_image_client(settings).generate(attempt)
            if index > 0:
                logger.warning("[media] 20021 审核抖动,第 %d 种措辞生效", index + 1)
            return remote, attempt
        except MediaError as exc:
            if "20021" not in str(exc):
                raise
            last_exc = exc
            logger.warning("[media] 20021 审核拦截,尝试下一种措辞(%d/%d)", index + 1, len(attempts))
    assert last_exc is not None
    raise MediaError(
        str(last_exc),
        user_message="图像内容审核未通过,可尝试修改相关设定措辞后重试",
    )


_SHOT_INDEX_RE = re.compile(r"(?:第|分镜)\s*(\d+)\s*(?:镜|格)?")
_GLOBAL_REWRITE_WORDS = ("全部", "所有", "整体", "通篇", "重写", "重排", "重新分镜")


def _named_shot_indices(feedback: str | None) -> set[int]:
    """反馈点名的镜头序号(第4镜/分镜4/4镜)。"""
    if not feedback:
        return set()
    return {int(m) for m in _SHOT_INDEX_RE.findall(feedback)}


def _is_global_rewrite(feedback: str | None) -> bool:
    """用户明确要求整体重写分镜时放行全量 LLM,而不是按无点名保留。"""
    if not feedback:
        return False
    return any(word in feedback for word in _GLOBAL_REWRITE_WORDS)


def _targeted_feedback(feedback: str | None, target: str) -> str:
    """反馈里点名目标才返回反馈文本(整段渲染时的单实体注入);否则空。

    B 机制(review 输出 target_index)生效后,此处只在分诊没给出下标时兜底;
    名字匹配做括号容错:库里常是全角括号(神秘乘客（守时者）),
    用户反馈多半打半角 —— 剥离括号后缀比 base 名再匹配。
    """
    if not feedback or not target:
        return ""
    if target in feedback:
        return feedback.strip()
    for separator in ("（", "("):
        base = target.split(separator, 1)[0].strip()
        if len(base) >= 2 and base in feedback:
            return feedback.strip()
    return ""


def _shot_portrait_refs(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    portraits: dict[str, Any],
) -> list[str]:
    """本镜在场角色的立绘 URL(跨镜身份锚,最多 3 张;无立绘则空)。"""
    ids = [int(x) for x in (shot.get("character_ids") or [])]
    refs: list[str] = []
    for i, ch in enumerate(characters):
        if i in ids:
            url = portraits.get(ch.get("name"))
            if url:
                refs.append(url)
    return refs[:3]


def _characters_snippet(
    shot: dict[str, Any], characters: list[dict[str, Any]]
) -> str:
    """在场角色的身份摘要(文本锚):视频模型没有参考图时用它锁形象。"""
    ids = [int(x) for x in (shot.get("character_ids") or [])]
    lines: list[str] = []
    for i, ch in enumerate(characters):
        if i not in ids:
            continue
        name = str(ch.get("name", f"角色{i}")).strip()
        appearance = str(ch.get("appearance", "")).strip()
        lines.append(f"{name}({appearance})" if appearance else name)
    return "、".join(lines)


async def produce_video(project: ComicProject, settings: Settings) -> dict[str, Any]:
    """成片合成:ark 走逐镜图生视频 + ffmpeg 拼接;其余 provider 返回 fake 预览。"""
    if settings.video_provider != "ark":
        return {
            "video": {
                "url": "fake://video/preview.mp4",
                "voiceover_url": "fake://audio/voiceover.mp3",
                "notes": "拼接 + 配音(fake 预览;真实成片走 ARK 图生视频 + ffmpeg)",
            },
            "summary": "成片合成完成(fake 预览)",
        }

    assets = project.get_content("shot_images") or []
    shots = project.get_content("shots") or []
    characters = project.get_content("characters") or []
    shots_by_index = {s.get("index"): s for s in shots}
    limit = settings.video_max_shots if settings.video_max_shots > 0 else len(assets)
    chosen = assets[:limit]

    client = get_video_client(settings)
    segments: list[Any] = []
    total_duration = 0.0
    for i, asset in enumerate(chosen, start=1):
        asset_url = str(asset.get("url", ""))
        if disk_path(asset_url) is None:
            raise MediaError(
                f"produce_video: 分镜图不是本地文件: {asset_url}",
                user_message="分镜图不是本地生成文件,无法用于视频合成;请先跑一次真实图像渲染",
            )
        shot = shots_by_index.get(asset.get("shot_index")) or {}
        prompt = resolve_video_prompt(shot)
        snippet = _characters_snippet(shot, characters)
        if snippet:
            prompt = f"{prompt}。出场角色:{snippet}"
        remote = await client.generate_video(prompt=prompt, image_static_url=asset_url)
        local = await download_to_static(remote, subdir="videos")
        segment = disk_path(local)
        if segment is None:
            raise MediaError(f"produce_video: 片段下载后本地文件缺失: {local}")
        segments.append(segment)
        total_duration += float(shot.get("duration") or 4.0)
        logger.info("视频片段 %s/%s 完成: %s", i, len(chosen), local)

    folder = STATIC_ROOT / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"final-{uuid.uuid4().hex[:10]}.mp4"
    await merge_videos(segments, output)
    voiceover_url, vo_note = await _attach_voiceover(
        settings, chosen, segments, shots_by_index, output
    )
    notes = (
        f"真实合成:{len(chosen)} 镜(ARK 图生视频 + ffmpeg 拼接){vo_note},"
        f"预计时长 {total_duration:.1f}s"
    )
    return {
        "video": {
            "url": f"/static/videos/{output.name}",
            "voiceover_url": voiceover_url,
            "notes": notes,
        },
        "summary": f"成片合成完成:{len(chosen)} 镜图生视频已拼接成片{vo_note}",
    }


async def _attach_voiceover(
    settings: Settings,
    assets: list[Any],
    segments: list[Path],
    shots_by_index: dict[Any, Any],
    silent_video: Path,
) -> tuple[str | None, str]:
    """成片台词配音(可选通道):逐镜对白 TTS → 按真实段长对齐 → 并入成片。

    语义:失败【不阻塞】成片 —— 任何异常只留 note(配音属锦上添花,
    画面是主产品);未开启/缺配置/无台词镜时静默跳过,不产生 note。
    """
    reason = tts_blocked_reason(settings)
    if reason is not None:
        return None, ""
    try:
        client = get_tts_client(settings)
        audio_segments: list[Path] = []
        offsets_ms: list[int] = []
        offset_ms = 0
        for asset, segment in zip(assets, segments):
            shot = shots_by_index.get(asset.get("shot_index")) or {}
            line = str(shot.get("dialogue") or "").strip()
            if line:
                data = await client.synthesize(line)
                audio_url = store_asset(data, subdir="audio", suffix=".mp3")
                audio_path = disk_path(audio_url)
                if audio_path is None:
                    raise MediaError(f"配音落盘失败:{audio_url}")
                audio_segments.append(audio_path)
                offsets_ms.append(offset_ms)
            # 时间轴按真实段长推进(探测失败退回脚本时长)—— 保证下一句起点正确
            real = await probe_duration(segment)
            offset_ms += int((real if real and real > 0 else float(shot.get("duration") or 4.0)) * 1000)
        if not audio_segments:
            return None, ""
        audio_dir = STATIC_ROOT / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        merged_audio = audio_dir / f"voiceover-{uuid.uuid4().hex[:8]}.m4a"
        # 成片总时长 = 逐段真实时长之和(offset 循环结束时恰好等于总和);
        # 音轨超长部分裁掉,保证视频时长永远不被音频缩短
        await build_voiceover(audio_segments, offsets_ms, merged_audio, total_ms=offset_ms)
        voiced = silent_video.with_name(f"{silent_video.stem}-vo.mp4")
        await mux_with_audio(silent_video, merged_audio, voiced)
        silent_video.unlink(missing_ok=True)  # 静音版被有声版替换
        voiced.rename(silent_video)
        logger.info("台词配音合入完成:%d 句 → %s", len(audio_segments), silent_video.name)
        return f"/static/audio/{merged_audio.name}", f";配音 {len(audio_segments)} 句已合成"
    except (MediaError, OSError) as exc:
        # OSError 一并兜住:落盘磁盘满/权限、Windows 上 unlink/rename 被
        # 杀软或索引服务临时占用等 —— 配音是锦上添花,绝不因此废掉整条成片
        logger.warning("台词配音失败,成片保持静音: %s", exc)
        return None, f"(配音未合入:{user_facing(exc)})"


# ===========================================================================
# Critic / Review(规则版;Day7-8 换 LLM 或保留规则+LLM 双通道)
# ===========================================================================

async def assess_images(
    project: ComicProject, settings: Settings, *, assets_key: str
) -> dict[str, Any]:
    """图像质检(双路):

    - fake provider 或 CRITIQUE_FORCE_FAIL:规则路径(默认高分放行/强制低分演示);
    - openai provider:LLM critic(评分锚点式提示词,见 prompts/critic.py);
      critic 故障/输出不合法时降级为规则路径 —— 质检不得阻塞主流程。
    """
    assets = project.get_content(assets_key) or []
    if settings.critique_force_fail or settings.text_provider == "fake" or not assets:
        return await _rule_assess(project, settings, assets_key=assets_key)

    try:
        items = _critic_items(project, assets_key=assets_key)
        if not items:
            return {"should_regenerate": False, "scores": {assets_key: 8.5, "count": 0}}
        data = await get_llm(settings).chat_json(
            system=build_critic_system(assets_key),
            user=_critic_user(items),
            temperature=0.2,
            max_tokens=1500,
        )
        score = _normalize_critic_score(data)
    except (LLMOutputError, ValueError):
        logger.warning("critic LLM 路失败,降级规则路径(assets=%s)", assets_key)
        return await _rule_assess(project, settings, assets_key=assets_key)

    return {
        "should_regenerate": score["score"] < settings.critique_score_threshold,
        "scores": {
            assets_key: score["score"],
            "count": len(assets),
            "dimensions": score["dimensions"],
            "issues": score["issues"],
        },
    }


async def _rule_assess(
    project: ComicProject, settings: Settings, *, assets_key: str
) -> dict[str, Any]:
    """规则质检:默认高分放行;CRITIQUE_FORCE_FAIL=1 强制低分(演示回炉)。"""
    assets = project.get_content(assets_key) or []
    if settings.critique_force_fail:
        score = 3.0
    else:
        score = 8.5
    return {"should_regenerate": score < settings.critique_score_threshold,
            "scores": {assets_key: score, "count": len(assets)}}


def _critic_items(project: ComicProject, *, assets_key: str) -> list[dict[str, Any]]:
    """给 critic 的评审对象:图片 + 生成它所用的文本描述(代理评估依据)。"""
    assets = project.get_content(assets_key) or []
    characters = project.get_content("characters") or []
    shots = project.get_content("shots") or []
    by_name = {c.get("name"): c for c in characters}
    by_index = {s.get("index"): s for s in shots}
    items: list[dict[str, Any]] = []
    for i, asset in enumerate(assets[:6]):
        item = {"label": f"{assets_key}#{i}"}
        if assets_key == "character_images":
            ch = by_name.get(asset.get("character_name")) or {}
            item["description"] = (
                f"角色:{asset.get('character_name', '')} "
                f"外观:{ch.get('appearance', '')} 性格:{ch.get('personality', '')}"
            )
            item["prompt"] = asset.get("prompt", "")
        else:
            shot = by_index.get(asset.get("shot_index")) or {}
            item["description"] = (
                f"场景:{shot.get('scene', '')} 动作:{shot.get('action', '')} "
                f"台词:{shot.get('dialogue', '')}"
            )
            item["prompt"] = asset.get("prompt", "")
        items.append(item)
    return items


def _critic_user(items: list[dict[str, Any]]) -> str:
    lines = [
        "请评审下列生成图(依据:文本描述 + 生成 prompt,输出整体评分与逐项问题):"
    ]
    for item in items:
        lines.append(f"- [{item['label']}]\n  描述:{item['description']}\n  prompt:{item['prompt']}")
    return "\n".join(lines)


def _normalize_critic_score(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise LLMOutputError(f"critic 输出不是对象: {data!r}")
    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        # 畸形输出判为"无效"而非 0 分:0 分会触发无意义的重做,降级规则更安全
        raise LLMOutputError(f"critic dimensions 缺失或非对象: {str(data)[:120]!r}")
    consistency = _clamp_dim(dims.get("consistency"))
    quality = _clamp_dim(dims.get("quality"))
    composition = _clamp_dim(dims.get("composition"))
    score = round(0.4 * consistency + 0.3 * quality + 0.3 * composition, 1)
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    return {
        "score": score,
        "dimensions": {"consistency": consistency, "quality": quality, "composition": composition},
        "issues": [str(x)[:100] for x in issues],
    }


def _clamp_dim(value: Any) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# 注意顺序:具体词(角色图/分镜图)必须在泛化词(角色/分镜)之前,
# 否则"角色图的脸崩了"会先命中"角色"而分诊到 plan_characters。
_RERUN_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("角色图", "形象图", "立绘", "脸崩", "脸歪"), "render_characters"),
    (("分镜图", "画面"), "render_shots"),
    (("大纲", "故事", "情节", "世界观", "主题", "结局"), "plan_outline"),
    (("角色", "人物", "性格"), "plan_characters"),
    (("分镜", "镜头", "节奏", "台词"), "plan_shots"),
]


def decide_rerun_target(feedback: str, *, allowed: set[str]) -> str:
    """规则分诊(降级路径):关键词命中 → 目标生产阶段;未命中默认 plan_characters。"""
    for keywords, stage in _RERUN_KEYWORDS:
        if any(k in feedback for k in keywords) and stage in allowed:
            return stage
    return "plan_characters" if "plan_characters" in allowed else "plan_outline"


def _review_user(project: ComicProject, feedback: str) -> str:
    outline = project.get_content("outline") or {}
    characters = project.get_content("characters") or []
    # 角色索引表(0 起):review 的 target_index 依据就是这份表
    char_lines = "\n".join(
        f"{i}. {c.get('name', '?')}" for i, c in enumerate(characters[:8])
    ) or "(尚未设计)"
    state_lines = [
        f"大纲:《{outline.get('title', '')}》梗概:{outline.get('logline', '')}",
        f"角色索引表(0 起):\n{char_lines}",
        f"分镜数:{len(project.get_content('shots') or [])}",
        (
            f"角色图:{len(project.get_content('character_images') or [])} 张 | "
            f"分镜图:{len(project.get_content('shot_images') or [])} 张"
        ),
        f"成片:{'已有' if project.get_content('video') else '无'}",
    ]
    return f"用户反馈:{feedback}\n\n项目状态摘要:\n" + "\n".join(state_lines)


async def decide_review_target(
    project: ComicProject,
    feedback: str,
    settings: Settings,
    *,
    allowed: set[str],
) -> tuple[str, int | None]:
    """反馈分诊(双路,输出同一契约:(目标生产阶段, 目标角色下标|None)):

    - fake provider:关键词规则(decide_rerun_target),无下标;
    - openai provider:LLM 分诊(提示词见 prompts/review.py),输出经白名单校验;
      target_index 只在 render_characters 时生效:渲染器按下标精准重画一个角色,
      渲染层不再做名字猜测(名字匹配降级为无下标时的兜底)。
      LLM 故障/白名单外 → 降级规则,保证分诊永不阻塞。
    """
    if settings.text_provider == "fake" or not (feedback or "").strip():
        return decide_rerun_target(feedback, allowed=allowed), None
    try:
        data = await get_llm(settings).chat_json(
            system=REVIEW_SYSTEM_PROMPT,
            user=_review_user(project, feedback),
            temperature=0.2,
            max_tokens=800,
        )
        if not isinstance(data, dict):
            raise LLMOutputError(f"review 输出不是对象: {data!r}")
        stage = data.get("start_stage")
        if not (isinstance(stage, str) and stage in allowed):
            logger.warning("review LLM 输出了白名单外的 start_stage=%r,降级规则分诊", stage)
            raise LLMOutputError("start_stage 不在白名单")
        target_index: int | None = None
        if stage == "render_characters":
            target_index = _normalize_target_index(data.get("target_index"), project)
        return stage, target_index
    except LLMOutputError:
        logger.warning("review LLM 路失败,降级规则分诊")
    return decide_rerun_target(feedback, allowed=allowed), None


def _normalize_target_index(value: Any, project: ComicProject) -> int | None:
    """LLM 给的 target_index 必须落在角色表范围内,否则视为未点名(None)。"""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    n = len(project.get_content("characters") or [])
    if n > 0 and 0 <= index < n:
        return index
    logger.warning("review target_index=%r 越界(角色数 %d),忽略", value, n)
    return None
