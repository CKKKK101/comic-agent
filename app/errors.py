"""错误分层:同一异常携带"开发细节"(日志/调试)与"用户可读文案"(API/run 展示)。

- 抛出处写细节(message)与用户文案(user_message);
- 边界处(runner/api)用 user_facing() 取用户文案;日志打完整 str(exc)。
"""
from __future__ import annotations

# 部分抛出处只写了开发细节、漏写 user_message(网络/响应原文、JSON、URL 等
# 不该进用户界面) → 按异常类型给通用文案兜底,开发细节留在日志与 run.error
# 之前由 runner 截断的原始文本中不可见。文案统一指向『设置』页,永不给密钥/堆栈。
_GENERIC_FALLBACK: dict[str, str] = {
    "MediaError": "外部媒体服务调用失败,请稍后重试;若持续失败,请到『设置』页检查对应服务配置",
    "LLMOutputError": "文本模型输出异常,请稍后重试;若持续失败,请到『设置』页检查文本服务配置",
    "TimeoutError": "服务响应超时,请稍后重试",
}


def user_facing(exc: BaseException) -> str:
    """取用户可读文案:有 user_message 用之;没有则按已知类型给通用文案;
    未知异常(程序缺陷)退回异常文本,便于本地调试定位。"""
    msg = getattr(exc, "user_message", None)
    if isinstance(msg, str) and msg.strip():
        return msg
    generic = _GENERIC_FALLBACK.get(exc.__class__.__name__)
    if generic is not None:
        return generic
    return str(exc)
