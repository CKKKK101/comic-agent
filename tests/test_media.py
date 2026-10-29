"""真实媒体客户端离线单测:httpx.MockTransport 只替换网络层,请求构造与响应解析全走生产代码。

协议防回归点(以官方文档为准,非 OpenAI 兼容):
- SiliconFlow 图像:body 用 image_size(不是 size/n/batch_size),响应顶层 images:[{url|b64_json}];
- 火山方舟 contents API:POST /contents/generations/tasks 建任务 → 轮询 tasks/{id} → content.video_url;
- 错误分层:配置缺失带 user_message(指向『设置』页);HTTP/协议错误带细节进日志。
"""
from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest

from app.config import get_settings
from app.services import media as media_module
from app.services.media import ArkVideoClient, ImageGenClient, MediaError

_SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
_IMAGE_MODEL = "Tongyi-MAI/Z-Image-Turbo"


def _image_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """完整图像配置(全部显式钉死,不依赖 .env/默认值)。"""
    s = get_settings()
    monkeypatch.setattr(s, "image_base_url", _SILICONFLOW_BASE)
    monkeypatch.setattr(s, "image_api_key", "sk-test")
    monkeypatch.setattr(s, "image_model", _IMAGE_MODEL)
    monkeypatch.setattr(s, "image_size", "1024x576")
    monkeypatch.setattr(s, "image_negative_prompt", None)
    monkeypatch.setattr(s, "image_seed", None)
    return s


def _video_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """完整视频配置(全部显式钉死,不依赖 .env/默认值)。"""
    s = get_settings()
    monkeypatch.setattr(s, "doubao_api_key", "ark-test")
    monkeypatch.setattr(s, "doubao_video_model", "doubao-seedance-test")
    monkeypatch.setattr(s, "video_ratio", "adaptive")
    monkeypatch.setattr(s, "video_duration", 5)
    monkeypatch.setattr(s, "public_base_url", None)
    return s


# ===========================================================================
# 图像生成(SiliconFlow)
# ===========================================================================

async def test_image_payload_uses_siliconflow_fields(monkeypatch) -> None:
    s = _image_settings(monkeypatch)
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"images": [{"url": "https://cdn.example/1.png"}]})

    url = await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("戴围巾的猫")
    assert url == "https://cdn.example/1.png"
    assert calls[0]["url"] == f"{_SILICONFLOW_BASE}/images/generations"
    assert calls[0]["auth"] == "Bearer sk-test"
    assert calls[0]["body"] == {"model": _IMAGE_MODEL, "prompt": "戴围巾的猫", "image_size": "1024x576"}
    # 防回归:官方协议用 image_size,不是 OpenAI 图像接口的 size/n/batch_size
    for banned in ("size", "n", "batch_size"):
        assert banned not in calls[0]["body"]


async def test_image_payload_carries_optional_fields(monkeypatch) -> None:
    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_negative_prompt", "模糊,畸形")
    monkeypatch.setattr(s, "image_seed", 7)
    body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"images": [{"url": "https://cdn.example/2.png"}]})

    await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("x")
    assert body["negative_prompt"] == "模糊,畸形"
    assert body["seed"] == 7


async def test_image_missing_config_raises_user_facing(monkeypatch) -> None:
    s = get_settings()
    for field in ("image_base_url", "image_api_key", "image_model", "image_size"):
        monkeypatch.setattr(s, field, None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("配置缺失时应直接拒绝,不发请求")

    with pytest.raises(MediaError) as ei:
        await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("x")
    assert "设置" in (ei.value.user_message or "")


async def test_image_http_error_raises_media_error(monkeypatch) -> None:
    s = _image_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    with pytest.raises(MediaError, match="401"):
        await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("x")


async def test_image_b64_json_written_to_static(monkeypatch, tmp_path) -> None:
    s = _image_settings(monkeypatch)
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02payload"
    b64 = base64.b64encode(png).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"images": [{"b64_json": b64}]})

    url = await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("x")
    assert url.startswith("/static/images/") and url.endswith(".png")
    assert (tmp_path / url.removeprefix("/static/")).read_bytes() == png


