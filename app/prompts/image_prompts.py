"""媒体生成提示词:确定性拼装(LLM 漏写 image_prompt/video_prompt 时的兜底 + fake 样板)。

语种纪律(以目标模型卡为准):
- Z-Image-Turbo 模型卡语种=英语 → 图像类兜底以英文风格媒介锁开头,
  中文正文(场景/动作/角色外貌)保留原值 —— 兜底是降级档:质量低于
  LLM 按 prompts/shots.py 语种规则自写的英文 image_prompt,但永不阻断;
- doubao-seedance 模型卡语种=中文 → video 兜底全中文(模型原生,无需翻译层)。

分工(对齐"Schema 即契约"的兜底哲学):
- plan 阶段 LLM 按 prompts/shots.py 指南在 schema 里直接写 image_prompt / video_prompt;
- 渲染/成片阶段用 resolve_*() 取"LLM 自写优先,拼装兜底";
- fake provider 同样走 resolve_*(LLM 字段为空 → 拼装),保证双路产物结构一致。
"""
from __future__ import annotations

from typing import Any

# 英文风格媒介锁:图像模型的"美学词库"在英文语料里密度最高
_IMAGE_STYLE_LOCK = (
    "Anime comic style, 2D illustration, clean line art, cel shading, "
    "vivid colors, no text, no watermark"
)


def _style_token(style: str) -> str:
    """风格引用:保留用户原词(中文也行),让模型对齐 project.style。"""
    style = (style or "").strip() or "日系动漫"
    return f"style reference: {style}"


def build_character_image_prompt(style: str, ch: dict[str, Any]) -> str:
    """角色立绘 prompt:姓名 + 外貌 + 气质(中文原文)+ 英文风格媒介锁。

    角色图没有 LLM 撰写环节,英文锁就是它的质量上限来源。
    """
    name = str(ch.get("name", "角色")).strip()
    parts = [f"Character design: {name}"]
    appearance = str(ch.get("appearance", "")).strip()
    if appearance:
        parts.append(f"Appearance: {appearance}")
    personality = str(ch.get("personality", "")).strip()
    if personality:
        parts.append(f"Temperament: {personality}")
    parts.append(f"{_IMAGE_STYLE_LOCK}; {_style_token(style)}")
    parts.append("full body, centered, plain light background")
    parts.append("stable facial features, hairstyle and outfit; no extra characters")
    return "。".join(parts)


def build_shot_image_prompt(
    style: str, shot: dict[str, Any], *, characters: list[dict[str, Any]] | None = None
) -> str:
    """分镜首帧兜底:英文风格媒介锁开头,正文保留中文原文(降级档,非翻译层)。"""
    parts: list[str] = [_IMAGE_STYLE_LOCK]
    scene = str(shot.get("scene", "")).strip()
    if scene:
        parts.append(f"Scene: {scene}")
    camera = str(shot.get("camera", "")).strip()
    if camera:
        parts.append(f"Camera: {camera}")
    action = str(shot.get("action", "")).strip()
    if action:
        parts.append(f"Action: {action}")

    present = _present_characters(shot, characters)
    if present:
        parts.append("Characters in frame: " + "、".join(present))

    parts.append(_style_token(style))
    parts.append("single storyboard frame; no text, no subtitles, no watermark")
    return "。".join(parts)


def resolve_shot_image_prompt(
    style: str, shot: dict[str, Any], *, characters: list[dict[str, Any]] | None = None
) -> str:
    """分镜首帧 prompt:LLM 自写优先(已是按语种规则写好的成品,原样使用),空则拼装兜底。"""
    authored = str(shot.get("image_prompt") or "").strip()
    if authored:
        return authored[:600]
    return build_shot_image_prompt(style, shot, characters=characters)


def build_video_prompt(shot: dict[str, Any]) -> str:
    """图生视频兜底(seedance 中文原生 → 全中文):scene/camera/action 动起来 + 首帧一致锁。"""
    parts: list[str] = []
    scene = str(shot.get("scene", "")).strip()
    if scene:
        parts.append(f"场景:{scene}")
    camera = str(shot.get("camera", "")).strip()
    if camera:
        parts.append(f"运镜:{camera}")
    action = str(shot.get("action", "")).strip()
    if action:
        parts.append(f"动作:{action}")
    parts.append("把这张静态分镜图扩展为连贯的动态镜头")
    parts.append("构图、人物、场景与原图保持一致,动作自然流畅,不得改写人物形象或场景")
    return "。".join(parts)


def resolve_video_prompt(shot: dict[str, Any]) -> str:
    """图生视频 prompt:LLM 自写优先,空则拼装兜底。"""
    authored = str(shot.get("video_prompt") or "").strip()
    if authored:
        return authored[:500]
    return build_video_prompt(shot)


def _present_characters(
    shot: dict[str, Any], characters: list[dict[str, Any]] | None
) -> list[str]:
    ids = [int(x) for x in (shot.get("character_ids") or [])]
    present: list[str] = []
    for i, ch in enumerate(characters or []):
        if i not in ids:
            continue
        line = str(ch.get("name", f"角色{i}")).strip()
        appearance = str(ch.get("appearance", "")).strip()
        if appearance:
            line += f"({appearance})"
        present.append(line)
    return present


# ---------------------------------------------------------------------------
# 审核抖动降级(变化性重试):SiliconFlow 20021 对完整 prompt 判定不稳定,
# 单删任一稳定段就通过(实测),故按"损失最小"的顺序准备三次尝试。
# ---------------------------------------------------------------------------

_FULLBODY_TAIL = (
    "full body, centered, plain light background。"
    "stable facial features, hairstyle and outfit; no extra characters"
)


def build_character_image_prompt_condensed(ch: dict[str, Any]) -> str:
    """精简版:姓名 + 外貌 + 英文风格锁(最稳,信息最少的降级档)。"""
    name = str(ch.get("name", "角色")).strip()
    appearance = str(ch.get("appearance", "")).strip()
    parts = [f"Character design: {name}"]
    if appearance:
        parts.append(f"Appearance: {appearance}")
    parts.append(_IMAGE_STYLE_LOCK)
    return "。".join(parts)


def character_prompt_attempts(style: str, ch: dict[str, Any]) -> list[str]:
    """角色图三次尝试(20021 命中时按序降级):

    1. 完整版(常规);2. 去掉构图尾句(实测单删即过,形象信息无损);
    3. 精简版(姓名+外貌+风格锁)。
    """
    full = build_character_image_prompt(style, ch)
    return [
        full,
        full.replace(f"。{_FULLBODY_TAIL}", ""),
        build_character_image_prompt_condensed(ch),
    ]
