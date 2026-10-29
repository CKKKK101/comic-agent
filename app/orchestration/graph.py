"""漫剧创作 Phase-2 图。

图结构本身保证"人机协作":生产阶段产出后必达审批门,审批门由 interrupt 裁决;
批评失败只能回自己的渲染阶段(两段渲染各自闭环,互不连坐);review 是全图唯一
"跨段回退"通道(需清下游产物,见 nodes.review_node)。

LangGraph 约定:
- HITL 用动态 interrupt()(审批节点内),不用静态 interrupt_before;
- 恢复只经 Command(resume=...) 由 driver 发出;
- 持久化需 checkpointer + 稳定 thread_id。
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from . import nodes
from .state import RunState


def build_phase2_graph() -> StateGraph:
    graph = StateGraph(RunState)

    # ---- 生产节点 ----
    for stage in (
        "plan_outline",
        "plan_characters",
        "plan_shots",
        "render_characters",
        "render_shots",
        "compose",
    ):
        graph.add_node(stage, nodes.production_node_for(stage))

    # ---- 审批门 ----
    for gate in (
        "outline_approval",
        "characters_approval",
        "shots_approval",
        "character_images_approval",
        "shot_images_approval",
        "compose_approval",
    ):
        graph.add_node(gate, nodes.approval_node_for(gate))

    # ---- 批评节点 + review ----
    graph.add_node("critique_character_images", nodes.critique_node_for("character_images"))
    graph.add_node("critique_shot_images", nodes.critique_node_for("shot_images"))
    graph.add_node("review", nodes.review_node)

    # ---- 入口(条件:恢复时可从任意 current_stage 切入)----
    graph.set_conditional_entry_point(nodes.route_from_start)

    # ---- 生产 → 审批:静态直连(规划三段;渲染段先过机器质检再看图见下;
    #     compose 例外:成片被跳过时直达 END,不打终审门)----
    for prod, gate in (
        ("plan_outline", "outline_approval"),
        ("plan_characters", "characters_approval"),
        ("plan_shots", "shots_approval"),
    ):
        graph.add_edge(prod, gate)
    graph.add_conditional_edges(
        "compose",
        nodes.route_after_compose,
        {"compose_approval": "compose_approval", "__end__": END},
    )

    # ---- 渲染段:机器质检在【前】、审批门在【后】(人只审机器放行的候选)----
    # render → critique →(不合格自动回炉 render,不再打扰人;合格)→ 审批门
    graph.add_edge("render_characters", "critique_character_images")
    graph.add_edge("render_shots", "critique_shot_images")

    # ---- 审批后路由(经 state.route_stage:通过→下一站;反馈→review/重写)----
    gate_routes: dict[str, dict[str, str]] = {
        "outline_approval": {"plan_characters": "plan_characters", "plan_outline": "plan_outline"},
        "characters_approval": {"plan_shots": "plan_shots", "review": "review"},
        "shots_approval": {"render_characters": "render_characters", "review": "review"},
        "character_images_approval": {"render_shots": "render_shots", "review": "review"},
        "shot_images_approval": {"compose": "compose", "review": "review"},
        "compose_approval": {END: END, "review": "review"},
    }
    for gate, mapping in gate_routes.items():
        graph.add_conditional_edges(gate, nodes.route_after_approval, mapping)

    # ---- 批评:不合格回自己的渲染段(自动回炉,人不再参与机器循环);
    #      合格 → 审批门(人做最终把关)----
    graph.add_conditional_edges(
        "critique_character_images",
        nodes.route_after_critique,
        {
            "render_characters": "render_characters",
            "character_images_approval": "character_images_approval",
        },
    )
    graph.add_conditional_edges(
        "critique_shot_images",
        nodes.route_after_critique,
        {"render_shots": "render_shots", "shot_images_approval": "shot_images_approval"},
    )

    # ---- review:回炉到允许的生产阶段 ----
    graph.add_conditional_edges(
        "review",
        nodes.route_after_review,
        {
            "plan_outline": "plan_outline",
            "plan_characters": "plan_characters",
            "plan_shots": "plan_shots",
            "render_characters": "render_characters",
            "render_shots": "render_shots",
        },
    )

    return graph
