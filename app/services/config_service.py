"""运营配置服务:前端『设置』页的持久层与热更通道。

双通道语义(与 .env 的关系):
- .env 是"部署基线",首次启动时把当前值种进 config_item 表(ensure_initialized);
- 之后以【面板/DB 值为准】:启动时 apply_stored() 会把 DB 值覆盖进 Settings;
  用户改 .env 不再生效(文档化取舍:单机 demo 只有一个配置入口,避免双写混乱)。
- 保存即热更:apply_settings_overrides 原地更新 lru_cache 单例,无需重启。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlmodel import Session

from app.config import Settings, apply_settings_overrides, get_settings
from app.models.config_item import ConfigItem

logger = logging.getLogger(__name__)

# 前端设置页可编辑的字段白名单(Settings 字段名)
PANEL_FIELDS: tuple[str, ...] = (
    "text_provider",
    "text_base_url",
    "text_api_key",
    "text_model",
    "text_thinking",
    "image_provider",
    "image_base_url",
    "image_api_key",
    "image_model",
    "image_size",
    "image_negative_prompt",
    "image_seed",
    "image_consistency_model",
    "video_provider",
    "doubao_base_url",
    "doubao_api_key",
    "doubao_video_model",
    "video_duration",
    "video_ratio",
    "video_max_shots",
    "public_base_url",
    "tts_enabled",
    "tts_model",
    "tts_voice",
    "critique_enabled",
    "critique_score_threshold",
    "critique_max_rounds",
)

_SECRET_MARKERS = ("api_key", "secret", "token")


def is_sensitive(field_name: str) -> bool:
    lower = field_name.lower()
    return any(marker in lower for marker in _SECRET_MARKERS)


def _norm_value(field_name: str, raw: str | None) -> Any:
    """'' 与 None → None(可选字段清空);其余保持字符串交给 model_validate 强转。"""
    value = (raw or "").strip()
    if value == "":
        return None
    if field_name in Settings.model_fields:
        field = Settings.model_fields[field_name]
        if "bool" in str(field.annotation).lower():
            return value.lower() in {"1", "true", "yes", "on"}
    return value


class ConfigService:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def ensure_initialized(self) -> None:
        """首次启动种子:把当前 Settings 值落库(仅当行不存在)。"""
        settings = get_settings()
        for field in PANEL_FIELDS:
            if not hasattr(settings, field):
                continue
            exists = await self.session.get(ConfigItem, field)
            if exists is None:
                value = getattr(settings, field)
                self.session.add(
                    ConfigItem(key=field, value="" if value is None else str(value))
                )
        await self.session.commit()

    async def apply_stored(self) -> None:
        """启动时把 DB 值覆盖进 Settings(面板值优先于 .env)。

        逐键校验、坏键跳过:历史遗留的非法行(早期版本"先落库后校验"可能
        写入)绝不能把启动打死 —— 一条坏配置最多让该项退回 .env/默认值。
        """
        rows = (await self.session.execute(select(ConfigItem))).scalars().all()
        candidates = {
            row.key: _norm_value(row.key, row.value)
            for row in rows
            if row.key in PANEL_FIELDS
        }
        base = get_settings().model_dump()
        accepted: dict[str, Any] = {}
        for key, value in candidates.items():
            merged = {**base, **accepted, key: value}
            try:
                Settings.model_validate(merged)
            except ValidationError:
                logger.warning("config_item %s 值非法,启动时跳过(值=%r)", key, value)
                continue
            accepted[key] = value
        if accepted:
            apply_settings_overrides(accepted)

    async def list_public(self) -> dict[str, dict[str, Any]]:
        """给前端展示:密钥只返回"是否已设置",不泄露明文。

        布尔字段规范成小写 "true"/"false":前端 checkbox 与保存 diff 都用
        小写比较,str(True)="True" 会让开关永远显示为关(实测缺陷)。
        """
        settings = get_settings()
        out: dict[str, dict[str, Any]] = {}
        for field in PANEL_FIELDS:
            if not hasattr(settings, field):
                continue
            current = getattr(settings, field)
            secret = is_sensitive(field)
            if secret:
                # 密钥只回 is_set,明文不进任何响应字段(前端拿不到,只能重填)
                out[field] = {
                    "is_sensitive": True,
                    "is_set": bool(current not in (None, "")),
                    "value": "",
                    "current": "",
                }
            else:
                text = ""
                if current is not None:
                    if "bool" in str(Settings.model_fields[field].annotation).lower():
                        text = "true" if current else "false"
                    else:
                        text = str(current)
                out[field] = {
                    "is_sensitive": False,
                    "value": text,
                    "current": text,
                }
        return out

    async def save(self, values: dict[str, str]) -> dict[str, dict[str, Any]]:
        """保存一组配置:白名单 → 整体校验 → 落库 → 热更 Settings。

        顺序讲究:先在内存里对"当前值 + 本次改动"做整体校验,非法值直接
        拒绝且【不落库】 —— 曾经"先 commit 后校验"会把坏值写进 config_item,
        下次启动 apply_stored 重放即死,服务永久起不来。
        """
        unknown = [k for k in values if k not in PANEL_FIELDS]
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")

        overrides = {k: _norm_value(k, v) for k, v in values.items()}
        merged = {**get_settings().model_dump(), **overrides}
        try:
            Settings.model_validate(merged)
        except ValidationError as exc:
            brief = "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', ('?',)))}: {e.get('msg', '')}"
                for e in exc.errors()[:3]
            )
            raise ValueError(f"配置值不合法,未保存:{brief}") from exc

        for key, raw in values.items():
            row = await self.session.get(ConfigItem, key)
            normalized = "" if _norm_value(key, raw) is None else str(raw).strip()
            if row is None:
                self.session.add(ConfigItem(key=key, value=normalized))
            else:
                row.value = normalized
                self.session.add(row)
        await self.session.commit()

        apply_settings_overrides(overrides)
        return await self.list_public()


def provider_issues(settings: Settings) -> dict[str, str]:
    """全模态配置问题(设置页/面板可用);真实 provider 已选但配置不完整 → 用户语。

    返回 {modality: 用户可读问题}。fake 或完整配置不产生问题。
    """
    issues: dict[str, str] = {}
    if settings.text_provider == "openai" and not (settings.text_base_url and settings.text_model):
        issues["text"] = "文本服务:接口地址或模型未填写"
    if settings.image_provider == "siliconflow" and not (
        settings.image_base_url and settings.image_api_key and settings.image_model and settings.image_size
    ):
        issues["image"] = "图像服务:接口地址/密钥/模型/尺寸未填写完整"
    if settings.video_provider == "ark" and not (settings.doubao_api_key and settings.doubao_video_model):
        issues["video"] = "视频服务:密钥或模型未填写"
    return issues


def blocking_issues(settings: Settings) -> dict[str, str]:
    """generate 入口预检:只有 text/image 阻塞。

    视频未配置不拦入口:全流程照跑(文本+图像),成片阶段自动跳过并打
    video_pending,配置完善后『补生成成片』。
    """
    return {k: v for k, v in provider_issues(settings).items() if k != "video"}


def compose_blocked_reason(
    settings: Settings, shot_assets: list[dict[str, Any]] | None
) -> str | None:
    """真实成片(ark)此刻能不能跑;None=可以。fake 属离线占位,不算阻塞。

    判定顺序:视频凭据 → 分镜画面就绪(有真实本地文件,非 fake:// 占位)。
    compose 阶段跳过、前端"补成片"按钮共用此函数 —— UI 与图内看到同一答案。
    """
    if settings.video_provider != "ark":
        return None
    if not (settings.doubao_api_key and settings.doubao_video_model):
        return "视频服务:密钥或模型未填写"
    assets = shot_assets or []
    if not assets:
        return "还没有分镜画面,无法合成"
    if not all(str(a.get("url", "")).startswith("/static/") for a in assets):
        return "分镜画面仍是占位或非本地文件,真实成片需要先配置真实图像服务渲染"
    return None
