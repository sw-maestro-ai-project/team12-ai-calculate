from typing import TypedDict


class SettlementState(TypedDict, total=False):
    raw_input: str
    parsed_json: dict
    strategy: str
    calculation_result: dict
    feedback_history: list
    calc_explanation: str
    final_report: str
    safety_error: str
    # ── 피드백 루프 ──
    feedback_intent: str       # "modify_exception" | "reset" | "complaint"
    clarification_needed: str  # complaint일 때 사용자에게 보낼 되묻기 메시지
    prev_calc: dict            # 직전 calculation_result — 변경 하이라이트·불만 진단용 (front가 주입)
    change_summary: str        # 직전 결과 대비 금액 변동 요약 (결정적 생성)
