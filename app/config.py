"""pydantic-settings 配置。

约定:
- 运行时用 get_settings():lru_cache 单例,读 .env;
- 测试直接 Settings():纯默认值,不读仓库 .env;
- 【厂商相关配置(地址/模型/密钥/尺寸/时长)由 .env/设置页给出,代码不出现
  不可覆盖的硬编码】:个别地址保留"平台默认值"作兜底(如 Ark cn-beijing 端点),
  但一律可经 doubao_base_url 之类字段覆盖,没有焊死。
  见 .env.example 的完整变量清单。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "comic-agent"
    environment: str = Field(default="dev", description="dev|prod")
    api_v1_prefix: str = "/api/v1"

    # 数据库(默认 SQLite,零配置可跑)
    database_url: str = "sqlite+aiosqlite:///./comic-agent.db"

    # ---- 文本生成(厂商配置在 .env:TEXT_*)----
    text_provider: str = Field(default="fake", description="fake | openai(OpenAI 兼容)")
    text_base_url: str | None = None
    text_api_key: str | None = None
    text_model: str | None = None
    text_thinking: str = Field(
        default="auto",
        description=(
            "推理模型思考开关(auto 不传参数由服务端决定;disabled/enabled 显式指定,"
            "deepseek-v4-flash 用 disabled 直接出结构化 JSON,否则思考会先吃 max_tokens)"
        ),
    )

    # ---- 图像生成(厂商配置在 .env:IMAGE_*;SiliconFlow 等 OpenAI 兼容图像接口)----
    image_provider: str = Field(default="fake", description="fake | siliconflow")
    image_base_url: str | None = None
    image_api_key: str | None = None
    image_model: str | None = None
    image_size: str | None = Field(
        default=None, description='宽x高(按模型推荐值,Z-Image-Turbo 如 "1024x576")'
    )
    image_negative_prompt: str | None = None
    image_seed: int | None = Field(default=None, ge=0, description="生图种子;不填则随机")
    image_consistency_model: str | None = Field(
        default=None,
        description=(
            "跨镜身份锚定渲染模型(留空=纯文字,跨镜长相会漂移)。"
            "传 SiliconFlow 编辑模型如 Qwen/Qwen-Image-Edit-2509:渲染分镜时"
            "把在场角色立绘作为参考图喂给它,像素级锁身份(实测比文生图准)"
        ),
    )

    # ---- 台词配音(TTS;复用图像服务的 SiliconFlow 凭据,厂商配置在 .env:TTS_*)----
    tts_enabled: bool = Field(
        default=False,
        description="成片合成时把分镜台词逐句合成语音铺进音轨(可选通道,失败不阻塞成片)",
    )
    tts_model: str | None = Field(
        default=None,
        description="SiliconFlow 语音合成模型,如 fnlp/MOSS-TTSD-v0.5(双人对白)或 CosyVoice2-0.5B",
    )
    tts_voice: str | None = Field(
        default=None,
        description="预置音色名(必填,实测缺省报 20052),如女声 anna/claire/bella、男声 alex/benjamin;"
        "MOSS 双音色对白走 references,本通道暂不接",
    )

    # ---- 视频生成(厂商配置在 .env:DOUBAO_*/VIDEO_*;火山方舟 seedance)----
    video_provider: str = Field(default="fake", description="fake | ark")
    doubao_base_url: str | None = Field(
        default=None,
        description="火山方舟 API 端点;留空用平台默认 cn-beijing,换区域/企业网关时覆盖",
    )
    doubao_api_key: str | None = None
    doubao_video_model: str | None = None
    video_duration: int = Field(default=5, description="单镜视频时长(5 或 10 秒)")
    video_ratio: str = Field(default="adaptive", description="16:9 | 9:16 | 1:1 | adaptive")
    video_max_shots: int = Field(
        default=0, description="真实生成的最大镜头数(0=全部;演示省钱可设 2-3)"
    )
    public_base_url: str | None = Field(
        default=None,
        description="对外可访问地址;配了可把 /static 图以 URL 形式传给视频服务,否则走 data URL 内联",
    )

    # ---- 质量闭环 ----
    critique_enabled: bool = True
    critique_score_threshold: float = 6.0
    critique_max_rounds: int = 2
    critique_force_fail: bool = Field(
        default=False, description="调试开关:强制 critic 打低分,演示重做循环"
    )

    # ---- 执行 ----
    confirm_timeout_s: float = 1800.0

    def build_public_url(self, static_url: str | None) -> str | None:
        """本地 /static 路径 → 对外完整 URL(未配 public_base_url 返回原样)。"""
        if not static_url:
            return static_url
        if static_url.startswith(("http://", "https://")):
            return static_url
        if not self.public_base_url:
            return static_url
        normalized = static_url if static_url.startswith("/") else f"/{static_url}"
        return f"{self.public_base_url.rstrip('/')}{normalized}"


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=".env", _env_file_encoding="utf-8")  # type: ignore[call-arg]


def apply_settings_overrides(overrides: dict[str, Any]) -> None:
    """把面板/DB 里的配置热更到进程内 Settings 单例。

    整体校验、原地回写(lru_cache 单例身份不变,所有持有者看到新值);
    值一律字符串/None,类型转换交给 model_validate(与 env 读取同机制)。
    """
    if not overrides:
        return
    settings = get_settings()
    data = settings.model_dump()
    data.update(overrides)
    updated = Settings.model_validate(data)
    for field_name in Settings.model_fields:
        setattr(settings, field_name, getattr(updated, field_name))
