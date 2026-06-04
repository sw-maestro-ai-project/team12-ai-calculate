"""피드백 루프의 결정적 헬퍼에 대한 단위 테스트 (LLM 미사용).

ai.nodes import 시 모듈 레벨에서 OpenAI 클라이언트를 생성하므로, 키가 없는
환경에서도 import가 깨지지 않도록 더미 키를 먼저 설정한다 (실제 호출은 하지 않음).
"""
import os

os.environ.setdefault("UPSTAGE_API_KEY", "test-key-for-import")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from ai.nodes import (  # noqa: E402
    _build_change_summary,
    _build_complaint_clarification,
    _hoist_fixed_amount,
    _participant_overlap,
)


# ── _hoist_fixed_amount (잘못 중첩된 fixed_amount 교정) ─────────────────────

def test_hoist_fixed_amount_from_amount_pattern():
    p = {"name": "A", "exceptions": [{"type": "fixed_amount", "amount": 10000}]}
    _hoist_fixed_amount(p)
    assert p["fixed_amount"] == 10000
    assert p["exceptions"] == []


def test_hoist_fixed_amount_from_key_pattern():
    p = {"name": "A", "exceptions": [{"fixed_amount": 20000}]}
    _hoist_fixed_amount(p)
    assert p["fixed_amount"] == 20000
    assert p["exceptions"] == []


def test_hoist_preserves_other_exceptions():
    p = {"name": "A", "exceptions": [
        {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0},
        {"type": "fixed_amount", "amount": 5000},
    ]}
    _hoist_fixed_amount(p)
    assert p["fixed_amount"] == 5000
    assert len(p["exceptions"]) == 1
    assert p["exceptions"][0]["discount_rate"] == 1.0


def test_hoist_noop_when_absent():
    p = {"name": "A", "exceptions": [{"type": "지각", "surcharge_amount": 3000}]}
    _hoist_fixed_amount(p)
    assert "fixed_amount" not in p
    assert len(p["exceptions"]) == 1


# ── _participant_overlap (reset guard 판정) ────────────────────────────────

def test_overlap_identical():
    assert _participant_overlap(["A", "B", "C"], ["A", "B", "C"]) == 1.0


def test_overlap_disjoint():
    assert _participant_overlap(["A", "B"], ["C", "D"]) == 0.0


def test_overlap_empty_returns_zero():
    assert _participant_overlap([], ["A"]) == 0.0
    assert _participant_overlap(["A"], []) == 0.0


def test_overlap_below_half_triggers_reset():
    # {A,B} ∩ = 2, ∪ = 6 → 0.33 < 0.5 → reset 대상
    assert _participant_overlap(["A", "B", "C", "D"], ["A", "B", "E", "F"]) < 0.5


def test_overlap_at_or_above_half_stays_feedback():
    # ∩ = 3, ∪ = 5 → 0.6 ≥ 0.5 → 피드백 유지
    assert _participant_overlap(["A", "B", "C", "D"], ["A", "B", "C", "E"]) >= 0.5


# ── _build_complaint_clarification (불만 자가 진단) ─────────────────────────

def test_clarification_empty_cr_fallback():
    msg = _build_complaint_clarification({})
    assert "어떤 부분이 이상한지" in msg


def test_clarification_mentions_floor():
    msg = _build_complaint_clarification({"floor_applied": ["D"]})
    assert "D" in msg and "하한선" in msg


def test_clarification_mentions_surcharge():
    msg = _build_complaint_clarification({"surcharge_logs": {"C": ["..."]}})
    assert "C" in msg and "할증" in msg


def test_clarification_mentions_multi_discount():
    msg = _build_complaint_clarification(
        {"discount_logs": {"E": ["항목1", "항목2"], "A": ["항목1"]}}
    )
    assert "E" in msg and "감액" in msg
    # 항목이 하나뿐인 A는 '여러 항목 감액' 힌트 대상이 아니다
    assert "A님" not in msg


# ── _build_change_summary (변경 사항 하이라이트) ───────────────────────────

def test_change_summary_reports_only_changes():
    prev = {"A": 28000, "B": 20000, "C": 20000}
    new = [
        {"name": "A", "final_amount": 23000},
        {"name": "B", "final_amount": 20000},  # 변동 없음 → 생략
        {"name": "C", "final_amount": 25000},
    ]
    summary = _build_change_summary(prev, new)
    assert "A: 28,000원 → 23,000원 (−5,000원)" in summary
    assert "C: 20,000원 → 25,000원 (+5,000원)" in summary
    assert "B:" not in summary


def test_change_summary_empty_prev():
    assert _build_change_summary({}, [{"name": "A", "final_amount": 100}]) == ""


def test_change_summary_new_participant_skipped():
    # 직전에 없던 신규 참여자는 비교 대상에서 제외 (변동 표기 없음)
    summary = _build_change_summary({"A": 100}, [{"name": "Z", "final_amount": 500}])
    assert summary == ""
