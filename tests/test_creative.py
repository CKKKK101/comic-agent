"""创作功能的持久化、定向重画、素材生命周期与门禁集成测试。"""
import asyncio
import io
import time

import httpx
import pytest
from PIL import Image

from app import agents
from app.config import Settings
from app.models.project import ComicProject
from app.services import media


@pytest.fixture(autouse=True)
def isolated_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "STATIC_ROOT", tmp_path)


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (32, 24), "purple").save(out, "PNG")
    return out.getvalue()


def wait_project(client, pid, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        view = client.get(f"/api/v1/projects/{pid}").json()
        if predicate(view):
            return view
        time.sleep(.03)
    raise AssertionError(view)


def create_project(client, generated=False):
    pid = client.post("/api/v1/projects", json={"topic": "参考图与角色库的故事"}).json()["id"]
    if generated:
        assert client.post(f"/api/v1/projects/{pid}/generate", json={"auto_mode": True}).status_code == 200
        wait_project(client, pid, lambda v: v["latest_run"]["status"] == "succeeded")
    return pid


def test_edit_and_regenerate_only_one_shot_then_recompose(client):
    pid = create_project(client, generated=True)
    url = f"/api/v1/projects/{pid}"
    before = client.get(url).json()["content"]
    shot = {k: v for k, v in before["shots"][0].items() if k != "index"}
    shot.update(scene="暴雨中的车站", image_prompt="", video_prompt="")
    assert client.put(f"{url}/shots/1", json=shot).status_code == 200
    edited = client.get(url).json()
    assert edited["content"]["shot_images"][0]["stale"]
    assert edited["content"]["shot_images"][1:] == before["shot_images"][1:]
    assert not edited["content"]["video"]
    assert not edited["actions"]["can_complete_video"]
    assert client.post(f"{url}/generate", json={"start_stage": "compose"}).status_code == 409
    result = client.post(f"{url}/shots/1/regenerate", json={"feedback": "加上雨伞"})
    assert result.status_code == 200, result.text
    updated = client.get(url).json()
    assert updated["content"]["shot_images"][0]["url"] != before["shot_images"][0]["url"]
    assert updated["content"]["shot_images"][1:] == before["shot_images"][1:]
    assert updated["content"]["shots"][1:] == before["shots"][1:]
    assert updated["actions"]["can_complete_video"]
    assert client.post(f"{url}/generate", json={"start_stage": "compose", "auto_mode": True}).status_code == 200
    completed = wait_project(client, pid, lambda v: v["latest_run"]["status"] == "succeeded")
    assert completed["content"]["video"]["url"]


def test_reference_validation_persistence_and_removal(client):
    pid = create_project(client)
    url = f"/api/v1/projects/{pid}/references"
    assert client.post(url, content=b"not an image").status_code == 400
    assert client.post(url, content=b"x" * (8 * 1024 * 1024 + 1)).status_code == 413
    assert client.post(url + "?kind=character&target_index=50", content=image_bytes()).status_code == 400
    uploaded = client.post(url + "?kind=style", content=image_bytes())
    assert uploaded.status_code == 201
    ref = uploaded.json()
    path = media.disk_path(ref["url"])
    assert path and path.exists()
    assert client.post(f"/api/v1/projects/{pid}/generate", json={"auto_mode": True}).status_code == 200
    view = wait_project(client, pid, lambda v: v["latest_run"]["status"] == "succeeded")
    assert view["content"]["reference_images"] == [ref]
    assert client.delete(f"{url}/{ref['id']}").status_code == 200
    assert not path.exists()


def test_library_images_survive_source_deletion_and_project_regeneration(client):
    lib = client.post("/api/v1/characters", json={"name": "星野", "appearance": "银发蓝眼", "style": "水彩"}).json()
    cid = lib["id"]
    image = client.post(f"/api/v1/characters/{cid}/image", content=image_bytes())
    assert image.status_code == 200
    library_path = media.disk_path(image.json()["image_url"])
    pid = create_project(client)
    imported = client.post(f"/api/v1/projects/{pid}/library-characters/{cid}")
    assert imported.status_code == 200
    project_path = media.disk_path(imported.json()["reference_url"])
    assert library_path != project_path
    assert client.post(f"/api/v1/projects/{pid}/library-characters/{cid}").status_code == 409
    assert client.delete(f"/api/v1/characters/{cid}").status_code == 200
    assert not library_path.exists() and project_path.exists()
    assert client.post(f"/api/v1/projects/{pid}/generate", json={"auto_mode": True}).status_code == 200
    view = wait_project(client, pid, lambda v: v["latest_run"]["status"] == "succeeded")
    assert view["content"]["characters"][0]["name"] == "星野"
    assert view["content"]["character_images"][0]["url"] == imported.json()["reference_url"]
    saved = client.post(f"/api/v1/projects/{pid}/characters/0/library")
    assert saved.status_code == 201
    saved_path = media.disk_path(saved.json()["image_url"])
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 200
    assert saved_path.exists() and not project_path.exists()


def test_library_crud_and_invalid_names(client):
    assert client.post("/api/v1/characters", json={"name": "  "}).status_code == 400
    row = client.post("/api/v1/characters", json={"name": "主角"}).json()
    changed = client.put(f"/api/v1/characters/{row['id']}", json={"name": "女主角", "personality": "冷静"})
    assert changed.status_code == 200
    assert client.get("/api/v1/characters").json()["items"][0]["personality"] == "冷静"
    assert client.delete(f"/api/v1/characters/{row['id']}").status_code == 200
    assert client.get("/api/v1/characters").json()["items"] == []
    assert client.put("/api/v1/characters/9999", json={"name": "无"}).status_code == 404


def test_shot_edits_rejected_at_wrong_gate(client):
    pid = create_project(client)
    client.post(f"/api/v1/projects/{pid}/generate", json={})
    wait_project(client, pid, lambda v: v["latest_run"]["current_stage"] == "outline_approval")
    response = client.put(f"/api/v1/projects/{pid}/shots/1", json={"scene": "A", "camera": "B", "action": "C"})
    assert response.status_code == 409
    assert client.post(f"/api/v1/projects/{pid}/shots/1/regenerate", json={}).status_code == 409
    client.post(f"/api/v1/projects/{pid}/cancel")


async def test_uploaded_reference_reaches_image_provider(monkeypatch):
    seen = []
    class Client:
        async def generate(self, prompt, **kwargs):
            seen.append(kwargs["reference_urls"])
            return "https://image.test/result.png"
    monkeypatch.setattr(agents, "get_image_client", lambda settings: Client())
    async def download(*args, **kwargs):
        return "/static/images/new.png"
    monkeypatch.setattr(agents, "download_to_static", download)
    project = ComicProject(topic="t", content={
        "shots": [{"index": 1, "scene": "A", "action": "B"}, {"index": 2, "scene": "C"}],
        "shot_images": [{"shot_index": 2, "url": "old"}],
        "reference_images": [{"kind": "shot", "target_index": 1, "url": "/static/images/reference.png"}],
    })
    result = await agents.render_shot_images(project, Settings(image_provider="siliconflow", image_consistency_model="edit"), shot_indices={1})
    assert seen == [["/static/images/reference.png"]]
    assert result["shot_images"][1] == {"shot_index": 2, "url": "old"}


async def test_regeneration_failure_keeps_existing_images(client, monkeypatch):
    pid = create_project(client, generated=True)
    before = client.get(f"/api/v1/projects/{pid}").json()["content"]
    async def fail(*args, **kwargs):
        raise media.MediaError("provider error", user_message="图像服务暂时不可用")
    monkeypatch.setattr(agents, "render_shot_images", fail)
    result = client.post(f"/api/v1/projects/{pid}/shots/1/regenerate", json={})
    assert result.status_code == 502
    assert result.json()["detail"] == "图像服务暂时不可用"
    assert client.get(f"/api/v1/projects/{pid}").json()["content"] == before


async def test_confirm_waits_for_in_progress_shot_regeneration(db, monkeypatch):
    from app.main import create_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        pid = (await client.post("/api/v1/projects", json={"topic": "并发审批"})).json()["id"]
        url = f"/api/v1/projects/{pid}"
        run = (await client.post(f"{url}/generate", json={})).json()["run_id"]
        async def at_gate(gate):
            for _ in range(200):
                view = (await client.get(url)).json()
                if view["latest_run"]["current_stage"] == gate and view["actions"]["can_manage_references"]:
                    return view
                await asyncio.sleep(.02)
            raise AssertionError(view)
        try:
            for gate in ("outline_approval", "characters_approval", "shots_approval", "character_images_approval"):
                await at_gate(gate)
                assert (await client.post(f"{url}/confirm", json={"run_id": run})).status_code == 200
            view = await at_gate("shot_images_approval")
            shot = {k: v for k, v in view["content"]["shots"][0].items() if k != "index"}
            shot["scene"] = "雨夜"
            assert (await client.put(f"{url}/shots/1", json=shot)).status_code == 200
            assert (await client.post(f"{url}/confirm", json={"run_id": run})).status_code == 409
            started = asyncio.Event()
            original = agents.render_shot_images
            async def slow_render(*args, **kwargs):
                started.set()
                await asyncio.sleep(.1)
                return await original(*args, **kwargs)
            monkeypatch.setattr(agents, "render_shot_images", slow_render)
            generating = asyncio.create_task(client.post(f"{url}/shots/1/regenerate", json={}))
            await asyncio.wait_for(started.wait(), 5)
            confirming = asyncio.create_task(client.post(f"{url}/confirm", json={"run_id": run}))
            results = await asyncio.wait_for(asyncio.gather(generating, confirming), 10)
            assert [r.status_code for r in results] == [200, 200]
        finally:
            await client.post(f"{url}/cancel")