async def test_image_malformed_response_raises(monkeypatch) -> None:
    s = _image_settings(monkeypatch)

    for payload in ({"images": []}, {"images": [{"foo": "bar"}]}, "not-an-object"):

        def handler(request: httpx.Request, *, payload: Any = payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        with pytest.raises(MediaError):
            await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("x")


# ===========================================================================
# 视频生成(火山方舟 Ark contents API)
# ===========================================================================

async def test_ark_create_task_text_only_with_defaults(monkeypatch) -> None:
    s = _video_settings(monkeypatch)
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "method": request.method,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"id": "task-1"})

    task_id = await ArkVideoClient(s, transport=httpx.MockTransport(handler)).create_task(
        prompt="镜头:雪山日出", image_static_url=None
    )
    assert task_id == "task-1"
    assert calls[0]["url"] == f"{media_module.ARK_DEFAULT_BASE_URL}/contents/generations/tasks"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {
        "model": "doubao-seedance-test",
        # 官方契约:duration/ratio 走 request body 强校验,不进提示词
        "content": [{"type": "text", "text": "镜头:雪山日出"}],
        "duration": 5,
    }


async def test_ark_base_url_override_via_settings(monkeypatch) -> None:
    """doubao_base_url 可配置(换区域/企业网关):覆盖生效,不配回退平台默认。"""
    s = _video_settings(monkeypatch)
    monkeypatch.setattr(s, "doubao_base_url", "https://ark.cn-guangzhou.volces.com/api/v3")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "task-3"})

    await ArkVideoClient(s, transport=httpx.MockTransport(handler)).create_task(
        prompt="x", image_static_url=None
    )
    assert calls[0] == "https://ark.cn-guangzhou.volces.com/api/v3/contents/generations/tasks"


async def test_ark_create_task_first_frame_and_flags(monkeypatch) -> None:
    s = _video_settings(monkeypatch)
    monkeypatch.setattr(s, "video_ratio", "16:9")
    monkeypatch.setattr(s, "video_duration", 10)
    monkeypatch.setattr(s, "public_base_url", "https://demo.example.com")
    body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "task-2"})

    await ArkVideoClient(s, transport=httpx.MockTransport(handler)).create_task(
        prompt="慢推镜头", image_static_url="/static/images/shot-1.png"
    )
    assert body["ratio"] == "16:9" and body["duration"] == 10, "ratio/duration 走 body 强校验"
    assert body["content"] == [
        {"type": "text", "text": "慢推镜头"},
        {
            "type": "image_url",
            "image_url": {
                "url": "https://demo.example.com/static/images/shot-1.png",
                "role": "first_frame",
            },
        },
    ]


async def test_ark_missing_config_raises_user_facing(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "doubao_api_key", None)
    monkeypatch.setattr(s, "doubao_video_model", None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("配置缺失时应直接拒绝,不发请求")

    client = ArkVideoClient(s, transport=httpx.MockTransport(handler))
    with pytest.raises(MediaError) as ei:
        await client.create_task(prompt="x", image_static_url=None)
    assert "设置" in (ei.value.user_message or "")

    # 模型配了但缺 key:同样拦在请求发出前
    monkeypatch.setattr(s, "doubao_video_model", "doubao-seedance-test")
    with pytest.raises(MediaError) as ei:
        await client.create_task(prompt="x", image_static_url=None)
    assert "设置" in (ei.value.user_message or "")


async def test_ark_http_error_raises_media_error(monkeypatch) -> None:
    s = _video_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="gateway boom")

    with pytest.raises(MediaError, match="500"):
        await ArkVideoClient(s, transport=httpx.MockTransport(handler)).create_task(
            prompt="x", image_static_url=None
        )


async def test_ark_generate_video_polls_to_succeeded(monkeypatch) -> None:
    s = _video_settings(monkeypatch)
    polls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task-9"})
        polls.append(str(request.url))
        if len(polls) == 1:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(
            200, json={"status": "succeeded", "content": {"video_url": "https://media.example/out.mp4"}}
        )

    client = ArkVideoClient(s, transport=httpx.MockTransport(handler))
    client.poll_interval = 0.01
    url = await client.generate_video(prompt="x", image_static_url=None)
    assert url == "https://media.example/out.mp4"
    assert polls[0].endswith("/contents/generations/tasks/task-9")


async def test_ark_task_failed_raises_media_error(monkeypatch) -> None:
    s = _video_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task-x"})
        return httpx.Response(200, json={"status": "failed", "error": {"code": 5000}})

    with pytest.raises(MediaError, match="failed"):
        await ArkVideoClient(s, transport=httpx.MockTransport(handler)).generate_video(
            prompt="x", image_static_url=None
        )


async def test_ark_succeeded_without_video_url_raises(monkeypatch) -> None:
    s = _video_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task-y"})
        return httpx.Response(200, json={"status": "succeeded", "content": {}})

    with pytest.raises(MediaError):
        await ArkVideoClient(s, transport=httpx.MockTransport(handler)).generate_video(
            prompt="x", image_static_url=None
        )


