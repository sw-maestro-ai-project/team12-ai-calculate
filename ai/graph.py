from langgraph.graph import END, START, StateGraph

from ai.nodes import (
    calculation_node,
    feedback_intent_node,
    feedback_parsing_node,
    input_parsing_node,
    report_generation_node,
    route_request_node,
    safety_check_node,
)
from ai.state import SettlementState


def _route_entry(state: SettlementState) -> str:
    pj = state.get("parsed_json", {})
    if pj and pj.get("participants"):
        return "feedback_intent"   # 이전 계산이 있으면 먼저 의도를 분류
    return "input_parsing"


def _route_after_intent(state: SettlementState) -> str:
    """피드백 의도에 따라 분기.

    - reset: 기존 parsed_json 무시, 원문을 새로 파싱 (input_parsing)
    - complaint: clarification_needed만 담아 흐름 종료 (END)
    - modify_exception(기본): 조건 수정 (feedback_parsing)
    """
    intent = state.get("feedback_intent", "modify_exception")
    if intent == "reset":
        return "input_parsing"
    if intent == "complaint":
        return "end"
    return "feedback_parsing"


def _route_after_safety(state: SettlementState) -> str:
    if state.get("safety_error"):
        return "end"
    return "route_request"


builder = StateGraph(SettlementState)

builder.add_node("input_parsing", input_parsing_node)
builder.add_node("safety_check", safety_check_node)
builder.add_node("route_request", route_request_node)
builder.add_node("calculation", calculation_node)
builder.add_node("report_generation", report_generation_node)
builder.add_node("feedback_intent", feedback_intent_node)
builder.add_node("feedback_parsing", feedback_parsing_node)

# Entry: initial flow or feedback flow
builder.add_conditional_edges(
    START,
    _route_entry,
    {
        "input_parsing": "input_parsing",
        "feedback_intent": "feedback_intent",
    },
)

# Feedback intent 분기
builder.add_conditional_edges(
    "feedback_intent",
    _route_after_intent,
    {
        "input_parsing": "input_parsing",   # reset
        "feedback_parsing": "feedback_parsing",  # modify_exception
        "end": END,                          # complaint → clarification_needed
    },
)

# Main flow
builder.add_edge("input_parsing", "safety_check")
builder.add_conditional_edges(
    "safety_check",
    _route_after_safety,
    {
        "route_request": "route_request",
        "end": END,
    },
)
builder.add_edge("route_request", "calculation")
builder.add_edge("calculation", "report_generation")
builder.add_edge("report_generation", END)

# Feedback flow also goes through safety_check
builder.add_edge("feedback_parsing", "safety_check")

graph = builder.compile()
