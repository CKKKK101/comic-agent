"""台词配音客户端(SiliconFlow audio/speech,官方 create-speech 契约,2026-09 文档核实)。

契约要点(官方文档):
- POST {IMAGE_BASE_URL}/audio/speech,鉴权与图像服务同(Bearer image_api_key);
  body {model, input, response_format, [speed/stream...]} → 200 直接返回
  音频二进制(application/audio,响应头带 x-siliconcloud-trace-id);
- MOSS-TTSD-v0.5:对白文本用 [S1]/[S2] 标记轮流说话;双音色对白走 references
  (voice 与 references 互斥,references 仅 moss 支持)—— 本通道 v1 只做
  单音色"逐镜一句话"([S1] 前缀),角色-音色映射留待后续;
- 音色必填:voice 全名 = "{model}:{音色名}",实测缺 voice → 400 code 20052;
- CosyVoice2-0.5B 等非 MOSS 模型:input 不加标记,voice 格式同 "{model}:{音色名}"。
- 复用 SiliconFlow 凭据:不新增密钥字段;fake provider 或无凭据时通道不可用,
  调用方(compose)跳过配音且不影响成片。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.services.media import MediaError

logger = logging.getLogger(__name__)


def tts_blocked_reason(settings: Settings) -> str | None:
    """配音通道此刻能不能跑;None=可以。缺任一条件即给出设置页引导文案。"""
    if not settings.tts_enabled:
        return "台词配音未开启(『设置』→ 视频服务)"
    if settings.image_provider != "siliconflow":
        return "台词配音复用图像服务的 SiliconFlow 凭据,请先把图像服务商设为 SiliconFlow"
    if not (settings.image_base_url and settings.image_api_key):
        return "台词配音缺少 SiliconFlow 凭据,请到『设置』页完善图像服务"
    if not settings.tts_model:
        return "台词配音缺少模型(如 fnlp/MOSS-TTSD-v0.5),请到『设置』页填写"
    if not settings.tts_voice:
        # 实测(2026-09-08):MOSS 无服务端默认音色,不传 voice → 400 code 20052
        return "台词配音缺少音色(实测必填),请填预置名:女 anna/claire/bella,男 alex/benjamin/charles"
    return None


class TTSClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=120.0)
        return self._client

    async def synthesize(self, text: str) -> bytes:
        """把一句台词合成音频字节(mp3);失败抛 MediaError(用户语指向『设置』)。"""
        s = self._settings
        reason = tts_blocked_reason(s)
        if reason is not None:
            raise MediaError(f"TTS 不可用: {reason}", user_message=reason)
        payload: dict[str, Any] = {
            "model": s.tts_model,
            # 预置音色全名 = "{model}:{音色名}"(如 fnlp/MOSS-TTSD-v0.5:anna);
            # 实测必填:缺 voice → 400 code 20052 "Voice or reference audio should be set"
            "voice": f"{s.tts_model}:{s.tts_voice}",
            "response_format": "mp3",
            # 采样率显式 44.1k(mp3 支持 32k/44.1k):实测不传被服务端压到 32k
            # 档 → 吐字糊;显式 44.1k 同文本文件体积大 2.5 倍、辅音清晰
            "sample_rate": 44100,
        }
        # MOSS:对白格式 [S1]..[S2]..;单句台词包成 [S1] 一轮。非 MOSS:原文直传
        if "MOSS-TTSD" in (s.tts_model or ""):
            payload["input"] = f"[S1]{text}"
        else:
            payload["input"] = text

        url = f"{s.image_base_url.rstrip('/')}/audio/speech"
        http = await self._http()
        try:
            resp = await http.post(
                url,
                headers={"Authorization": f"Bearer {s.image_api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            # 网络层必须包成 MediaError:compose 的"配音失败不阻塞成片"只认它
            raise MediaError(
                f"TTS 网络请求失败: {exc}",
                user_message="语音合成网络异常,请稍后重试(配音失败不影响成片)",
            ) from exc
        if resp.status_code != 200:
            raise MediaError(
                f"TTS HTTP {resp.status_code}: {resp.text[:200]}",
                user_message="语音合成调用失败,请稍后重试或检查『设置』页配音配置",
            )
        if not resp.content:
            raise MediaError("TTS 200 但响应为空", user_message="语音合成返回空音频,请重试")
        return resp.content


_tts_client: TTSClient | None = None


def get_tts_client(settings: Settings) -> TTSClient:
    """进程内单例(连接复用);测试可注入 transport 或 monkeypatch。"""
    global _tts_client
    if _tts_client is None:
        _tts_client = TTSClient(settings)
    return _tts_client