async def test_ark_poll_timeout_raises(monkeypatch) -> None:
    s = _video_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task-t"})
        return httpx.Response(200, json={"status": "running"})

    client = ArkVideoClient(s, transport=httpx.MockTransport(handler))
    client.poll_interval = 0.001
    client.max_poll_time = 0.05
    with pytest.raises(TimeoutError):
        await client.generate_video(prompt="x", image_static_url=None)


# ===========================================================================
# 静态资产存取
# ===========================================================================

async def test_download_to_static_writes_local_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cdn.example/a.png?sign=1"
        return httpx.Response(200, content=b"PNG-BYTES")

    # 生产代码内部 new 的 AsyncClient 无处注入 transport → 本测试内临时替换
    real_async_client = media_module.httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(media_module.httpx, "AsyncClient", fake_client)

    url = await media_module.download_to_static("https://cdn.example/a.png?sign=1", subdir="images")
    assert url.startswith("/static/images/") and url.endswith(".png")  # 查询串不影响后缀推断
    assert (tmp_path / url.removeprefix("/static/")).read_bytes() == b"PNG-BYTES"


async def test_download_to_static_passthrough_local() -> None:
    url = await media_module.download_to_static("/static/images/keep.png", subdir="videos")
    assert url == "/static/images/keep.png"


def test_inline_local_image_data_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    images = tmp_path / "images"
    images.mkdir(parents=True)
    payload = b"\x89PNG-inline"
    (images / "shot.png").write_bytes(payload)

    data = media_module.inline_local_image("/static/images/shot.png")
    assert data.startswith("data:image/png;base64,")
    assert base64.b64decode(data.split(",", 1)[1]) == payload


def test_inline_local_image_oversize_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    monkeypatch.setattr(media_module, "MAX_INLINE_IMAGE_BYTES", 4)
    images = tmp_path / "images"
    images.mkdir(parents=True)
    (images / "big.png").write_bytes(b"12345")

    with pytest.raises(MediaError):
        media_module.inline_local_image("/static/images/big.png")


def test_inline_local_image_passthrough_remote_or_missing() -> None:
    assert media_module.inline_local_image("https://cdn.example/x.png") == "https://cdn.example/x.png"
    assert media_module.inline_local_image("/static/images/nope.png") == "/static/images/nope.png"


# ---------------------------------------------------------------------------
# 跨镜身份锚:一致性模型 + 参考图(2026-09-07 官方文档 + 实弹验证)
# - image_size:文档称 Edit-2509 不支持,但实测不带 → 纯黑背景;带 → 正常
#   1360x768 → 一律携带(经验覆盖文档);
# - 多图参考:image/image2/image3 独立字符串字段(仅 2509,上限 3)→ 逐字段;
# - 非 2509 编辑模型:无多图字段 → 并排拼"参考条"单图兜底;
# - 数组会 400 "image should be a string",永不用数组。
# ---------------------------------------------------------------------------

async def test_image_generate_with_consistency_model_payload(monkeypatch, tmp_path) -> None:
    import base64 as _b64

    from app.services import media as m

    monkeypatch.setattr(m, "STATIC_ROOT", tmp_path)
    (tmp_path / "images").mkdir()
    portrait = b"\x89PNG-portrait"
    (tmp_path / "images" / "sato.png").write_bytes(portrait)

    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_consistency_model", "Qwen/Qwen-Image-Edit-2509")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    client = ImageGenClient(s, transport=httpx.MockTransport(handler))
    url = await client.generate("scene", reference_urls=["/static/images/sato.png"])
    assert url == "https://cdn/x.png"
    body = captured["body"]
    assert body["model"] == "Qwen/Qwen-Image-Edit-2509", "参考图走一致性模型"
    assert body["image_size"] == "1024x576", "实测:不带 image_size 输出纯黑背景,必须携带"
    ref = body["image"]
    assert ref.startswith("data:image/png;base64,")
    assert _b64.b64decode(ref.split(",", 1)[1]) == portrait


async def test_image_generate_without_refs_keeps_plain_payload(monkeypatch) -> None:
    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_consistency_model", "Qwen/Qwen-Image-Edit-2509")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate("scene")
    assert captured["body"]["model"] == "Tongyi-MAI/Z-Image-Turbo"
    assert captured["body"]["image_size"] == "1024x576", "文生图模型 image_size 必填"
    assert "image" not in captured["body"], "无参考图时不带 image 字段"


# ---------------------------------------------------------------------------
# 多角色镜身份修复(官方多图字段 / 拼条兜底两路)
# ---------------------------------------------------------------------------

def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    import io as _io

    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_pngs(tmp_path, *specs: tuple[str, int, int]) -> list[str]:
    """写入 n 张合成用 PNG,返回 /static 路径列表。"""
    (tmp_path / "images").mkdir(exist_ok=True)
    urls = []
    for name, w, h in specs:
        (tmp_path / "images" / name).write_bytes(_png_bytes(w, h, (200, 40, 40)))
        urls.append(f"/static/images/{name}")
    return urls


