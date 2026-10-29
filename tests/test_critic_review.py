"""critic 与 review 的 LLM 路离线测试(降级路径与契约)。"""
from __future__ import annotations

import pytest

from app.agents import assess_images, decide_review_target
from app.config import get_settings
from app.prompts.critic import build_critic_system

# ---------------------------------------------------------------------------
# critic 提示词构建
# ---------------------------------------------------------------------------

def test_critic_prompt_anchors_per_kind() -> None:
    char_prompt = build_critic_system("character_images")
    shot_prompt = build_critic_system("shot_images")
    assert "角色形象图" in char_prompt and "外观描述" in char_prompt
    assert "分镜画面图" in shot_prompt and "scene" in shot_prompt
    assert char_prompt != shot_prompt


def test_critic_prompt_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        build_critic_system("video")


# ---------------------------------------------------------------------------
# assess_images LLM 路:合格/不合格/畸形输出降级
# ---------------------------------------------------------------------------

class _StubLLM:
    def __init__(self, payload: dict | None = None, *, error: bool = False) -> None:
        self._payload = payload
        self._error = error

    async def chat_json(self, **kwargs) -> dict:
        if self._error:
            from app.services.llm import LLMOutputError

            raise LLMOutputError("模拟网络故障")
        return self._payload or {}


async def _project_with_images(make_project) -> object:
    """带角色图的项目(含可供 critic 对照的描述)。"""
    project = await make_project()
    project.set_content(
        "characters",
        [{"name": "林小满", "appearance": "黑发琥珀瞳", "personality": "冷静"}],
    )
    project.set_content(
        "character_images",
        [{"character_name": "林小满", "prompt": "anime: 黑发少女", "url": "fake://c/0.png"}],
    )
    return project


def _enable_llm(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    return settings


@pytest.mark.asyncio
async def test_assess_images_llm_low_score_triggers_regenerate(make_project, monkeypatch) -> None:
    project = await _project_with_images(make_project)
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM(
            {
                "score": 5.0,
                "dimensions": {"consistency": 5, "quality": 5, "composition": 5},
                "issues": ["与描述不符"],
            }
        ),
    )
    result = await assess_images(project, settings, assets_key="character_images")
    assert result["should_regenerate"] is True
    assert result["scores"]["character_images"] == 5.0


@pytest.mark.asyncio
async def test_assess_images_llm_high_score_passes(make_project, monkeypatch) -> None:
    project = await _project_with_images(make_project)
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM(
            {
                "score": 8.6,
                "dimensions": {"consistency": 9, "quality": 8, "composition": 9},
                "issues": [],
            }
        ),
    )
    result = await assess_images(project, settings, assets_key="character_images")
    assert result["should_regenerate"] is False


@pytest.mark.asyncio
async def test_assess_images_llm_failure_falls_back_to_rules(make_project, monkeypatch) -> None:
    """critic 网络故障 → 降级规则路径(默认高分放行),质检不阻塞主流程。"""
    project = await _project_with_images(make_project)
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm", lambda s: _StubLLM(error=True)
    )
    result = await assess_images(project, settings, assets_key="character_images")
    assert result["should_regenerate"] is False  # 规则默认 8.5 分放行


@pytest.mark.asyncio
async def test_assess_images_malformed_output_falls_back(make_project, monkeypatch) -> None:
    project = await _project_with_images(make_project)
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"score": "很差"}),  # 畸形:缺 dimensions
    )
    result = await assess_images(project, settings, assets_key="character_images")
    # 畸形输出(缺 dimensions)判为无效 → 降级规则默认高分放行
    assert result["should_regenerate"] is False


# ---------------------------------------------------------------------------
# decide_review_target LLM 路
# ---------------------------------------------------------------------------

_ALLOWED = {"plan_outline", "plan_characters", "plan_shots", "render_characters", "render_shots"}


@pytest.mark.asyncio
async def test_decide_review_target_llm_respected_whitelist(make_project, monkeypatch) -> None:
    project = await make_project()
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"start_stage": "render_shots", "target_index": None, "reason": "分镜画面崩了"}),
    )
    stage, index = await decide_review_target(project, "分镜图崩了", settings, allowed=_ALLOWED)
    assert stage == "render_shots" and index is None


@pytest.mark.asyncio
async def test_decide_review_target_llm_outside_whitelist_falls_back(
    make_project, monkeypatch
) -> None:
    project = await make_project()
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"start_stage": "compose", "target_index": 1, "reason": "x"}),
    )
    stage, index = await decide_review_target(project, "角色太单薄", settings, allowed=_ALLOWED)
    assert stage == "plan_characters"  # compose 不在白名单 → 规则兜底命中"角色"
    assert index is None


@pytest.mark.asyncio
async def test_decide_review_target_target_index_forwarded_and_validated(
    make_project, monkeypatch
) -> None:
    """render_characters 分诊:LLM 点名下标 → 透出;越界/非数字 → None(渲染层兜底)。"""
    project = await make_project()
    project.set_content("characters", [{"name": "A"}, {"name": "B"}, {"name": "守时者"}])
    settings = _enable_llm(monkeypatch)

    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"start_stage": "render_characters", "target_index": 2, "reason": "手表"}),
    )
    stage, index = await decide_review_target(project, "守时者的手表错了", settings, allowed=_ALLOWED)
    assert (stage, index) == ("render_characters", 2)

    # 越界下标 → 忽略(视作未点名),不 crash
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"start_stage": "render_characters", "target_index": 99, "reason": "x"}),
    )
    _, index = await decide_review_target(project, "角色的手", settings, allowed=_ALLOWED)
    assert index is None

    # 非 render_characters 时即便给了下标也不透出
    monkeypatch.setattr(
        "app.agents.get_llm",
        lambda s: _StubLLM({"start_stage": "render_shots", "target_index": 1, "reason": "x"}),
    )
    stage, index = await decide_review_target(project, "画面崩", settings, allowed=_ALLOWED)
    assert stage == "render_shots" and index is None


@pytest.mark.asyncio
async def test_decide_review_target_llm_failure_falls_back(make_project, monkeypatch) -> None:
    project = await make_project()
    settings = _enable_llm(monkeypatch)
    monkeypatch.setattr("app.agents.get_llm", lambda s: _StubLLM(error=True))
    stage, index = await decide_review_target(project, "镜头节奏太慢", settings, allowed=_ALLOWED)
    assert stage == "plan_shots"  # 规则兜底命中"镜头"
    assert index is None


# ---------------------------------------------------------------------------
# 路由护栏:review 只能回退到已发生过的阶段(禁止跳向未来)
# ---------------------------------------------------------------------------

def test_rerun_allowlist_never_points_forward() -> None:
    from app.orchestration.nodes import _rerun_allowlist_for_gate

    # 角色门(锚点 plan_characters):只能回 plan_outline/plan_characters,
    # render_* 还没发生过,绝不能出现
    at_chars = _rerun_allowlist_for_gate("characters_approval")
    assert at_chars == {"plan_outline", "plan_characters"}

    at_shots = _rerun_allowlist_for_gate("shots_approval")
    assert at_shots == {"plan_outline", "plan_characters", "plan_shots"}

    # 分镜图门:回退范围放开到 render_shots(均已发生)
    at_shot_imgs = _rerun_allowlist_for_gate("shot_images_approval")
    assert at_shot_imgs == {"plan_outline", "plan_characters", "plan_shots",
                            "render_characters", "render_shots"}

    # compose 永远不进白名单(成片重跑走新 run/补成片)
    assert "compose" not in _rerun_allowlist_for_gate("compose_approval")
