"""创作素材存储和编辑门禁。"""
import io
import warnings

from fastapi import HTTPException, Request
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlmodel import select

from app.gateway import gateway
from app.models.agent_run import AgentRun
from app.services.media import disk_path, store_asset

MAX_UPLOAD_BYTES = 8 * 1024 * 1024


async def read_image(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "图片不能超过 8 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("format")
                if source.width * source.height > 20_000_000:
                    raise ValueError("size")
                source.load()
                result = ImageOps.exif_transpose(source).convert("RGBA")
                result.thumbnail((1400, 1400))
                output = io.BytesIO()
                result.save(output, format="PNG")
                return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise HTTPException(400, "请上传有效的 PNG、JPEG 或 WebP 图片（不超过 2000 万像素）")


def copy_image(url: str | None) -> str | None:
    if not url or url.startswith("fake://"):
        return None
    path = disk_path(url)
    if path is None:
        raise HTTPException(409, "角色图片已不存在，请重新上传")
    return store_asset(path.read_bytes(), subdir="images", suffix=path.suffix)


async def latest_run(session, project_id: int):
    return (await session.execute(
        select(AgentRun).where(AgentRun.project_id == project_id)
        .order_by(AgentRun.id.desc()).limit(1)
    )).scalars().first()


def edit_permissions(run) -> dict:
    idle = run is None or run.status in {"succeeded", "failed", "cancelled"}
    waiting = bool(run and run.status == "running" and gateway.is_waiting(run.id))
    stage = run.current_stage if run else None
    early = waiting and stage in {"outline_approval", "characters_approval"}
    shots = idle or (waiting and stage in {"shots_approval", "shot_images_approval"})
    references = idle or (waiting and stage != "compose_approval")
    return {
        "can_edit_shots": shots,
        "can_manage_cast": idle or early,
        "can_manage_references": references,
        "edit_blocker": None if idle else (
            "请在对应审批阶段编辑；生成执行中请先等待或取消，已中断任务请先恢复。"
        ),
    }


async def require_edit(session, project_id: int, permission: str):
    run = await latest_run(session, project_id)
    permissions = edit_permissions(run)
    if not permissions[permission]:
        raise HTTPException(409, permissions["edit_blocker"])
    return run


def invalidate_video(project) -> None:
    project.set_content("video", {})
    project.set_content("video_dirty", True)
    if project.status == "succeeded":
        project.status = "draft"
