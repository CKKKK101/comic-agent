"""提示词目录:每个 agent 一个模块,统一在此导出。

(与产品代码解耦:改提示词不碰逻辑;
critic 的提示词是按资产类型构建的,直接 import app.prompts.critic.build_critic_system。)
"""
from __future__ import annotations

from .characters import SYSTEM_PROMPT as CHARACTERS_SYSTEM_PROMPT
from .outline import SYSTEM_PROMPT as OUTLINE_SYSTEM_PROMPT
from .review import SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT
from .shots import SYSTEM_PROMPT as SHOTS_SYSTEM_PROMPT

__all__ = [
    "CHARACTERS_SYSTEM_PROMPT",
    "OUTLINE_SYSTEM_PROMPT",
    "REVIEW_SYSTEM_PROMPT",
    "SHOTS_SYSTEM_PROMPT",
]
