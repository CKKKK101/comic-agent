"""state.py 纯函数单测:阶段换算函数是整个坐标系的基石,先钉死它。"""
from __future__ import annotations

from app.orchestration.state import (
    PRODUCTION_STAGE_SEQUENCE,
    _resolve_base_stage,
    next_production_stage,
    workflow_progress_for_stage,
)


def test_production_sequence_has_six_stages() -> None:
    assert PRODUCTION_STAGE_SEQUENCE == (
        "plan_outline",
        "plan_characters",
        "plan_shots",
        "render_characters",
        "render_shots",
        "compose",
    )


def test_resolve_base_stage_normalizes_all_kinds() -> None:
    # 生产/审批/批评三类阶段都应归位到各自的生产锚点
    assert _resolve_base_stage("plan_outline") == "plan_outline"
    assert _resolve_base_stage("outline_approval") == "plan_outline"
    assert _resolve_base_stage("character_images_approval") == "render_characters"
    assert _resolve_base_stage("critique_shot_images") == "render_shots"
    assert _resolve_base_stage("compose_approval") == "compose"
    assert _resolve_base_stage("review") is None  # review 无生产锚点
    assert _resolve_base_stage(None) is None


def test_next_production_stage_advances_and_ends() -> None:
    assert next_production_stage("outline_approval") == "plan_characters"
    assert next_production_stage("plan_shots") == "render_characters"
    assert next_production_stage("critique_shot_images") == "compose"
    assert next_production_stage("compose") is None  # 最后一个生产阶段


def test_progress_is_6_equal_buckets() -> None:
    # 8 等分 -> 6 等分:plan_outline=0, compose=5/6;审批门用锚点下标
    assert workflow_progress_for_stage("plan_outline") == 0.0
    assert workflow_progress_for_stage("plan_outline", within_stage=1.0) == 1 / 6
    assert workflow_progress_for_stage("plan_shots") == 2 / 6
    assert workflow_progress_for_stage("compose") == 5 / 6
    # 审批门 = 它守护的生产锚点进度
    assert workflow_progress_for_stage("outline_approval") == 0.0
    assert workflow_progress_for_stage("compose_approval") == 5 / 6
    assert workflow_progress_for_stage("review") == 0.0
