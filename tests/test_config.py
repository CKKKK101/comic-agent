"""运营配置:预检 / 种子 / 掩码 / 保存热更 / API 端点。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.session import async_session_maker
from app.models.config_item import ConfigItem
from app.services.config_service import ConfigService, is_sensitive, provider_issues
from app.services.project_view import _actions

# ---------------------------------------------------------------------------
# provider_issues 预检(纯函数)
# ---------------------------------------------------------------------------

def test_provider_issues_all_fake_is_clean() -> None:
    assert provider_issues(get_settings()) == {}


def test_provider_issues_real_but_incomplete(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    assert "text" in provider_issues(settings)  # 缺 base_url/model

    monkeypatch.setattr(settings, "text_model", "m")
    monkeypatch.setattr(settings, "text_base_url", "https://x/v1")
    monkeypatch.setattr(settings, "image_provider", "siliconflow")
    assert "image" in provider_issues(settings)  # 缺 api_key/model/size

    monkeypatch.setattr(settings, "image_api_key", "k")
    monkeypatch.setattr(settings, "image_model", "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(settings, "image_size", "1024x576")
    monkeypatch.setattr(settings, "image_base_url", "https://api.siliconflow.cn/v1")
    monkeypatch.setattr(settings, "video_provider", "ark")
    assert "video" in provider_issues(settings)

    monkeypatch.setattr(settings, "doubao_api_key", "k")
    monkeypatch.setattr(settings, "doubao_video_model", "m")
    assert provider_issues(settings) == {}


# ---------------------------------------------------------------------------
# ConfigService:种子 / 掩码 / 保存热更 / 清空
# ---------------------------------------------------------------------------

def test_sensitive_field_detection() -> None:
    assert is_sensitive("doubao_api_key") is True
    assert is_sensitive("text_model") is False


@pytest.mark.asyncio
async def test_ensure_initialized_and_save_applies(db, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "text_model", settings.text_model)
    monkeypatch.setattr(settings, "doubao_api_key", settings.doubao_api_key)

    async with async_session_maker() as session:
        service = ConfigService(session)
        await service.ensure_initialized()
        rows = (await session.execute(select(ConfigItem))).scalars().all()
        assert any(r.key == "text_provider" for r in rows), "应把当前配置种进 config_item"

        await service.save({"text_model": "deepseek-v4-flash", "doubao_api_key": "sk-secret"})
        assert settings.text_model == "deepseek-v4-flash"
        assert settings.doubao_api_key == "sk-secret"

        public = await service.list_public()
        assert public["doubao_api_key"]["is_sensitive"] is True
        assert public["doubao_api_key"]["is_set"] is True
        assert public["doubao_api_key"]["value"] == ""  # 明文不进 value
        assert public["doubao_api_key"]["current"] == ""  # 明文不进任何响应字段
        assert public["text_model"]["value"] == "deepseek-v4-flash"
        # 布尔字段必须小写序列化(str(True)="True" 会让前端 checkbox 判 false)
        assert public["tts_enabled"]["value"] == "false"
        assert public["critique_enabled"]["value"] == "true"  # Settings 默认 True
        await service.save({"tts_enabled": "true"})
        assert settings.tts_enabled is True
        assert (await service.list_public())["tts_enabled"]["value"] == "true"


@pytest.mark.asyncio
async def test_save_rejects_unknown_key(db) -> None:
    async with async_session_maker() as session:
        with pytest.raises(ValueError):
            await ConfigService(session).save({"hacker_field": "x"})


@pytest.mark.asyncio
async def test_save_empty_clears_optional(db, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", settings.public_base_url)
    async with async_session_maker() as session:
        service = ConfigService(session)
        await service.ensure_initialized()
        await service.save({"public_base_url": "http://localhost:18766"})
        assert settings.public_base_url == "http://localhost:18766"
        await service.save({"public_base_url": ""})
        assert settings.public_base_url is None  # 空串 = 清空


# ---------------------------------------------------------------------------
# project_view:前端可执行动作推导
# ---------------------------------------------------------------------------

def test_actions_derivation() -> None:
    none_view = _actions(None)
    assert none_view["can_generate"] is True and none_view["waiting_gate"] is None

    interrupted = type("R", (), {"status": "interrupted", "current_stage": "plan_shots"})()
    iv = _actions(interrupted, has_checkpoint=True)
    assert iv["can_resume"] is True and iv["can_generate"] is False
    # 无断点(崩溃于起步阶段):resume 必 409 → 给重新生成(P1-3)
    iv2 = _actions(interrupted, has_checkpoint=False)
    assert iv2["can_generate"] is True and iv2["can_resume"] is False

    running_at_gate = type("R", (), {"status": "running", "current_stage": "shots_approval"})()
    rv = _actions(running_at_gate)
    assert rv["can_cancel"] is True and rv["waiting_gate"] == "shots_approval"

    running_mid_stage = type("R", (), {"status": "running", "current_stage": "plan_shots"})()
    assert _actions(running_mid_stage)["waiting_gate"] is None

    done = type("R", (), {"status": "succeeded", "current_stage": "compose_approval"})()
    assert _actions(done)["can_generate"] is True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_config_api_flow(client) -> None:
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert "text_model" in items and "doubao_api_key" in items

    assert client.put("/api/v1/config", json={"values": {"no_such": "1"}}).status_code == 400

    orig = items["text_model"]["value"]
    r = client.put("/api/v1/config", json={"values": {"text_model": orig or "deepseek-chat"}})
    assert r.status_code == 200 and r.json()["applied"] is True


def test_generate_precheck_blocks_incomplete_real_provider(client, monkeypatch) -> None:
    """选了真实 provider 但没配全 → generate 409,引导去设置页。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    project_id = client.post("/api/v1/projects", json={"topic": "预检"}).json()["id"]
    resp = client.post(f"/api/v1/projects/{project_id}/generate", json={})
    assert resp.status_code == 409
    assert "设置" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 补生成成片入口:合成失败(非跳过)后也要能补
# ---------------------------------------------------------------------------

def test_video_actions_compose_failed_still_allows_complete(monkeypatch) -> None:
    from app.config import get_settings
    from app.services.project_view import _video_actions

    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "ark")
    monkeypatch.setattr(settings, "doubao_api_key", "k")
    monkeypatch.setattr(settings, "doubao_video_model", "m")

    failed = type("R", (), {"status": "failed", "video_pending": False})()
    content = {"shot_images": [{"shot_index": 1, "url": "/static/images/real.png"}]}
    actions = _video_actions(failed, content)
    assert actions["can_complete_video"] is True, "合成失败的 run 应保留补片入口"
    assert actions["video_blocker"] is None

    # 没到合成阶段的失败(如文本阶段,尚无分镜画面)→ 不误导
    early = type("R", (), {"status": "failed", "video_pending": False})()
    assert _video_actions(early, {"outline": {"title": "T"}})["can_complete_video"] is False

    # 成功且已有成片 → 无补片入口
    done = type("R", (), {"status": "succeeded", "video_pending": False})()
    assert _video_actions(done, {"video": {"url": "/static/videos/f.mp4"}})["can_complete_video"] is False
