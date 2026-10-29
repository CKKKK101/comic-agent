"""TTS 客户端单测(官方 create-speech 契约,2026-09 文档核实):

- 鉴权/端点复用 SiliconFlow 图像凭据;
- MOSS 模型:对白文本包 [S1] 前缀;非 MOSS:原文 + 可选 voice 字段;
- 200 → 音频二进制;非 200/空体 → MediaError(用户语指向『设置』);
- 未开启/缺凭据 → tts_blocked_reason 给引导文案。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.services.media import MediaError
from app.services.tts import TTSClient, tts_blocked_reason


def _settings(**overrides) -> Settings:
    base = {
        "tts_enabled": True,
        "tts_model": "fnlp/MOSS-TTSD-v0.5",
        "tts_voice": "anna",
        "image_provider": "siliconflow",
        "image_base_url": "https://api.siliconflow.cn/v1",
        "image_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


def test_blocked_reasons() -> None:
    assert "未开启" in tts_blocked_reason(_settings(tts_enabled=False))
    assert "SiliconFlow" in tts_blocked_reason(_settings(image_provider="fake"))
    assert "凭据" in tts_blocked_reason(_settings(image_api_key=None))
    assert "模型" in tts_blocked_reason(_settings(tts_model=None))
    assert "音色" in tts_blocked_reason(_settings(tts_voice=None))  # 实测缺 voice → 20052
    assert tts_blocked_reason(_settings()) is None


async def test_moss_payload_and_binary_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\xff\xfbmp3-bytes")

    client = TTSClient(_settings(), transport=httpx.MockTransport(handler))
    data = await client.synthesize("抓住那班车!")
    assert data == b"\xff\xfbmp3-bytes"

    req = requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/audio/speech"  # base 含 /v1 → 与官方端点一致
    assert req.headers["authorization"] == "Bearer sk-test"
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["model"] == "fnlp/MOSS-TTSD-v0.5"
    assert payload["input"] == "[S1]抓住那班车!"  # MOSS 对白标记
    assert payload["voice"] == "fnlp/MOSS-TTSD-v0.5:anna"  # 全名 = model:音色(实测必填)
    assert payload["response_format"] == "mp3"
    assert payload["sample_rate"] == 44100  # 显式 44.1k,防服务端压 32k 档致吐字糊


async def test_non_moss_plain_text_and_voice() -> None:
    import json

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"audio")

    settings = _settings(tts_model="CosyVoice2-0.5B", tts_voice="some-voice")
    client = TTSClient(settings, transport=httpx.MockTransport(handler))
    assert await client.synthesize("你好") == b"audio"

    payload = json.loads(requests[0].content)
    assert payload["input"] == "你好"  # 非 MOSS 不加 [S1] 标记
    assert payload["voice"] == "CosyVoice2-0.5B:some-voice"  # 全名格式一致


async def test_http_error_raises_user_facing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text='{"error":"insufficient quota"}')

    client = TTSClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(MediaError) as ei:
        await client.synthesize("hi")
    assert "TTS HTTP 402" in str(ei.value)
    assert "设置" in (ei.value.user_message or "")


async def test_empty_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = TTSClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(MediaError) as ei:
        await client.synthesize("hi")
    assert "空音频" in (ei.value.user_message or "")


async def test_network_error_wrapped_as_media_error() -> None:
    """网络层异常必须包成 MediaError:compose 的"配音失败不阻塞成片"只认它。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = TTSClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(MediaError) as ei:
        await client.synthesize("hi")
    assert "网络" in (ei.value.user_message or "")
