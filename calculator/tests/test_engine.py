import pytest
from calculator.engine import calculate, recalculate


# ── 입력 검증 ──────────────────────────────────────────────────────────────

def test_missing_total_amount_raises():
    with pytest.raises(ValueError):
        calculate({"participants": [{"name": "A", "exceptions": []}]})


def test_missing_participants_raises():
    with pytest.raises(ValueError):
        calculate({"total_amount": 80000})


def test_items_sum_mismatch_raises():
    with pytest.raises(ValueError, match="총액"):
        calculate({
            "total_amount": 80000,
            "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 40000}],  # 70000 ≠ 80000
            "participants": [{"name": "A", "exceptions": []}],
        })


def test_invalid_discount_rate_raises():
    with pytest.raises(ValueError):
        calculate({
            "total_amount": 10000,
            "participants": [
                {"name": "A", "exceptions": []},
                {"name": "B", "exceptions": [
                    {"type": "test", "target_items": ["주류"], "discount_rate": 1.5}
                ]},
            ],
        })


# ── 기본 균등 분배 ─────────────────────────────────────────────────────────

def test_equal_split_no_exceptions():
    result = calculate({
        "total_amount": 80000,
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
            {"name": "D", "exceptions": []},
        ],
    })
    assert result["total_verified"] is True
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert all(amt == 20000 for amt in amounts.values())


# ── 예외 조건 및 재분배 ────────────────────────────────────────────────────

def test_full_exclusion_redistributed():
    """D가 주류 전액 제외 → 감액분이 A, B에게 균등 재분배"""
    result = calculate({
        "total_amount": 60000,
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 30000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": []},
            {"name": "D", "exceptions": [
                {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
            ]},
        ],
    })
    # base = 20,000 / D saves 30000*1.0/3 = 10,000 / A,B each +5,000
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["A"] == 25000
    assert amounts["B"] == 25000
    assert amounts["D"] == 10000
    assert result["total_verified"] is True


def test_partial_discount_redistributed():
    """C가 안주 30% 감액 → 감액분이 나머지에게 재분배"""
    result = calculate({
        "total_amount": 40000,
        "items": [{"name": "안주", "amount": 40000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": [
                {"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.3}
            ]},
            {"name": "D", "exceptions": []},
        ],
    })
    # base = 10,000 / C saves 40000*0.3/4 = 3,000 / A,B,D each +1,000
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["C"] == 7000
    assert amounts["A"] == 11000
    assert result["total_verified"] is True


# ── 최소 부담 하한선 ───────────────────────────────────────────────────────

def test_full_exclusion_exempt_from_floor():
    """완전 제외(discount_rate=1.0)된 참여자는 하한선을 적용하지 않고 0원으로 둔다.

    부분 감액으로 30% 미만이 된 경우에만 하한선이 강제 적용된다.
    유일 항목을 완전히 제외한 B는 실제로 소비한 비용이 없으므로 0원이 맞다.
    """
    result = calculate({
        "total_amount": 10000,
        "items": [{"name": "주류", "amount": 10000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": [
                {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
            ]},
        ],
    })
    # B는 유일 항목을 완전 제외 → 0원 (하한선 면제), A가 전액 부담
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["B"] == 0
    assert amounts["A"] == 10000
    assert "B" not in result.get("floor_applied", [])
    assert result["total_verified"] is True


# ── 시나리오 A ─────────────────────────────────────────────────────────────

