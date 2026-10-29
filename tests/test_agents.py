"""agents.py 单测:生产者的内容契约 + review 规则分诊。"""
from __future__ import annotations

import pytest

from app.agents import decide_rerun_target, produce_outline, produce_shots
from app.config import get_settings


@pytest.mark.asyncio
async def test_produce_outline_returns_expected_contract(make_project) -> None:
    project = await make_project(topic="咖啡师与时间旅行者")
    result = await produce_outline(project, get_settings())
    outline = result["outline"]
    assert outline["title"]
    assert len(outline["acts"]) == 3  # 三幕结构
    assert "summary" in result


@pytest.mark.asyncio
async def test_produce_outline_is_deterministic(make_project) -> None:
    p1 = await make_project(topic="雾都侦探")
    p2 = await make_project(topic="雾都侦探")
    r1 = await produce_outline(p1, get_settings())
    r2 = await produce_outline(p2, get_settings())
    assert r1["outline"]["title"] == r2["outline"]["title"]


@pytest.mark.asyncio
async def test_produce_shots_references_existing_characters(make_project) -> None:
    project = await make_project()
    project.set_content("characters", [{"name": "A"}, {"name": "B"}])
    result = await produce_shots(project, get_settings())
    shots = result["shots"]
    assert len(shots) >= 3
    for shot in shots:
        assert shot["character_ids"], "分镜必须引用至少一个角色"
    assert "summary" in result


def test_decide_rerun_target_keywords() -> None:
    allowed = {"plan_outline", "plan_characters", "plan_shots", "render_characters", "render_shots"}
    assert decide_rerun_target("大纲太拖沓,故事要更紧凑", allowed=allowed) == "plan_outline"
    assert decide_rerun_target("角色太单薄,性格不鲜明", allowed=allowed) == "plan_characters"
    assert decide_rerun_target("镜头节奏太慢", allowed=allowed) == "plan_shots"
    assert decide_rerun_target("角色图的脸崩了", allowed=allowed) == "render_characters"
    assert decide_rerun_target("画面构图不行", allowed=allowed) == "render_shots"


def test_decide_rerun_target_default_and_allowed_filter() -> None:
    allowed = {"plan_outline", "plan_shots", "render_characters", "render_shots"}
    # 未命中关键词 → 默认 plan_characters 不在 allowed → 兜底 plan_outline
    assert decide_rerun_target("看不懂", allowed=allowed) == "plan_outline"
    # 命中关键词但不在 allowed → 也会被过滤(生产端兜底)
    assert decide_rerun_target("画面模糊", allowed={"plan_outline", "plan_shots"}) == "plan_outline"


