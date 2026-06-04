"""target_items 정규화·검증의 결정적 단위 테스트 (LLM 미사용).

정규화(_normalize_target_items)=보정, 검증(safety_check_node)=차단 — 두 책임을
각각 독립적으로 확인한다. docs/target-items-validation.md §5 시나리오 대응.
"""
import os

os.environ.setdefault("UPSTAGE_API_KEY", "test-key-for-import")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from ai.nodes import _normalize_target_items, safety_check_node  # noqa: E402


def _exc(target):
    return {"type": "x", "target_items": target, "discount_rate": 1.0}


# ── 정규화 (P1) — N1~N3 ────────────────────────────────────────────────────

def test_normalize_synonym_maps_sul_to_juryu():
    """N1: '술' → '주류' 동의어 교정."""
    parsed = {
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [{"name": "D", "exceptions": [_exc(["술"])]}],
    }
    out = _normalize_target_items(parsed)
    assert out["participants"][0]["exceptions"][0]["target_items"] == ["주류"]


def test_normalize_partial_substring():
    """N2: '공통' → '공통비' 부분 문자열 교정."""
    parsed = {
        "items": [{"name": "공통비", "amount": 20000}],
        "participants": [{"name": "C", "exceptions": [_exc(["공통"])]}],
    }
    out = _normalize_target_items(parsed)
    assert out["participants"][0]["exceptions"][0]["target_items"] == ["공통비"]


def test_normalize_unmappable_kept():
    """N3: 매핑 불가('택시비')는 원본 유지 → 이후 safety_check가 차단."""
    parsed = {
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [{"name": "D", "exceptions": [_exc(["택시비"])]}],
    }
    out = _normalize_target_items(parsed)
    assert out["participants"][0]["exceptions"][0]["target_items"] == ["택시비"]


def test_normalize_no_items_noop():
    """items가 없으면 정규화하지 않는다 (임의 항목 생성 금지)."""
    parsed = {"participants": [{"name": "A", "exceptions": [_exc(["술"])]}]}
    out = _normalize_target_items(parsed)
    assert out["participants"][0]["exceptions"][0]["target_items"] == ["술"]


# ── 검증 (P0) — V1~V3 ──────────────────────────────────────────────────────

def _pj(target):
    return {
        "total_amount": 80000,
        "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "D", "exceptions": [_exc(target)]},
        ],
    }


def test_safety_unknown_target_blocked():
    """V1: 항목명 오기('택시비') → safety_error로 차단, 잘못된 이름 명시."""
    out = safety_check_node({"parsed_json": _pj(["택시비"])})
    assert out["safety_error"]
    assert "택시비" in out["safety_error"]


def test_safety_valid_target_passes():
    """V2: 정상 입력('주류')은 통과 (safety_error 빈 문자열)."""
    out = safety_check_node({"parsed_json": _pj(["주류"])})
    assert out["safety_error"] == ""


def test_safety_no_items_skips_target_check():
    """V3: items 없는 균등 분배는 본 검증을 건너뛴다 (기존과 동일 통과)."""
    pj = {
        "total_amount": 60000,
        "participants": [
            {"name": "A", "exceptions": []},
            {"name": "B", "exceptions": []},
        ],
    }
    out = safety_check_node({"parsed_json": pj})
    assert out["safety_error"] == ""