INPUT_A = {
    "total_amount": 80000,
    "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
    "participants": [
        {"name": "A", "exceptions": []},
        {"name": "B", "exceptions": []},
        {"name": "C", "exceptions": [
            {"type": "늦은 도착", "target_items": ["안주"], "discount_rate": 0.3}
        ]},
        {"name": "D", "exceptions": [
            {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
        ]},
    ],
}


def test_scenario_a_total_verified():
    result = calculate(INPUT_A)
    assert result["total_verified"] is True
    assert sum(p["final_amount"] for p in result["participants"]) == 80000


def test_scenario_a_amounts():
    result = calculate(INPUT_A)
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    # A,B 예외 없음 → 동일
    assert amounts["A"] == amounts["B"]
    # D(주류 제외) < A / C(안주 감액) < A
    assert amounts["D"] < amounts["A"]
    assert amounts["C"] < amounts["A"]


def test_scenario_a_exact_amounts():
    result = calculate(INPUT_A)
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    # A,B: 23,750 / C: 18,750 / D: 13,750
    assert amounts["A"] == 23750
    assert amounts["B"] == 23750
    assert amounts["C"] == 18750
    assert amounts["D"] == 13750


# ── 시나리오 B (복합 예외) ─────────────────────────────────────────────────

INPUT_B = {
    "total_amount": 120000,
    "items": [
        {"name": "주류", "amount": 50000},
        {"name": "안주", "amount": 50000},
        {"name": "공통비", "amount": 20000},
    ],
    "participants": [
        {"name": "A", "exceptions": []},
        {"name": "B", "exceptions": []},
        {"name": "C", "exceptions": []},
        {"name": "D", "exceptions": [
            {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
        ]},
        {"name": "E", "exceptions": [
            {"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.5}
        ]},
    ],
}


def test_scenario_b_total_verified():
    result = calculate(INPUT_B)
    assert result["total_verified"] is True
    assert sum(p["final_amount"] for p in result["participants"]) == 120000


# ── 시나리오 C (피드백 재계산) ─────────────────────────────────────────────

def test_scenario_c_total_verified():
    feedback = {
        "name": "D",
        "additional_exception": {"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.5},
    }
    result = recalculate(INPUT_B, feedback)
    assert result["total_verified"] is True


def test_scenario_c_d_pays_less_than_b():
    """피드백 추가 후 D의 부담이 줄어야 한다"""
    feedback = {
        "name": "D",
        "additional_exception": {"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.5},
    }
    result_b = calculate(INPUT_B)
    result_c = recalculate(INPUT_B, feedback)
    d_b = next(p["final_amount"] for p in result_b["participants"] if p["name"] == "D")
    d_c = next(p["final_amount"] for p in result_c["participants"] if p["name"] == "D")
    assert d_c < d_b


# ── SPONSOR 레이어 (선결제·지원금·송금정산) ────────────────────────────────

# docs/sponsor.md §8 워크드 예시: 5명/120,000, D 주류 미섭취(1.0), E 안주 소량(0.7), A 전액 선결제
INPUT_SPONSOR = {
    "total_amount": 120000,
    "items": [
        {"name": "주류", "amount": 50000},
        {"name": "안주", "amount": 50000},
        {"name": "공통비", "amount": 20000},
    ],
    "participants": [
        {"name": "A", "prepaid": 120000, "exceptions": []},
        {"name": "B", "exceptions": []},
        {"name": "C", "exceptions": []},
        {"name": "D", "exceptions": [
            {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
        ]},
        {"name": "E", "exceptions": [
            {"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.7}
        ]},
    ],
}


def test_no_sponsor_omits_settlement():
    """불변식 1: prepaid/subsidy 없으면 settlement·net_amount 미출력 (하위호환)."""
    result = calculate(INPUT_B)
    assert "settlement" not in result
    assert all("net_amount" not in p for p in result["participants"])


def test_sponsor_burden_amounts_match_doc():
    """§8 부담액 기준값: A/B/C=28,250, D=15,750, E=19,500."""
    result = calculate(INPUT_SPONSOR)
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["A"] == 28250
    assert amounts["B"] == 28250
    assert amounts["C"] == 28250
    assert amounts["D"] == 15750
    assert amounts["E"] == 19500
    assert result["total_verified"] is True
    assert sum(amounts.values()) == 120000


def test_sponsor_net_and_balanced():
    """§8 순정산: A=−91,750, sum(net)==0, balanced."""
    result = calculate(INPUT_SPONSOR)
    nets = {p["name"]: p["net_amount"] for p in result["participants"]}
    assert nets["A"] == -91750
    assert sum(nets.values()) == 0
    settlement = result["settlement"]
    assert settlement["balanced"] is True
    assert settlement["unsettled"] == []


def test_sponsor_transfers_all_to_payer():
    """§8 송금: B/C→A 28,250, D→A 15,750, E→A 19,500 (총 91,750)."""
    result = calculate(INPUT_SPONSOR)
    transfers = result["settlement"]["transfers"]
    # 모든 송금은 A(전액 선결제자)에게 향한다
    assert all(t["to"] == "A" for t in transfers)
    by_payer = {t["from"]: t["amount"] for t in transfers}
    assert by_payer["B"] == 28250
    assert by_payer["C"] == 28250
    assert by_payer["D"] == 15750
    assert by_payer["E"] == 19500
    assert sum(t["amount"] for t in transfers) == 91750


def test_subsidy_reduces_total_burden():
    """지원금 3만원 → 부담 총합 == total − subsidy, 총액 검증 통과."""
    parsed = {
        "total_amount": 120000,
        "subsidy": 30000,
        "items": [
            {"name": "주류", "amount": 50000},
            {"name": "안주", "amount": 50000},
            {"name": "공통비", "amount": 20000},
        ],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
        ],
    }
    result = calculate(parsed)
    assert sum(p["final_amount"] for p in result["participants"]) == 90000
    assert result["total_verified"] is True
    assert result["settlement"]["net_total"] == 90000


def test_prepaid_shortfall_unsettled():
    """선결제 미달(A 5만만 선결제) → unsettled 존재, balanced False (에러 아님)."""
    parsed = {
        "total_amount": 60000,
        "items": [{"name": "식사", "amount": 60000}],
        "participants": [
            {"name": "A", "prepaid": 50000, "exceptions": []},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
        ],
    }
    result = calculate(parsed)
    # 각 부담 20,000. A net=20,000−50,000=−30,000(받을), B/C net=+20,000
    settlement = result["settlement"]
    assert settlement["balanced"] is False
    assert settlement["unsettled"]  # 남은 현장 결제분 존재
    unsettled_total = sum(u["amount"] for u in settlement["unsettled"])
    assert unsettled_total == 10000  # B/C 40,000 중 A가 30,000만 회수 → 10,000 미정산


def test_prepaid_over_net_total_raises():
    """선결제 합이 정산 대상액 초과 → ValueError."""
    with pytest.raises(ValueError, match="선결제"):
        calculate({
            "total_amount": 60000,
            "participants": [
                {"name": "A", "prepaid": 50000, "exceptions": []},
                {"name": "B", "prepaid": 20000, "exceptions": []},
            ],
        })


def test_subsidy_over_total_raises():
    """지원금이 총액 이상 → ValueError."""
    with pytest.raises(ValueError, match="지원금"):
        calculate({
            "total_amount": 60000,
            "subsidy": 60000,
            "participants": [{"name": "A", "exceptions": []}],
        })


# ── 최종 금액 직접 지정 (fixed_amount / 피드백 Direct Override) ──────────────

def test_fixed_amount_pins_and_redistributes():
    """A를 10,000원으로 고정 → 차액이 나머지에게 비례 재분배, 총합 보존."""
    result = calculate({
        "total_amount": 60000,
        "participants": [
            {"name": "A", "exceptions": [], "fixed_amount": 10000},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
        ],
    })
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["A"] == 10000          # 고정값 정확히 보존
    assert amounts["B"] == 25000          # (20000 → 20000×1.25) 비례 흡수
    assert amounts["C"] == 25000
    assert sum(amounts.values()) == 60000
    assert result["total_verified"] is True


def test_fixed_amount_skips_floor():
    """고정값이 30% 하한선 미만이어도 사용자 지정이 우선 → 하한선 미적용."""
    result = calculate({
        "total_amount": 60000,
        "participants": [
            {"name": "A", "exceptions": [], "fixed_amount": 1000},  # 하한선(6,000) 미만
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
        ],
    })
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["A"] == 1000           # 하한선으로 끌어올리지 않음
    assert result["floor_applied"] == []
    assert sum(amounts.values()) == 60000


def test_fixed_amount_with_items_preserves_total():
    """항목이 있어도 고정값 보존 + 총액 검증 통과."""
    result = calculate({
        "total_amount": 80000,
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [
            {"name": "A", "exceptions": [], "fixed_amount": 10000},
            {"name": "B", "exceptions": []},
            {"name": "C", "exceptions": []},
            {"name": "D", "exceptions": []},
        ],
    })
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts["A"] == 10000
    assert sum(amounts.values()) == 80000
    assert result["total_verified"] is True


def test_all_fixed_must_match_total():
    """전원 고정값 합이 정산 대상액과 같으면 그대로 통과."""
    result = calculate({
        "total_amount": 60000,
        "participants": [
            {"name": "A", "exceptions": [], "fixed_amount": 30000},
            {"name": "B", "exceptions": [], "fixed_amount": 30000},
        ],
    })
    amounts = {p["name"]: p["final_amount"] for p in result["participants"]}
    assert amounts == {"A": 30000, "B": 30000}
    assert result["total_verified"] is True


def test_fixed_amount_over_net_total_raises():
    """고정값이 정산 대상액 초과 → ValueError."""
    with pytest.raises(ValueError, match="고정 금액"):
        calculate({
            "total_amount": 60000,
            "participants": [
                {"name": "A", "exceptions": [], "fixed_amount": 70000},
                {"name": "B", "exceptions": []},
            ],
        })


def test_all_fixed_mismatch_raises():
    """전원 고정인데 합이 총액과 불일치 → ValueError."""
    with pytest.raises(ValueError, match="고정 금액"):
        calculate({
            "total_amount": 60000,
            "participants": [
                {"name": "A", "exceptions": [], "fixed_amount": 20000},
                {"name": "B", "exceptions": [], "fixed_amount": 20000},
            ],
        })


# ── target_items 방어적 가드 (이름 불일치 silent failure 차단) ──────────────

def test_unknown_target_item_raises():
    """items에 없는 target_items('택시비') → 엔진이 ValueError로 차단."""
    with pytest.raises(ValueError, match="target_items"):
        calculate({
            "total_amount": 80000,
            "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
            "participants": [
                {"name": "A", "exceptions": []},
                {"name": "D", "exceptions": [
                    {"type": "술 미섭취", "target_items": ["택시비"], "discount_rate": 1.0}
                ]},
            ],
        })


def test_valid_target_item_passes():
    """정상 항목명은 통과 (회귀 없음)."""
    result = calculate({
        "total_amount": 80000,
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "D", "exceptions": [
                {"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}
            ]},
        ],
    })
    assert result["total_verified"] is True
