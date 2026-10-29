"""LLM 客户端(OpenAI 兼容 Chat Completions)。

- 文本 provider 走 OpenAI 兼容接口(DeepSeek/中转站均可:改 base_url + model + key);
- 统一要求模型输出 JSON,返回 dict;解析做防御(容忍代码围栏/前后缀废话);
- fake 走 producers 内置模板,不经过本客户端 —— 测试零网络、零 Key;
- transport 可注入(httpx.MockTransport):只替换网络层,请求构造/解析全走
  生产代码路径,便于离线单测(真实验证见 scripts/live_smoke.py)。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings

DEFAULT_TIMEOUT_S = 120.0


class LLMOutputError(RuntimeError):
    """LLM 返回无法解析为 JSON / HTTP 异常。

    message=开发细节(进日志);user_message=用户可读文案(边界处展示)。
    """

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象。

    容忍:代码围栏(```json ... ```)、前后缀废话;取首个平衡的 {...} 块。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        head = stripped[3:first_nl] if first_nl != -1 else ""
        stripped = stripped[first_nl + 1 :] if first_nl != -1 else stripped[3:]
        if "json" not in head.lower():
            stripped = f"{head}\n{stripped}"  # 语言标识非 json,原样保留
        stripped = stripped.removesuffix("```").strip()

    start = stripped.find("{")
    if start == -1:
        raise LLMOutputError(f"模型输出中没有 JSON 对象: {text[:120]!r}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(stripped[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMOutputError(f"JSON 解析失败: {exc}") from exc
                if not isinstance(obj, dict):
                    raise LLMOutputError("JSON 顶层必须是对象")
                return obj
    raise LLMOutputError(f"JSON 括号不闭合: {text[:120]!r}")


class LLMClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=DEFAULT_TIMEOUT_S)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.8,
        max_tokens: int = 3000,
    ) -> dict[str, Any]:
        s = self._settings
        if s.text_provider == "fake":
            raise LLMOutputError("LLMClient 不服务 fake provider(由 producers 内置模板承担)")
        if not (s.text_base_url and s.text_model):
            raise LLMOutputError(
                "文本 provider 配置不完整(env: TEXT_BASE_URL/TEXT_MODEL/TEXT_API_KEY)",
                user_message="文本服务未配置完整,请到『设置』页填写后重试",
            )
        base = s.text_base_url.rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"User-Agent": s.app_name}
        if s.text_api_key:
            headers["Authorization"] = f"Bearer {s.text_api_key}"

        payload = {
            "model": s.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 推理模型(deepseek-v4-flash 等)默认先写 reasoning_content,思考完才有
        # content;结构化 JSON 场景建议 disabled(见 Settings.text_thinking / .env)
        if s.text_thinking == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif s.text_thinking == "enabled":
            payload["thinking"] = {"type": "enabled"}
        # auto:不传参数,交给服务端

        http = await self._http()
        try:
            resp = await http.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            # 网络层必须包成 LLMOutputError:调用方(critic/review 降级、run 失败
            # 文案)只认这个异常类 —— 裸抛会把"降级不阻塞"契约击穿
            raise LLMOutputError(
                f"LLM 网络请求失败: {exc}", user_message="文本服务网络异常,请稍后重试"
            ) from exc
        if resp.status_code != 200:
            raise LLMOutputError(
                f"LLM HTTP {resp.status_code}: {resp.text[:200]}",
                user_message="文本服务调用失败,请稍后重试",
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMOutputError(
                f"LLM 200 但响应非 JSON: {resp.text[:200]}",
                user_message="文本服务响应异常,请稍后重试",
            ) from exc
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMOutputError(f"响应结构异常: {str(data)[:200]}") from exc
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # 推理模型最容易踩的坑:思考吃掉 max_tokens → content 为空
            if message.get("reasoning_content"):
                raise LLMOutputError(
                    f"模型只返回了推理内容,content 为空: {str(data)[:200]}",
                    user_message="文本服务返回空内容:请在『设置』页把『思考模式』设为 disabled 后重试",
                )
            raise LLMOutputError(
                f"模型返回空 content: {str(data)[:200]}",
                user_message="文本服务返回空内容,请稍后重试",
            )
        return extract_json(content)


_client: LLMClient | None = None


def get_llm(settings: Settings) -> LLMClient:
    """进程内单例(连接复用);测试可注入 transport 或 monkeypatch。"""
    global _client
    if _client is None:
        _client = LLMClient(settings)
    return _client
