"""参考图、分镜编辑和可复用角色库。写操作与管线确认共用项目锁。"""
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from app import agents
from app.api.projects import _project_lock
from app.config import get_settings
from app.db.session import async_session_maker
from app.db.utils import china_now
from app.errors import user_facing
from app.models.library_character import LibraryCharacter
from app.models.project import ComicProject
from app.services.creative import copy_image, invalidate_video, read_image, require_edit
from app.services.media import delete_static_asset, store_asset

router = APIRouter()


async def project_or_404(session, project_id: int):
    project = await session.get(ComicProject, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


class ShotEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    scene: str = Field(max_length=2000)
    camera: str = Field(max_length=1000)
    action: str = Field(max_length=4000)
    dialogue: str = Field(default="", max_length=4000)
    duration: float = Field(default=4, gt=0, le=60)
    character_ids: list[int] = Field(default_factory=list, max_length=30)
    image_prompt: str = Field(default="", max_length=4000)
    video_prompt: str = Field(default="", max_length=4000)


class RegenerateRequest(BaseModel):
    feedback: str = Field(default="", max_length=1000)


class CharacterFields(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    personality: str = Field(default="", max_length=2000)
    appearance: str = Field(default="", max_length=4000)
    quirks: str = Field(default="", max_length=2000)
    style: str = Field(default="", max_length=200)


@router.post("/projects/{project_id}/references", status_code=201)
async def upload_reference(
    project_id: int, request: Request,
    kind: Literal["style", "character", "shot"] = "style",
    target_index: int | None = Query(default=None, ge=0),
):
    data = await read_image(request)
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_manage_references")
        refs = list(project.get_content("reference_images") or [])
        if len(refs) >= 20:
            raise HTTPException(409, "每个项目最多保存 20 张参考图")
        if kind == "character":
            if target_index is None or target_index >= len(project.get_content("characters") or []):
                raise HTTPException(400, "请选择有效角色")
        elif kind == "shot":
            if not any(s["index"] == target_index for s in project.get_content("shots") or []):
                raise HTTPException(400, "请选择有效分镜")
        elif target_index is not None:
            raise HTTPException(400, "风格参考图无需指定角色或分镜")
        ref = {"id": uuid.uuid4().hex, "kind": kind, "target_index": target_index,
               "url": store_asset(data, subdir="images", suffix=".png")}
        if kind == "character":
            ref["target_name"] = project.get_content("characters")[target_index]["name"]
        project.set_content("reference_images", [*refs, ref])
        project.updated_at = china_now()
        session.add(project)
        try:
            await session.commit()
        except Exception:
            delete_static_asset(ref["url"])
            raise
        return ref


@router.delete("/projects/{project_id}/references/{reference_id}")
async def remove_reference(project_id: int, reference_id: str):
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_manage_references")
        refs = project.get_content("reference_images") or []
        ref = next((r for r in refs if r["id"] == reference_id), None)
        if ref is None:
            raise HTTPException(404, "参考图不存在")
        project.set_content("reference_images", [r for r in refs if r["id"] != reference_id])
        session.add(project)
        await session.commit()
    delete_static_asset(ref["url"])
    return {"deleted": True}


@router.put("/projects/{project_id}/shots/{shot_index}")
async def edit_shot(project_id: int, shot_index: int, body: ShotEdit):
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_edit_shots")
        shots = project.get_content("shots") or []
        old = next((s for s in shots if s["index"] == shot_index), None)
        if old is None:
            raise HTTPException(404, "分镜不存在")
        if any(i < 0 or i >= len(project.get_content("characters") or []) for i in body.character_ids):
            raise HTTPException(400, "分镜包含无效角色")
        new = {**old, **body.model_dump(), "index": shot_index}
        visual_changed = any(old.get(k) != new.get(k) for k in
                             ("scene", "camera", "action", "character_ids"))
        if visual_changed and new.get("image_prompt") == old.get("image_prompt"):
            new["image_prompt"] = ""
        if (visual_changed or old.get("dialogue") != new.get("dialogue")) and new.get("video_prompt") == old.get("video_prompt"):
            new["video_prompt"] = ""
        # 清空旧的 AI 提示词时，由生产者按编辑后的场景重新构造。
        for key in ("image_prompt", "video_prompt"):
            if not new[key].strip():
                new.pop(key, None)
        visual = any(old.get(k) != new.get(k) for k in
                     ("scene", "camera", "action", "character_ids", "image_prompt"))
        project.set_content("shots", [new if s["index"] == shot_index else s for s in shots])
        if visual:
            project.set_content("shot_images", [
                {**im, "stale": True} if im["shot_index"] == shot_index else im
                for im in project.get_content("shot_images") or []
            ])
        invalidate_video(project)
        project.updated_at = china_now()
        session.add(project)
        await session.commit()
        return {"shot": new, "image_stale": visual}


@router.post("/projects/{project_id}/shots/{shot_index}/regenerate")
async def regenerate_shot(project_id: int, shot_index: int, body: RegenerateRequest):
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_edit_shots")
        if not any(s["index"] == shot_index for s in project.get_content("shots") or []):
            raise HTTPException(404, "分镜不存在")
        try:
            result = await agents.render_shot_images(
                project, get_settings(),
                feedback=f"第{shot_index}镜：{body.feedback}" if body.feedback.strip() else None,
                shot_indices={shot_index},
            )
        except Exception as exc:
            raise HTTPException(502, user_facing(exc)) from exc
        project.set_content("shot_images", result["shot_images"])
        invalidate_video(project)
        project.updated_at = china_now()
        session.add(project)
        await session.commit()
        return next(im for im in result["shot_images"] if im["shot_index"] == shot_index)


@router.get("/characters")
async def list_characters():
    async with async_session_maker() as session:
        rows = (await session.execute(
            select(LibraryCharacter).order_by(LibraryCharacter.id.desc())
        )).scalars().all()
        return {"items": [r.model_dump() for r in rows]}


@router.post("/characters", status_code=201)
async def create_character(body: CharacterFields):
    if not body.name.strip():
        raise HTTPException(400, "角色名不能为空")
    async with async_session_maker() as session:
        row = LibraryCharacter(**body.model_dump())
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.model_dump()


@router.put("/characters/{character_id}")
async def update_character(character_id: int, body: CharacterFields):
    if not body.name.strip():
        raise HTTPException(400, "角色名不能为空")
    async with async_session_maker() as session:
        row = await session.get(LibraryCharacter, character_id)
        if row is None:
            raise HTTPException(404, "角色不存在")
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.model_dump()


@router.post("/characters/{character_id}/image")
async def upload_character_image(character_id: int, request: Request):
    data = await read_image(request)
    async with async_session_maker() as session:
        row = await session.get(LibraryCharacter, character_id)
        if row is None:
            raise HTTPException(404, "角色不存在")
        old = row.image_url
        row.image_url = store_asset(data, subdir="images", suffix=".png")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        result = row.model_dump()
    delete_static_asset(old)
    return result


@router.delete("/characters/{character_id}")
async def delete_character(character_id: int):
    async with async_session_maker() as session:
        row = await session.get(LibraryCharacter, character_id)
        if row is None:
            raise HTTPException(404, "角色不存在")
        image = row.image_url
        await session.delete(row)
        await session.commit()
    delete_static_asset(image)
    return {"deleted": True}


@router.post("/projects/{project_id}/characters/{character_index}/library", status_code=201)
async def save_character(project_id: int, character_index: int):
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_manage_references")
        chars = project.get_content("characters") or []
        if character_index < 0 or character_index >= len(chars):
            raise HTTPException(404, "角色不存在")
        char = chars[character_index]
        image = next((im.get("url") for im in project.get_content("character_images") or []
                      if im.get("character_name") == char["name"]), char.get("reference_url"))
        fields = {k: char.get(k, "") for k in ("name", "personality", "appearance", "quirks")}
        row = LibraryCharacter(**fields, style=project.style, image_url=copy_image(image))
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.model_dump()


@router.post("/projects/{project_id}/library-characters/{character_id}")
async def import_character(project_id: int, character_id: int):
    async with _project_lock(project_id), async_session_maker() as session:
        project = await project_or_404(session, project_id)
        await require_edit(session, project_id, "can_manage_cast")
        row = await session.get(LibraryCharacter, character_id)
        if row is None:
            raise HTTPException(404, "角色不存在")
        chars = project.get_content("characters") or []
        cast = project.get_content("cast") or []
        if any(c["name"] == row.name for c in [*chars, *cast]):
            raise HTTPException(409, "项目已存在同名角色")
        char = {k: getattr(row, k) for k in ("name", "personality", "appearance", "quirks")}
        char.update(library_id=row.id, reference_url=copy_image(row.image_url))
        project.set_content("cast", [*cast, char])
        project.set_content("characters", [*chars, char])
        # 新增角色不改变原角色下标；已有分镜保持原样。
        if char["reference_url"]:
            project.set_content("character_images", [
                *(project.get_content("character_images") or []),
                {"character_name": row.name, "url": char["reference_url"], "prompt": "角色库形象"},
            ])
        invalidate_video(project)
        session.add(project)
        await session.commit()
        return char
