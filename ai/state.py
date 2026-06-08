from typing import TypedDict


class SettlementState(TypedDict, total=False):
    # ── 입력 ──
    raw_input: str           # 사용자 원문. 초기 정산이면 정산 상황 전체, 피드백이면 추가 조건 텍스트.

    # ── 파싱 결과 ──
    parsed_json: dict        # input_parsing / feedback_parsing이 생성하는 구조화 JSON.
                             # 참여자·항목·예외 조건(discount_rate / surcharge_rate / surcharge_amount)·
                             # 선결제(prepaid)·지원금(subsidy) 포함. 이후 노드들이 공유하는 핵심 데이터.

    # ── 전략·계산 ──
    strategy: str            # route_request_node가 결정. "SIMPLE" | "EXCEPTION" | "SPONSOR".
    calculation_result: dict # calculator/engine.calculate()의 반환값.
                             # participants(final_amount·breakdown)·floor_applied·total_verified·
                             # discount_logs·surcharge_logs·settlement 포함.

    # ── 출력 ──
    calc_explanation: str    # 계산 근거 텍스트. _build_explanation으로 코드 조립 (LLM 미사용).
    final_report: str        # 카카오톡 공유용 메시지. LLM이 최종 금액 목록만 보고 생성.

    # ── 안전 검증 ──
    safety_error: str        # safety_check_node가 감지한 오류 메시지. 비어 있으면 정상 통과.

    # ── 피드백 루프 ──
    feedback_history: list   # 이번 세션의 피드백 텍스트 누적 목록. feedback_parsing이 이력 충돌 해소에 사용.
    feedback_intent: str     # feedback_intent_node가 분류한 의도. "modify_exception" | "reset" | "complaint".
    clarification_needed: str  # complaint 의도일 때 사용자에게 보낼 되묻기 메시지 (LLM 미사용, 자가 진단).
    prev_calc: dict          # 직전 calculation_result. front가 주입하며 변경 하이라이트·불만 진단에 사용.
    change_summary: str      # 직전 결과 대비 참여자별 금액 변동 요약. _build_change_summary로 결정적 생성.