# ---------------------------------------------------------------------------
# 定向修改轮:produce_characters 上下文带现有阵容与用户反馈
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_produce_characters_feedback_carries_existing_cast(make_project, monkeypatch) -> None:
    """回炉轮必须把现有角色+反馈给 LLM,否则会整组重写(名字漂移)。"""
    captured: dict = {}

    class _CaptureLLM:
        async def chat_json(self, *, system: str, user: str, **kw) -> dict:
            captured["user"] = user
            return {"user_message": "改好了", "characters": [{"name": "佐藤晴人", "personality": "温和", "appearance": "绿瞳"}, {"name": "真希", "personality": "坚韧", "appearance": "深蓝短发"}]}

    project = await make_project()
    project.set_content(
        "characters",
        [{"name": "佐藤晴人", "personality": "温和", "appearance": "深灰瞳"}],
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr("app.agents.get_llm", lambda s: _CaptureLLM())

    from app.agents import produce_characters

    await produce_characters(project, settings, feedback="佐藤晴人瞳色改绿")
    user = captured["user"]
    assert "现有角色" in user and "佐藤晴人" in user, "现有阵容必须进上下文"
    assert "用户反馈:佐藤晴人瞳色改绿" in user
    # 首轮(无反馈)不夹带
    captured.clear()
    await produce_characters(project, settings)
    assert "现有角色" not in captured["user"]


# ---------------------------------------------------------------------------
# 定向修改轮:outline / shots 上下文带现有内容与反馈
# ---------------------------------------------------------------------------

async def _capture_produce(monkeypatch, settings, project, fn, feedback=None, chars=True):
    captured: dict = {}

    class _CapLLM:
        async def chat_json(self, *, system: str, user: str, **kw) -> dict:
            captured["user"] = user
            return {"user_message": "done", "outline": {"title": "T", "logline": "L", "acts": [{"title": "a", "plot": "p"}]}, "characters": [
                {"name": "真希", "personality": "坚韧", "appearance": "长发"},
                {"name": "佐藤晴人", "personality": "温和", "appearance": "黄瞳"},
            ], "shots": [{"index": 1, "scene": "S", "action": "A"}]}

    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr("app.agents.get_llm", lambda s: _CapLLM())
    return captured


@pytest.mark.asyncio
async def test_produce_outline_feedback_carries_existing(make_project, monkeypatch) -> None:
    from app.agents import produce_outline

    project = await make_project()
    project.set_content("outline", {"title": "倒行的末班车", "logline": "L", "acts": [{"title": "起", "plot": "P"}]})
    settings = get_settings()
    cap = await _capture_produce(monkeypatch, settings, project, None)
    await produce_outline(project, settings, feedback="结局改圆满一些")
    user = cap["user"]
    assert "现有大纲" in user and "倒行的末班车" in user and "用户反馈:结局改圆满一些" in user


@pytest.mark.asyncio
async def test_produce_shots_feedback_carries_existing_shots(make_project, monkeypatch) -> None:
    from app.agents import produce_shots

    project = await make_project()
    project.set_content(
        "shots",
        [{"index": 1, "scene": "车厢", "action": "低头", "image_prompt": "ip1", "video_prompt": "vp1"},
         {"index": 2, "scene": "站台", "action": "抬头", "image_prompt": "ip2", "video_prompt": "vp2"}],
    )
    settings = get_settings()
    cap = await _capture_produce(monkeypatch, settings, project, None)
    await produce_shots(project, settings, feedback="第2镜动作改慢")
    user = cap["user"]
    assert "现有分镜" in user and "第2镜动作改慢" in user
    assert "#1" in user and "#2" in user, "现有分镜逐字进上下文"
    assert "ip2" in user, "未点名镜头的 image_prompt 也要原样带出"


# ---------------------------------------------------------------------------
# 分镜索引合并:未点名镜头代码级保留
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_produce_shots_named_index_merges_others_verbatim(make_project, monkeypatch) -> None:
    """反馈点名第2镜 → LLM 输出只采信第2镜,其余沿用旧数据(不信模型逐字)。"""
    from app.agents import produce_shots

    existing = [
        {"index": 1, "scene": "车厢", "action": "低头", "image_prompt": "ip1", "video_prompt": "vp1"},
        {"index": 2, "scene": "站台", "action": "旧动作", "image_prompt": "ip2", "video_prompt": "vp2"},
        {"index": 3, "scene": "病房", "action": "认人", "image_prompt": "ip3", "video_prompt": "vp3"},
    ]
    project = await make_project()
    project.set_content("shots", existing)

    class _ShotLLM:
        async def chat_json(self, **kw) -> dict:
            # 模型"不听话":把 1、3 也改了 —— 合并层必须无视
            return {"user_message": "done", "shots": [
                {"index": 1, "scene": "被改", "action": "改", "image_prompt": "改1"},
                {"index": 2, "scene": "站台", "action": "新动作", "image_prompt": "ip2new", "video_prompt": "vp2new"},
                {"index": 3, "scene": "被改3", "action": "改", "image_prompt": "改3"},
            ]}

    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    monkeypatch.setattr("app.agents.get_llm", lambda s: _ShotLLM())

    out = await produce_shots(project, settings, feedback="第2镜动作改慢")
    shots = {s["index"]: s for s in out["shots"]}
    assert shots[1] == existing[0], "未点名镜头必须原样保留"
    assert shots[3] == existing[2], "未点名镜头必须原样保留"
    assert shots[2]["action"] == "新动作" and shots[2]["image_prompt"] == "ip2new"


@pytest.mark.asyncio
async def test_produce_shots_feedback_without_target_preserves_without_llm(
    make_project, monkeypatch
) -> None:
    """级联场景(反馈只改角色字段)→ 分镜无点名 → 不调 LLM,整体原样。"""
    from app.agents import produce_shots

    existing = [{"index": 1, "scene": "S", "action": "A"}]
    project = await make_project()
    project.set_content("shots", existing)
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")

    called = []
    monkeypatch.setattr("app.agents.get_llm", lambda s: called.append(1) or _FakeLLM())
    out = await produce_shots(project, settings, feedback="佐藤晴人发色改为黑色")
    assert called == [], "无点名且非全局重写时不应调用 LLM"
    assert out["shots"] == existing


class _FakeLLM:
    async def chat_json(self, **kw) -> dict:
        return {"user_message": "x", "shots": []}
