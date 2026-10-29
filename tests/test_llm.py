"""services/llm.py 与 agents LLM 路 glue 的离线单测。

零网络:httpx.MockTransport 只替换传输层,请求构造/解析走生产代码;
LLM 路的归一化用 stub client 喂确定性 JSON,校验我们的契约代码。
真实模型验证属"评估"范畴,不在自动套件内(有 Key 时手动跑 demo 即可)。
"""
from __future__ import annotations

import httpx
import pytest

from app.agents import (
    _normalize_characters,
    _normalize_outline,
    _normalize_shots,
    produce_characters,
    produce_outline,
    produce_shots,
)
from app.config import Settings, get_settings
from app.services.llm import LLMClient, LLMOutputError, extract_json

# ---------------------------------------------------------------------------
# extract_json:容忍代码围栏/废话,拒绝坏 JSON
# ---------------------------------------------------------------------------

def test_extract_json_from_code_fence() -> None:
    text = '```json\n{"outline": {"title": "A"}}\n```'
    assert extract_json(text) == {"outline": {"title": "A"}}


def test_extract_json_with_prefix_chatter() -> None:
    text = '好的,以下是大纲:\n{"title": "夜行者", "acts": []} 希望对你有帮助'
    assert extract_json(text)["title"] == "夜行者"


def test_extract_json_rejects_non_json() -> None:
    with pytest.raises(LLMOutputError):
        extract_json("抱歉,我无法生成 JSON")


def test_extract_json_rejects_unbalanced_braces() -> None:
    with pytest.raises(LLMOutputError):
        extract_json('{"a": {"b": 1}')


# ---------------------------------------------------------------------------
# LLMClient:请求构造/鉴权/错误路径(传输层 Mock)
# ---------------------------------------------------------------------------

def _client_with(handler) -> LLMClient:
    settings = Settings(
        _env_file=None,  # 纯默认值,不读仓库 .env
        text_provider="openai",
        text_base_url="https://api.example.com/v1",
        text_api_key="sk-test",
        text_model="demo-model",
    )
    return LLMClient(settings, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_chat_json_builds_request_and_parses() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"outline": {"title": "T"}}'}}]}
        )

    client = _client_with(handler)
    result = await client.chat_json(system="s", user="u")
    assert result == {"outline": {"title": "T"}}
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert b'"demo-model"' in body
    assert b'"role":"system"' in body  # httpx json 序列化为紧凑格式


@pytest.mark.asyncio
async def test_chat_json_http_error_raises() -> None:
    client = _client_with(lambda _request: httpx.Response(429, text="rate limited"))
    with pytest.raises(LLMOutputError):
        await client.chat_json(system="s", user="u")


