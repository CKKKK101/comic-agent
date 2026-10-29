"""真实媒体能力:图像生成(SiliconFlow 图像接口)+ 视频生成(火山方舟 Ark)+ 静态资产存取。

协议均以官方文档为准(非臆造 OpenAI 兼容):
- 图像(SiliconFlow,经查证):POST {IMAGE_BASE_URL}/images/generations
  body {model, prompt, image_size, [negative_prompt, seed]}
  → 顶层 images:[{url | b64_json}](URL 1 小时过期 → 立即下载落盘);
  注意:非 OpenAI 的 size/n;batch_size/steps/guidance 各模型按默认即可。
- 视频(火山方舟 contents API):POST /contents/generations/tasks 建任务
  → 轮询 /contents/generations/tasks/{task_id} → succeeded 取 content.video_url;
  图生视频首帧:本地 /static → data URL 内联(≤8MB)或 PUBLIC_BASE_URL 外链。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import Settings

logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024

# 火山方舟默认端点(cn-beijing)。区域/企业网关可经 doubao_base_url 配置覆盖 ——
# 与 text_base_url/image_base_url 同权,代码只留"平台默认值"而非不可配常量。
ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


class MediaError(RuntimeError):
    """真实媒体服务调用失败。

    message=开发细节(进日志);user_message=用户可读文案(边界处展示)。
    """

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message


# ===========================================================================
# 静态资产存取(/static 由 FastAPI 托管)
# ===========================================================================

def ensure_static_dirs() -> None:
    for sub in ("images", "videos", "audio"):
        (STATIC_ROOT / sub).mkdir(parents=True, exist_ok=True)


def disk_path(static_url: str) -> Path | None:
    """/static/... → 磁盘路径;非本地路径返回 None。"""
    if not static_url.startswith("/static/"):
        return None
    path = (STATIC_ROOT / static_url.removeprefix("/static/")).resolve()
    return path if path.is_relative_to(STATIC_ROOT.resolve()) and path.is_file() else None


def delete_static_asset(static_url: str | None) -> None:
    """尽力删除一个 /static 资产文件(项目删除时清理磁盘;失败仅记日志不抛)。

    只删该 URL 指向的单个文件 —— 整轮重跑留下的"孤儿"旧图不在引用内,
    不在此处清理(不可达资源,待统一磁盘整理任务处理)。
    """
    path = disk_path(static_url or "")
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("删除静态资产失败(忽略): %s", path, exc_info=True)


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """RGBA/P → 白底 RGB(参考条画布是 RGB,直接转会丢/黑化透明区)。"""
    if image.mode == "RGBA":
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image, mask=image.getchannel("A"))
        return bg
    return image.convert("RGB")


def compose_reference_strip(
    paths: list[Path], *, max_width: int = 2048, target_height: int = 768
) -> bytes:
    """把多张本地立绘横向拼成一张"参考条"。

    编辑类一致性模型(image 只收单字符串)一次只能收一张参考图 —— 多角色镜
    把在场角色并排拼成一张,双身份同锁;失败由调用方降级单张锚。

    - 读不到的图跳过(其余照样拼);全部不可读抛 MediaError;
    - 等比缩放到统一高度,总宽超 max_width 再整体降宽(构图不失真);
    - 单张可用时原样返回文件字节(不缩放,避免无谓质量损失)。
    """
    images: list[Image.Image] = []
    for path in paths:
        try:
            with Image.open(path) as opened:
                images.append(_flatten_to_rgb(opened.copy()))
        except (OSError, ValueError):  # UnidentifiedImageError/截断文件等解码失败
            logger.warning("参考条跳过不可读图片: %s", path)
    if not images:
        raise MediaError(f"参考条合成:全部图片不可读(共 {len(paths)} 张)")
    if len(images) == 1:
        return paths[0].read_bytes()  # 单图原样返回,不做缩放

    resized: list[Image.Image] = []
    for image in images:
        ratio = target_height / image.height
        new_w = max(1, int(image.width * ratio))
        resized.append(image.resize((new_w, target_height), Image.Resampling.LANCZOS))
    total_w = sum(i.width for i in resized)
    if total_w > max_width:  # 太宽整体等比降宽
        ratio = max_width / total_w
        resized = [
            i.resize((max(1, int(i.width * ratio)), max(1, int(i.height * ratio))), Image.Resampling.LANCZOS)
            for i in resized
        ]

    height = max(i.height for i in resized)
    canvas = Image.new("RGB", (sum(i.width for i in resized), height), (255, 255, 255))
    x_pos = 0
    for image in resized:
        canvas.paste(image, (x_pos, 0))
        x_pos += image.width
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _bytes_data_uri(data: bytes, mime: str) -> str:
    """内存图片字节 → data URI(编辑模型参考图只收单字符串)。"""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _file_data_uri(path: Path) -> str:
    """本地图片文件 → data URI(编辑模型参考图只收单字符串)。"""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return _bytes_data_uri(path.read_bytes(), mime)


def store_asset(data: bytes, *, subdir: str, suffix: str) -> str:
    folder = STATIC_ROOT / subdir
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{suffix}"
    (folder / name).write_bytes(data)
    return f"/static/{subdir}/{name}"


async def download_to_static(url: str, *, subdir: str) -> str:
    """远程 URL → 落盘并返回 /static 路径;已是本地路径则原样返回。"""
    if url.startswith("/static/"):
        return url
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
    suffix = Path(url.split("?")[0]).suffix or ".png"
    if len(suffix) > 6:
        suffix = ".bin"
    return store_asset(resp.content, subdir=subdir, suffix=suffix)


def inline_local_image(static_url: str) -> str:
    """本地 /static 图 → data URL(喂给视频 API 当首帧);超限或非本地则原样返回。"""
    path = disk_path(static_url)
    if path is None:
        return static_url
    if path.stat().st_size > MAX_INLINE_IMAGE_BYTES:
        raise MediaError(f"图片过大无法内联: {path.stat().st_size}B > {MAX_INLINE_IMAGE_BYTES}B")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ===========================================================================
# 图像生成(SiliconFlow 图像接口,字段以官方文档为准)
# ===========================================================================

class ImageGenClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=120.0)
        return self._client

    async def generate(
        self,
        prompt: str,
        *,
        reference_urls: list[str] | None = None,
        size: str | None = None,
    ) -> str:
        """生成图片 → 远程 URL(1 小时过期,调用方应及时 download_to_static 落盘)。

        reference_urls: 本地 /static 角色立绘列表 → 用一致性模型(编辑类)做
        像素级身份锚(实测 Qwen-Image-Edit-2509 准;纯文生图跨镜长相会漂移)。

        协议实测(2026-09-07,经验覆盖文档):
        - 官方文档称 Edit-2509 不支持 image_size,但实测【不带 → 输出纯黑背景】,
          带 image_size=1024x576 → 正常 1360x768 → 一致性渲染也带 image_size;
        - 多图参考是 image/image2/image3 三个独立字符串字段(仅 2509,上限 3 张,
          数组会 400 "image should be a string")→ 多角色镜逐字段携带,全部同锁;
          第 4+ 角色由 agents 层截断(见 _shot_portrait_refs,最多 3 个像素锚);
        - 非 2509 编辑模型无多图字段 → 并排拼一张"参考条"(Pillow)兜底。
        """
        s = self._settings
        if not (s.image_base_url and s.image_api_key and s.image_model and s.image_size):
            raise MediaError(
                "图像 provider 配置不完整(env: IMAGE_BASE_URL/API_KEY/MODEL/SIZE)",
                user_message="图像服务未配置完整,请到『设置』页填写后重试",
            )
        use_consistency = bool(reference_urls) and bool(s.image_consistency_model)
        url = f"{s.image_base_url.rstrip('/')}/images/generations"
        consistency_model = s.image_consistency_model if use_consistency else s.image_model
        payload: dict[str, Any] = {
            "model": consistency_model,
            "prompt": prompt,
            "image_size": size or s.image_size,
        }
        if use_consistency:
            local_paths: list[Path] = []
            for ref_url in (reference_urls or [])[:3]:
                path = disk_path(ref_url)
                if path is None:
                    logger.warning("一致性参考图非本地文件,跳过: %s", ref_url)
                    continue
                local_paths.append(path)
            if not local_paths:
                raise MediaError(
                    "一致性渲染需要本地角色立绘参考图(全部缺失)",
                    user_message="缺少可用的角色立绘,请先完成角色形象图渲染",
                )
            if "Qwen-Image-Edit-2509" in consistency_model:
                fields = ["image"] + [f"image{i + 1}" for i in range(1, len(local_paths))]
                for field, path in zip(fields, local_paths):
                    payload[field] = _file_data_uri(path)
                if len(local_paths) > 1:
                    logger.info(
                        "多角色镜多图参考:共 %d 张立绘 → %s(身份同锁)",
                        len(local_paths),
                        "+".join(fields),
                    )
            elif len(local_paths) == 1:
                payload["image"] = _file_data_uri(local_paths[0])
            else:
                try:
                    strip = compose_reference_strip(local_paths)
                    payload["image"] = _bytes_data_uri(strip, "image/png")
                    logger.info(
                        "多角色参考条合成:共 %d 张立绘 → 单张参考条(身份同锁)", len(local_paths)
                    )
                except (OSError, ValueError) as exc:  # 拼条解码失败 → 降级首位角色
                    logger.warning("参考条合成失败,降级首位角色单张锚: %s", exc)
                    payload["image"] = _file_data_uri(local_paths[0])
        else:
            if s.image_negative_prompt:
                payload["negative_prompt"] = s.image_negative_prompt
            if s.image_seed is not None:
                payload["seed"] = s.image_seed

        http = await self._http()
        resp = await http.post(
            url, headers={"Authorization": f"Bearer {s.image_api_key}"}, json=payload
        )
        if resp.status_code != 200:
            raise MediaError(f"image API HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return _pick_image_url(data)


def _pick_image_url(data: Any) -> str:
    """官方响应 images:[{url|b64_json}];兼容聚合服务 data:[...] 形态。"""
    if not isinstance(data, dict):
        raise MediaError(f"image API 响应不是对象: {str(data)[:200]}")
    items = data.get("images")
    if not isinstance(items, list):
        items = data.get("data")  # 部分聚合层兼容形态
    if not isinstance(items, list) or not items:
        raise MediaError(f"image API 响应缺 images: {str(data)[:200]}")
    first = items[0] if isinstance(items[0], dict) else {}
    url = first.get("url")
    if isinstance(url, str) and url:
        return url
    b64 = first.get("b64_json") or first.get("image")
    if isinstance(b64, str) and b64:
        return store_asset(base64.b64decode(b64), subdir="images", suffix=".png")
    raise MediaError(f"image API 响应项缺 url/b64_json: {str(first)[:200]}")


# ===========================================================================
# 视频生成(火山方舟 doubao-seedance:任务创建 + 轮询)
# ===========================================================================

class ArkVideoClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.poll_interval = 5.0
        self.max_poll_time = 600.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=60.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        key = self._settings.doubao_api_key
        if not key:
            raise MediaError(
                "视频 provider 配置不完整(env: DOUBAO_API_KEY/DOUBAO_VIDEO_MODEL)",
                user_message="视频服务未配置完整,请到『设置』页填写后重试",
            )
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _base_url(self) -> str:
        """请求端点:配置的 doubao_base_url 优先,缺省用平台默认 cn-beijing。"""
        return (self._settings.doubao_base_url or ARK_DEFAULT_BASE_URL).rstrip("/")

    async def _post(self, path: str, payload: dict) -> dict:
        http = await self._http()
        resp = await http.post(f"{self._base_url()}{path}", headers=self._headers(), json=payload)
        if resp.status_code != 200:
            raise MediaError(f"ark HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise MediaError(f"ark 响应结构异常: {str(data)[:200]}")
        return data

    async def _get(self, path: str) -> dict:
        http = await self._http()
        resp = await http.get(f"{self._base_url()}{path}", headers=self._headers())
        if resp.status_code != 200:
            raise MediaError(f"ark HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise MediaError(f"ark 响应结构异常: {str(data)[:200]}")
        return data

    async def create_task(self, *, prompt: str, image_static_url: str | None) -> str:
        """文生视频 / 图生视频(首帧);返回任务 id。

        官方契约(contents/generations/tasks,2026-09 文档):
        - ratio/duration/resolution 走【request body 强校验】(弱校验后缀为
          --rt/--rs/--dur,与旧版 --ratio 不同,直接 body 传参最稳);
        - 1.5 pro:ratio ∈ 16:9|4:3|1:1|3:4|9:16|21:9|adaptive;duration ∈ [4,12];
          首帧图 role=first_frame;model 直填模型 ID(需先开通模型服务,
          未开通报 InvalidEndpointOrModel.NotFound)。
        """
        s = self._settings
        if not s.doubao_video_model:
            raise MediaError(
                "视频 provider 配置不完整(env: DOUBAO_VIDEO_MODEL)",
                user_message="视频服务未配置完整,请到『设置』页填写后重试",
            )
        payload: dict[str, Any] = {
            "model": s.doubao_video_model,
            "content": [{"type": "text", "text": prompt}],
        }
        if s.video_ratio and s.video_ratio != "adaptive":
            payload["ratio"] = s.video_ratio
        if s.video_duration in (4, 5, 6, 8, 10, 12):  # 1.5 pro 有效区间 [4,12]
            payload["duration"] = s.video_duration
        if image_static_url:
            public = s.build_public_url(image_static_url)
            image_url = public if public and public != image_static_url else inline_local_image(image_static_url)
            payload["content"].append(
                {"type": "image_url", "image_url": {"url": image_url, "role": "first_frame"}}
            )
        result = await self._post("/contents/generations/tasks", payload)
        task_id = result.get("id")
        if not task_id:
            raise MediaError(f"ark 响应缺少任务 id: {str(result)[:200]}")
        return str(task_id)

    async def generate_video(self, *, prompt: str, image_static_url: str | None) -> str:
        """建任务 → 轮询 → 视频 URL(调用方及时落盘)。"""
        task_id = await self.create_task(prompt=prompt, image_static_url=image_static_url)
        start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > self.max_poll_time:
                raise TimeoutError(f"ark 视频任务 {task_id} 轮询超时")
            result = await self._get(f"/contents/generations/tasks/{task_id}")
            status = result.get("status", "")
            if status == "succeeded":
                content = result.get("content") if isinstance(result.get("content"), dict) else {}
                video_url = content.get("video_url") or content.get("url") or result.get("url")
                if not video_url:
                    raise MediaError(f"ark 成功响应缺视频 URL: {str(result)[:200]}")
                logger.info("ark 视频生成成功: task=%s", task_id)
                return str(video_url)
            if status in ("failed", "cancelled"):
                raise MediaError(f"ark 视频任务 {status}: {str(result.get('error'))[:200]}")
            await asyncio.sleep(self.poll_interval)


# ===========================================================================
# 单例
# ===========================================================================

_image_client: ImageGenClient | None = None
_video_client: ArkVideoClient | None = None


def get_image_client(settings: Settings) -> ImageGenClient:
    global _image_client
    if _image_client is None:
        _image_client = ImageGenClient(settings)
    return _image_client


def get_video_client(settings: Settings) -> ArkVideoClient:
    global _video_client
    if _video_client is None:
        _video_client = ArkVideoClient(settings)
    return _video_client
