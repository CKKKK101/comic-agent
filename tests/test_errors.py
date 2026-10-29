"""errors.user_facing 分层兜底:user_message 优先 → 已知类型通用文案 → 原文本。"""
from __future__ import annotations

from app.errors import user_facing
from app.services.llm import LLMOutputError
from app.services.media import MediaError


def test_user_message_wins() -> None:
    exc = MediaError("raw detail: 500 http", user_message="媒体服务调用失败,请检查『设置』页")
    assert user_facing(exc) == "媒体服务调用失败,请检查『设置』页"


def test_known_type_without_user_message_gets_generic() -> None:
    """P2 兜底:漏写 user_message 的抛出处,细节(网络原文/JSON/URL)不得进用户界面。"""
    raw = MediaError("HTTP 500: {'error': {'message': 'invalid api key xyz'}}")
    assert user_facing(raw) != str(raw), "不得退回原始 provider 文本"
    assert "设置" in user_facing(raw)

    llm = LLMOutputError("expect json object but got: 'sorry'")
    assert user_facing(llm) != str(llm)
    assert "设置" in user_facing(llm)


def test_unknown_exception_keeps_text_for_debugging() -> None:
    assert user_facing(RuntimeError("boom-test")) == "boom-test"