def test_compose_reference_strip_two_images(monkeypatch, tmp_path) -> None:
    from PIL import Image as PILImage

    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("a.png", 60, 40), ("b.png", 40, 40))
    strip = media_module.compose_reference_strip(
        [media_module.disk_path(u) for u in urls]  # type: ignore[arg-type]
    )
    with PILImage.open(io.BytesIO(strip)) as out:
        # 统一缩放到高 768:60x40→1152x768,40x40→768x768,总宽 1920
        assert (out.width, out.height) == (1920, 768)


def test_compose_reference_strip_downscales_too_wide(monkeypatch, tmp_path) -> None:
    from PIL import Image as PILImage

    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("a.png", 60, 40), ("b.png", 40, 40))
    strip = media_module.compose_reference_strip(
        [media_module.disk_path(u) for u in urls], max_width=1000  # type: ignore[arg-type]
    )
    with PILImage.open(io.BytesIO(strip)) as out:
        assert (out.width, out.height) == (1000, 400), "超宽应整体等比降宽"


def test_compose_reference_strip_single_and_garbage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("ok.png", 10, 10))
    (tmp_path / "images" / "junk.png").write_bytes(b"not-an-image")

    # 垃圾图被跳过 → 只剩一张 → 原样返回(不缩放)
    strip = media_module.compose_reference_strip(
        [media_module.disk_path(u) for u in (*urls, "/static/images/junk.png")]  # type: ignore[arg-type]
    )
    assert strip == (tmp_path / "images" / "ok.png").read_bytes()

    # 全部不可读 → 明确错误
    with pytest.raises(media_module.MediaError):
        media_module.compose_reference_strip([tmp_path / "images" / "junk.png"])


async def test_image_generate_2509_multi_ref_uses_image2(monkeypatch, tmp_path) -> None:
    """多角色镜(Qwen-Image-Edit-2509):image+image2 逐字段携带,不带 image_size。"""
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("sato.png", 10, 10), ("maki.png", 20, 20))

    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_consistency_model", "Qwen/Qwen-Image-Edit-2509")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate(
        "scene", reference_urls=urls
    )
    body = captured["body"]
    assert body["image_size"] == "1024x576", "实测:不带 image_size 输出纯黑背景"
    assert "image2" in body and "image3" not in body, "两张参考 → image + image2"
    for field, name in (("image", "sato.png"), ("image2", "maki.png")):
        ref = body[field]
        assert ref.startswith("data:image/png;base64,")
        assert base64.b64decode(ref.split(",", 1)[1]) == (tmp_path / "images" / name).read_bytes(), (
            f"{field} 应携带 {name} 原图,不拼接不缩放"
        )


async def test_image_generate_other_edit_multi_ref_composes_strip(monkeypatch, tmp_path) -> None:
    """非 2509 编辑模型没有多图字段 → 两张立绘拼成一张参考条(单字符串)。"""
    from PIL import Image as PILImage

    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("a.png", 60, 40), ("b.png", 40, 40))

    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_consistency_model", "Qwen/Qwen-Image-Edit-Other")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate(
        "scene", reference_urls=urls
    )
    body = captured["body"]
    assert "image2" not in body, "非 2509 无多图字段"
    assert body["image_size"] == "1024x576"
    ref = body["image"]
    assert isinstance(ref, str) and ref.startswith("data:image/png;base64,")
    with PILImage.open(io.BytesIO(base64.b64decode(ref.split(",", 1)[1]))) as out:
        assert (out.width, out.height) == (1920, 768), "非 2509 编辑模型应拼成参考条"


async def test_image_generate_other_edit_falls_back_to_single(monkeypatch, tmp_path) -> None:
    """拼条素材不可用(非 2509)→ 降级首位角色单张锚(不硬失败,维持旧行为)。"""
    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    urls = _write_pngs(tmp_path, ("sato.png", 10, 10))
    (tmp_path / "images" / "broken.png").write_bytes(b"junk")

    s = _image_settings(monkeypatch)
    monkeypatch.setattr(s, "image_consistency_model", "Qwen/Qwen-Image-Edit-Other")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    await ImageGenClient(s, transport=httpx.MockTransport(handler)).generate(
        "scene", reference_urls=[*urls, "/static/images/broken.png"]
    )
    ref = captured["body"]["image"]
    assert isinstance(ref, str) and ref.startswith("data:image/png;base64,")
    assert base64.b64decode(ref.split(",", 1)[1]) == (tmp_path / "images" / "sato.png").read_bytes()