@pytest.mark.asyncio
async def test_chat_json_network_error_wrapped() -> None:
    """网络层异常必须包成 LLMOutputError:critic/review 的"降级不阻塞"只认它。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client_with(handler)
    with pytest.raises(LLMOutputError) as ei:
        await client.chat_json(system="s", user="u")
    assert "网络" in (ei.value.user_message or "")


@pytest.mark.asyncio
async def test_chat_json_refuses_fake_provider() -> None:
    settings = Settings(_env_file=None, text_provider="fake")
    client = LLMClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LLMOutputError):
        await client.chat_json(system="s", user="u")


# ---------------------------------------------------------------------------
# 归一化契约:Schema 与代码是同一份
# ---------------------------------------------------------------------------

def test_normalize_outline_accepts_valid_and_trim() -> None:
    data = {
        "title": "T",
        "logline": "L",
        "genre": ["都市"],
        "acts": [{"title": "起", "plot": "p1"}],
    }
    out = _normalize_outline(data)
    assert out["acts"][0]["plot"] == "p1"
    assert out["genre"] == ["都市"]
    assert out["setting"] == ""


def test_normalize_outline_rejects_missing_acts() -> None:
    with pytest.raises(LLMOutputError):
        _normalize_outline({"title": "T"})


def test_normalize_characters_bounds() -> None:
    ok = {"characters": [{"name": "A"}, {"name": "B"}]}
    assert len(_normalize_characters(ok)) == 2
    too_many = {"characters": [{"name": str(i)} for i in range(8)]}
    with pytest.raises(LLMOutputError):
        _normalize_characters(too_many)
    unnamed = {"characters": [{"name": "  "}, {"name": "B"}]}
    with pytest.raises(LLMOutputError):
        _normalize_characters(unnamed)


def test_normalize_shots_clamps_ids_and_duration() -> None:
    data = {
        "shots": [
            {"scene": "S", "character_ids": [0, 1, 99, "x"], "duration": 99},
            {"scene": "T", "character_ids": None},
        ]
    }
    shots = _normalize_shots(data, n_char=2)
    assert shots[0]["character_ids"] == [0, 1]  # 99 越界被剔除
    assert shots[0]["duration"] == 20.0  # 钳到上限
    assert shots[1]["character_ids"] == [0]  # 缺失时兜底第 0 号角色
    assert shots[0]["index"] == 1  # 索引由代码重排,不信模型


# ---------------------------------------------------------------------------
# produce_* 的 LLM 路:stub client 喂 JSON → 契约输出(离线)
# ---------------------------------------------------------------------------

class _StubLLM:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def chat_json(self, **kwargs) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_produce_outline_llm_path_uses_user_message(make_project, monkeypatch) -> None:
    project = await make_project(topic="测试故事")
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM(
            {
                "user_message": "我按三幕搭好了骨架",
                "outline": {
                    "title": "LLM标题",
                    "logline": "L",
                    "acts": [{"title": "起", "plot": "p"}],
                },
            }
        ),
    )
    result = await produce_outline(project, settings)
    assert result["outline"]["title"] == "LLM标题"
    assert result["summary"] == "我按三幕搭好了骨架"


@pytest.mark.asyncio
async def test_produce_characters_llm_path_rejects_bad_shape(make_project, monkeypatch) -> None:
    project = await make_project()
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr(
        "app.agents.get_llm", lambda s: _StubLLM({"characters": [{"name": "唯一角色"}]})
    )
    with pytest.raises(LLMOutputError):
        await produce_characters(project, settings)


@pytest.mark.asyncio
async def test_produce_shots_llm_path_filters_unknown_characters(make_project, monkeypatch) -> None:
    project = await make_project()
    project.set_content("characters", [{"name": "A"}, {"name": "B"}])
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"shots": [{"scene": "S", "character_ids": [3, 1], "duration": 4}]}),
    )
    result = await produce_shots(project, settings)
    assert result["shots"][0]["character_ids"] == [1]  # 索引 3 不存在,被过滤


# ---------------------------------------------------------------------------
# 推理模型:text_thinking 参数 + 空 content 兜底(deepseek-v4-flash 类)
# ---------------------------------------------------------------------------

def _client_with_thinking(handler, thinking: str) -> LLMClient:
    settings = Settings(
        _env_file=None,
        text_provider="openai",
        text_base_url="https://api.example.com/v1",
        text_api_key="sk-test",
        text_model="demo-model",
        text_thinking=thinking,
    )
    return LLMClient(settings, transport=httpx.MockTransport(handler))


def _capture_handler(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    return handler


@pytest.mark.asyncio
async def test_chat_json_thinking_disabled_is_sent() -> None:
    captured: dict = {}
    client = _client_with_thinking(_capture_handler(captured), thinking="disabled")
    await client.chat_json(system="s", user="u")
    assert b'"thinking":{"type":"disabled"}' in captured["body"]


@pytest.mark.asyncio
async def test_chat_json_thinking_enabled_is_sent() -> None:
    captured: dict = {}
    client = _client_with_thinking(_capture_handler(captured), thinking="enabled")
    await client.chat_json(system="s", user="u")
    assert b'"thinking":{"type":"enabled"}' in captured["body"]


@pytest.mark.asyncio
async def test_chat_json_thinking_auto_omits_param() -> None:
    captured: dict = {}
    client = _client_with_thinking(_capture_handler(captured), thinking="auto")
    await client.chat_json(system="s", user="u")
    assert b"thinking" not in captured["body"], "auto 不传 thinking 参数,交给服务端"


@pytest.mark.asyncio
async def test_chat_json_empty_content_with_reasoning_points_to_settings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # deepseek-v4-flash 思考吃掉 max_tokens 的典型响应:content 空、reasoning 有货
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "reasoning_content": "让我想想…思考思考…"}}
                ]
            },
        )

    client = _client_with(handler)
    with pytest.raises(LLMOutputError) as ei:
        await client.chat_json(system="s", user="u")
    assert "思考模式" in (ei.value.user_message or ""), "要引导用户去『设置』页关思考"


@pytest.mark.asyncio
async def test_chat_json_empty_content_plain_raises_user_facing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    client = _client_with(handler)
    with pytest.raises(LLMOutputError) as ei:
        await client.chat_json(system="s", user="u")
    assert "空内容" in (ei.value.user_message or "")
