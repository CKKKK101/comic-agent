"""视频"跳过不失败"语义:video_pending + 补成片。

- 入口预检只拦 text/image;视频没配也放行;
- 成片阶段图内自跳过(compose_blocked_reason),run 成功并打 video_pending;
- 配置完善后:前端动作 can_complete_video → 以 start_stage=compose 补生成。
"""
from __future__ import annotations

import time

from app.config import get_settings
from app.services.config_service import blocking_issues, compose_blocked_reason, provider_issues

_TOPIC = "雪国列车上的魔术师"


def _wait_run_finished(client, project_id: int, *, expect: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        status = view["latest_run"]["status"]
        if status == expect:
            return view
        time.sleep(0.05)
    raise AssertionError(f"等待 run 状态 {expect} 超时,最后状态 {status}")


# ---------------------------------------------------------------------------
# 纯函数:预检拆分 + compose 阻塞原因
# ---------------------------------------------------------------------------

def test_video_issue_is_not_blocking(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "ark")
    assert "video" in provider_issues(settings)  # 全量问题里能看到(设置页提示)
    assert blocking_issues(settings) == {}  # 但入口不拦


def test_compose_blocked_reason_matrix(monkeypatch) -> None:
    settings = get_settings()
    # fake provider:离线占位,永不阻塞
    assert compose_blocked_reason(settings, []) is None

    monkeypatch.setattr(settings, "video_provider", "ark")
    # 缺凭据 → 视频问题
    assert "视频服务" in (compose_blocked_reason(settings, [{"url": "/static/a.png"}]) or "")
    monkeypatch.setattr(settings, "doubao_api_key", "k")
    monkeypatch.setattr(settings, "doubao_video_model", "m")
    # 没有分镜图
    assert "分镜" in (compose_blocked_reason(settings, None) or "")
    # 分镜是占位/非本地 → 不能合成
    assert "占位" in (compose_blocked_reason(settings, [{"url": "fake://shot/1/v1.png"}]) or "")
    # 本地真实分镜 → 放行
    assert compose_blocked_reason(settings, [{"url": "/static/images/1.png"}]) is None


# ---------------------------------------------------------------------------
# API:入口放行 → 图内跳过 → video_pending → 补成片从 compose 起步
# ---------------------------------------------------------------------------

def test_video_incomplete_run_skips_compose_and_marks_pending(client, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "ark")  # 有 provider 没凭据

    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    # 视频未配置 → generate 仍然 200(不再 409)
    resp = client.post(f"/api/v1/projects/{project_id}/generate", json={"auto_mode": True})
    assert resp.status_code == 200

    view = _wait_run_finished(client, project_id, expect="succeeded")
    run = view["latest_run"]
    assert run["video_pending"] is True
    assert run["current_stage"] == "compose"
    assert "video" not in view["content"], "跳过时不应写入成片内容"
    assert any("跳过成片" in m["content"] for m in view["messages"])
    # 服务还没配好 → 不能补,给出去『设置』页的引导
    assert view["actions"]["can_complete_video"] is False
    assert "视频服务" in (view["actions"]["video_blocker"] or "")


def test_complete_video_action_when_ready(monkeypatch) -> None:
    """video_pending 的 run + 配置已完善 + 分镜已是本地文件 → 可补成片。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "video_provider", "ark")
    monkeypatch.setattr(settings, "doubao_api_key", "k")
    monkeypatch.setattr(settings, "doubao_video_model", "m")

    from app.services.project_view import _video_actions

    pending = type("R", (), {"video_pending": True})()
    content = {"shot_images": [{"url": "/static/images/s1.png"}]}
    actions = _video_actions(pending, content)
    assert actions["can_complete_video"] is True and actions["video_blocker"] is None

    # 已有成片 → 无需补
    done = _video_actions(pending, {"video": {"url": "/static/videos/f.mp4"}})
    assert done["can_complete_video"] is False


def test_generate_start_stage_compose_ends_at_final_gate(client) -> None:
    """补成片入口:从 compose 起步,复用已有内容,终审仍是人工确认。"""
    from tests.test_api import _confirm_gate

    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]

    assert client.post(
        f"/api/v1/projects/{project_id}/generate",
        json={"start_stage": "plan_shots"},  # 白名单外的 start_stage
    ).status_code == 400

    resp = client.post(
        f"/api/v1/projects/{project_id}/generate",
        json={"start_stage": "compose"},  # fake 视频 → 正常合成占位成片,进终审门
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # 只应经过 compose → compose_approval 一道门;通过即成功
    # (helper 带重试:裁决须打在 run 真正停在门的时刻,见 gateway 状态机)
    deadline = time.time() + 15
    confirmed = False
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] == "running" and run["current_stage"] == "compose_approval":
            _confirm_gate(client, project_id, run_id, feedback="")
            confirmed = True
            break
        time.sleep(0.05)
    assert confirmed, "应停在终审门等人确认"
    view = _wait_run_finished(client, project_id, expect="succeeded")
    assert view["content"]["video"]["url"].startswith("fake://")
    assert view["latest_run"]["video_pending"] is False
