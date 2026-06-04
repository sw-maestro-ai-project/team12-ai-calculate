import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI
from langsmith.wrappers import wrap_openai

from calculator.engine import calculate
from ai.state import SettlementState

load_dotenv()

# wrap_openai: LangSmith 트레이싱 활성화 시 각 LLM 호출을 자동 기록한다.
# (LANGSMITH_TRACING=false면 오버헤드 없이 일반 OpenAI 클라이언트처럼 동작)
_client = wrap_openai(OpenAI(
    api_key=os.getenv("UPSTAGE_API_KEY"),
    base_url="https://api.upstage.ai/v1",
))
_MODEL = "solar-pro"

_INPUT_PARSING_SYSTEM = """당신은 정산 데이터 파서입니다.
산술 계산은 수행하지 말고, 다음을 수행하라:
1. 참여자, 총 금액, 비용 항목, 예외 조건, 선결제 정보를 JSON으로 추출하라.
2. 각 예외 조건을 아래 규칙에 따라 반드시 정확히 분류하라.

   [감액 조건] 실제로 소비를 덜 한 경우 → discount_rate 사용 (본인 부담 감소)
   감액 수치 기준표 (반드시 이 표를 따를 것):
   | 사용자 표현                       | discount_rate | 적용 범위   |
   | 전혀 안 먹음 / 미섭취 / 안 마심   | 1.0           | 해당 항목   |
   | 거의 안 먹음 / 한 입만            | 0.7           | 해당 항목   |
   | 조금 먹음 / 적게 먹음 / 소량 섭취 | 0.5           | 해당 항목   |
   | 반만 먹음 / 절반 정도             | 0.5           | 해당 항목   |
   | 중도 귀가 (절반 이상 자리 비움)   | 0.5           | 모든 항목   |
   | 조금 있다 감 / 잠깐만 있었음      | 0.3           | 모든 항목   |
   위 표에 해당하지 않는 모호한 표현은 discount_rate: null로 설정하라.

   [할증 조건] 패널티를 부과하는 경우 (본인 부담 증가)
   - 지각/늦은 도착:
     - 금액 명시 → surcharge_amount 사용 (surcharge_rate 생략)
       예) "지각비 5000원" → surcharge_amount: 5000
     - 비율 명시 → surcharge_rate 사용 (surcharge_amount 생략)
       예) "지각자는 20% 더 내기로 했어" → surcharge_rate: 0.2
     - 비율도 금액도 미명시 → surcharge_rate: null
       예) "C는 늦게 왔어" → surcharge_rate: null

   [선결제 조건] (감액/할증이 아니다 — 이미 누가 가게에 낸 돈)
   - "A가 먼저 냈어 / A가 카드로 긁었어 / A가 쐈어 / A가 계산했어 / A가 다 결제했어"
     → 해당 참여자에 prepaid: 금액 을 넣는다.
   - 금액이 명시되면 그 값, "다/전부 냈어"처럼 전액이면 total_amount 값을 넣는다.
   - 금액이 불명확하면 prepaid를 넣지 않는다(생략).
   - 선결제는 participants[].exceptions에 넣지 말 것. prepaid 필드로만 표현한다.

   [지원금 조건] (외부에서 들어와 총액을 깎는 돈)
   - "동아리에서 지원받았어 / 회비로 충당 / 협찬 N만원 / 지원금 N만원"
     → 최상위 subsidy: 금액 을 넣는다. 금액 불명확하면 생략한다.

   [최종 금액 지정] (특정인의 최종 부담액을 콕 집어 못박는 경우)
   - "A는 2만원만 내 / A는 20000원으로 고정"
     → 해당 참여자에 fixed_amount: 금액 을 넣는다. discount_rate로 바꾸지 말 것. 불명확하면 생략한다.

   주의: 받을 사람·송금액·정산 결과는 절대 계산하지 말 것. 추출만 한다.

반드시 유효한 JSON만 반환하라. 설명 없이 JSON만 출력하라.

출력 형식 예시 (지각비 5000원인 C, 술 미섭취 D):
{
  "total_amount": 80000,
  "items": [{"name": "주류", "amount": 30000}, {"name": "안주", "amount": 50000}],
  "participants": [
    {"name": "A", "exceptions": []},
    {"name": "B", "exceptions": []},
    {"name": "C", "exceptions": [{"type": "늦은 도착", "target_items": ["주류", "안주"], "surcharge_amount": 5000}]},
    {"name": "D", "exceptions": [{"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}]}
  ]
}

출력 형식 예시 (지각 20%인 C, 술 미섭취 D, 안주 거의 안 먹은 E):
{
  "total_amount": 120000,
  "items": [{"name": "주류", "amount": 50000}, {"name": "안주", "amount": 50000}, {"name": "공통비", "amount": 20000}],
  "participants": [
    {"name": "A", "exceptions": []},
    {"name": "B", "exceptions": []},
    {"name": "C", "exceptions": [{"type": "늦은 도착", "target_items": ["주류", "안주", "공통비"], "surcharge_rate": 0.2}]},
    {"name": "D", "exceptions": [{"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}]},
    {"name": "E", "exceptions": [{"type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.7}]}
  ]
}

출력 형식 예시 (A가 전액 선결제, 동아리 지원금 2만원, 술 미섭취 D):
{
  "total_amount": 120000,
  "subsidy": 20000,
  "items": [{"name": "주류", "amount": 50000}, {"name": "안주", "amount": 50000}, {"name": "공통비", "amount": 20000}],
  "participants": [
    {"name": "A", "prepaid": 120000, "exceptions": []},
    {"name": "B", "exceptions": []},
    {"name": "C", "exceptions": []},
    {"name": "D", "exceptions": [{"type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0}]},
    {"name": "E", "exceptions": []}
  ]
}"""

# 전략 분기는 route_request_node에서 결정적 코드로 판정한다 (LLM 미사용).
# SIMPLE / EXCEPTION / SPONSOR — parsed_json의 prepaid·subsidy·exceptions 유무로 확정.

_SHARE_MESSAGE_SYSTEM = """아래 [최종 정산 금액] 목록만을 사용하여 카카오톡에 붙여넣을 공유 메시지를 작성하라.

[허용]
- 각 참여자 이름과 최종 금액을 명확히 표기하라.
- 음수 금액은 해당 참여자가 받을 금액임을 표기하라. (예: -15,000원 → "A ← 15,000원 수령")
- 이모지를 적절히 사용하고 친근하고 가벼운 톤으로 3~8줄 이내로 작성하라.

[절대 금지 — 위반 시 응답 전체가 무효]
- 할인율, 지각비, 예외 조건, 적용 비율 등 계산 근거나 이유를 일절 언급하지 말라.
- "술을 안 드셔서", "지각하셔서", "소량 섭취" 등 조건 설명 금지.
- [최종 정산 금액] 목록에 없는 수치를 임의로 추가하거나 계산하지 말라.
- 산술 계산을 절대 수행하지 말라. 금액 합산, 차감, 퍼센트 계산 등 모든 연산 금지. (예: 20,000 + 4,000 = 27,000 같은 오류 발생 원인)
- 왜 금액이 다른지 설명하지 말라. 최종 금액 나열만 허용한다."""

_SHARE_MESSAGE_SPONSOR_SYSTEM = """아래 [입력]에 주어진 줄만 사용해 카카오톡 공유용 정산 메시지를 작성하라.

규칙:
- [입력]에 있는 줄만 반영하라. 인사말과 이모지만 덧붙이고, 새 항목·숫자를 만들지 말라.
- [입력]에 없는 항목은 추가하지 말라. 숫자를 새로 계산하거나 바꾸지 말라.
- 송금 목록은 "보내는사람 → 받는사람: 금액" 형식으로 그대로 나열하라.
- [미정산 잔액]이 [입력]에 있으면 "현장 결제분 N원은 별도" 한 줄로 안내하라. 없으면 언급 금지.
- **메시지는 단 하나만 출력하라.** 여러 버전, "최종 답변", 규칙·주석(※ 괄호 설명 등)을 출력하지 말라.

[입력] 예시 1:
[선결제]
  A: 120,000원 선결제
[송금 목록]
  B → A: 28,250원
  C → A: 28,250원

출력 1:
💳 정산 안내
A님이 120,000원 먼저 결제했어요! 아래대로 송금 부탁드려요 🙏
- B → A: 28,250원
- C → A: 28,250원

[입력] 예시 2:
[선결제]
  A: 50,000원 선결제
[송금 목록]
  B → A: 20,000원
  C → A: 10,000원
[미정산 잔액]
  10,000원 (현장 결제분)

출력 2:
💳 정산 안내
A님이 50,000원 먼저 결제했어요! 아래대로 송금 부탁드려요 🙏
- B → A: 20,000원
- C → A: 10,000원
※ 현장 결제분 10,000원은 별도입니다."""

_FEEDBACK_PARSING_SYSTEM = """기존 정산 정보에 피드백을 반영하라.
규칙:
- 기존 parsed_json을 임의로 덮어쓰지 말라
- 사용자가 새로 말한 조건만 추가 또는 수정하라. 언급하지 않은 참여자의 exceptions는 절대 변경하지 말라.
- 할증/추가 부담 조건(지각, 늦은 도착 등)은 반드시 명시적으로 언급된 참여자에게만 surcharge_rate를 할당하라.
  예) "C가 20% 더 낸다" → C에게만 surcharge_rate: 0.2 할당. A, B, D는 건드리지 않는다.
- 산술 계산은 수행하지 말고, 조건과 rate 변경 사항만 추출하라
- 감액 조건(소비 덜 함)은 discount_rate, 할증 조건(지각 등 패널티)은 surcharge_rate(비율) 또는 surcharge_amount(고정금액) 사용

감액 수치 기준표 (반드시 이 표를 따를 것):
| 사용자 표현                       | discount_rate |
| 전혀 안 먹음 / 미섭취 / 안 마심   | 1.0           |
| 거의 안 먹음 / 한 입만            | 0.7           |
| 조금 먹음 / 적게 먹음 / 소량 섭취 | 0.5           |
| 반만 먹음 / 절반 정도             | 0.5           |
위 표에 해당하지 않는 모호한 표현은 discount_rate: null로 설정하라.

- 금액 명시 시 surcharge_amount, 비율 명시 시 surcharge_rate (둘을 동시에 쓰지 말라)
- 모든 rate는 0.0~1.0 범위, surcharge_amount는 0 이상 정수로 결정하라

선결제(prepaid)·지원금(subsidy) 관련 규칙:
- 새 선결제 언급("A가 먼저 냈어") → 해당 참여자에 prepaid 추가. 언급 없으면 기존 prepaid 유지.
- "A가 아니라 B가 냈어" → 기존 prepaid를 A에서 제거하고 B에 옮긴다.
- "5만원이 아니라 6만원" → 기존 prepaid 금액만 수정한다.
- 지원금 언급 없으면 기존 subsidy를 그대로 유지한다.
- 선결제/지원금 수정 때문에 기존 participants/items/exceptions를 삭제하지 말 것.

최종 금액 직접 지정(fixed_amount) 규칙:
- "A는 2만원만 내게 해줘 / A는 20000원으로 고정 / B는 만원만 내" 처럼 특정인의 최종 부담액을
  콕 집어 지정하면 → 해당 참여자 객체에 직접 "fixed_amount": 금액 키를 넣는다.
  반드시 participants 원소의 최상위 키로 넣어라. exceptions 배열 안에 넣지 말 것.
  올바른 예) {"name": "A", "fixed_amount": 10000, "exceptions": []}
  잘못된 예) {"name": "A", "exceptions": [{"type": "fixed_amount", "amount": 10000}]}  ← 금지
- fixed_amount는 그 사람의 '최종 부담액'을 강제한다. 나머지 사람에게 얼마가 가는지는 계산하지 말 것.
- discount_rate로 바꾸지 말 것. 지정 해제("A 고정 풀어줘") 언급 시 fixed_amount 제거, 언급 없으면 유지.

이력 일관성(중요):
- 이전 피드백과 현재 피드백이 충돌하면, 가장 최근(현재) 지시를 우선한다.
  예) 앞에서 "A 1만원 고정"이라 했고 지금 "A는 그냥 똑같이 내"라고 하면 → A의 fixed_amount 제거.
- 현재 피드백에서 언급되지 않은 참여자/조건은 기존 값을 그대로 보존한다 (임의 변경 금지).

- 수정된 전체 parsed_json을 반환하라. 설명 없이 JSON만 출력하라."""


_FEEDBACK_INTENT_SYSTEM = """당신은 정산 피드백의 '의도'를 분류하는 분류기다.
사용자가 이미 한 번 정산 결과를 받은 뒤 보낸 추가 메시지의 의도를 아래 셋 중 하나로만 판단하라.

- "modify_exception": 기존 계산에 조건을 추가/수정하는 요청
  예) "A가 10% 더 낸다고 했어", "D는 안주도 적게 먹었어", "C 지각비 5000원",
      "A는 2만원만 내게 해줘"(최종 금액 지정도 수정 요청이다)
- "reset": 기존 계산과 무관하게 완전히 새로운 정산을 시작하려는 요청
  예) "다시 처음부터 할게", "새로운 정산이야", "방금 건 취소하고", "이번엔 3명이서 5만원인데"
- "complaint": 결과에 불만/의문을 표현하지만 '구체적인 수정 조건'이 없는 경우
  예) "계산이 이상한 것 같아", "왜 이렇게 나왔어?", "이거 맞아?", "다시 계산해봐"

판단 원칙:
- 구체적 숫자/조건(누가, 얼마, 몇 %)이 있으면 modify_exception 또는 reset이다. complaint가 아니다.
- 새 인원·새 총액으로 처음부터 다시 하려는 신호가 강하면 reset이다.
- 불만만 있고 무엇을 바꿔야 할지 정보가 없으면 complaint다.

설명 없이 JSON만 출력하라. 형식:
{"intent": "modify_exception"}"""


def _call_llm(system: str, user: str, temperature: float = 0, *, tag: str = "") -> str:
    response = _client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        langsmith_extra={"name": tag or "llm_call"},  # LangSmith run 이름으로 노출
    )
    return response.choices[0].message.content


def _extract_json(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1)
    return json.loads(text.strip())


# 동의어 → 실제 항목명 후보 (부분 문자열로 매칭). 항목이 실제로 존재할 때만 매핑한다.
ITEM_SYNONYMS = {
    "주류": ["술", "맥주", "소주", "주류", "음주"],
    "안주": ["안주", "음식", "고기", "food", "메뉴"],
}


def _normalize_target_items(parsed: dict) -> dict:
    """예외 조건의 target_items 이름을 실제 항목명으로 교정한다 (보정 단계).

    LLM이 "술/고기"처럼 항목명과 다르게 써도 실제 items 이름으로 맞춰, 감액/할증이
    조용히 누락되는 것을 막는다. 매핑에 실패하면 원본을 그대로 두어 이후
    safety_check_node가 차단하게 한다. 없는 항목을 새로 만들어내지 않는다.
    """
    item_names = [i["name"] for i in parsed.get("items", [])]
    if not item_names:
        return parsed

    def _map_one(token: str) -> str:
        if token in item_names:
            return token
        # 1) 동의어 사전으로 실제 항목명 추정 (항목명이 실제로 존재할 때만)
        for canonical, syns in ITEM_SYNONYMS.items():
            if canonical in item_names and any(s in token or token in s for s in syns):
                return canonical
        # 2) 부분 문자열 일치(예: "주류값" → "주류", "공통" → "공통비")
        for name in item_names:
            if name in token or token in name:
                return name
        return token  # 매핑 실패 → 원본 유지 (이후 safety_check가 차단)

    for p in parsed.get("participants", []):
        for exc in p.get("exceptions", []):
            if exc.get("target_items"):
                exc["target_items"] = [_map_one(t) for t in exc["target_items"]]
    return parsed


def _post_validate_exceptions(parsed: dict) -> dict:
    """LLM 분류 오류를 코드 레벨에서 보정한다.

    1. 지각/늦은 도착 → surcharge 교정 (discount_rate 잘못 분류 시 전환)
    2. discount_rate null → 타입 키워드로 rate 추론 교정
    3. target_items 동의어 정규화 (술→주류 등)
    """
    SURCHARGE_KEYWORDS = {"지각", "늦은 도착", "늦게", "late", "지각비", "늦음", "늦은"}
    # 감액 키워드 → discount_rate 매핑 (모호한 null 보정용)
    DISCOUNT_NULL_CORRECTION = [
        ({"미섭취", "안 마심", "안 먹음", "전혀"}, 1.0),
        ({"거의 안", "한 입만"}, 0.7),
        ({"소량", "조금 먹음", "적게 먹음", "절반", "반만", "중도 귀가"}, 0.5),
        ({"잠깐", "조금 있다"}, 0.3),
    ]

    all_item_names = [item["name"] for item in parsed.get("items", [])]

    for p in parsed.get("participants", []):
        # LLM이 fixed_amount를 exceptions 안에 잘못 넣은 경우 참여자 레벨로 끌어올린다.
        _hoist_fixed_amount(p)
        for exc in p.get("exceptions", []):
            exc_type = exc.get("type", "")
            if any(kw in exc_type for kw in SURCHARGE_KEYWORDS):
                # discount_rate → surcharge_rate 강제 교정 (surcharge_amount 없는 경우)
                if "discount_rate" in exc and "surcharge_amount" not in exc:
                    exc["surcharge_rate"] = exc.pop("discount_rate")
                # surcharge_rate도 surcharge_amount도 없으면 null 주입
                if "surcharge_rate" not in exc and "surcharge_amount" not in exc:
                    exc["surcharge_rate"] = None
                # target_items를 모든 항목으로 교정
                if all_item_names:
                    exc["target_items"] = all_item_names
            elif "discount_rate" in exc and exc["discount_rate"] is None:
                # discount_rate null → 타입 키워드로 rate 추론
                for keywords, rate in DISCOUNT_NULL_CORRECTION:
                    if any(kw in exc_type for kw in keywords):
                        exc["discount_rate"] = rate
                        break

        # 할증 예외 중복 병합 (피드백 시 LLM이 기존 null 할증을 갱신하지 않고
        # 새 할증을 덧붙이는 경우 방어 — 가장 구체적인 값 하나만 남긴다)
        _merge_surcharge_exceptions(p)

    # target_items 동의어/오기 정규화 (검증 전 보정 — 실제 항목명으로 교정)
    return _normalize_target_items(parsed)


def _hoist_fixed_amount(participant: dict) -> None:
    """LLM이 fixed_amount(최종 금액 지정)를 exceptions 배열 안에 잘못 넣은 경우
    이를 참여자 레벨 `fixed_amount` 필드로 끌어올린다 (엔진은 이 필드만 인식한다).

    교정 대상 패턴:
      - {"fixed_amount": 10000}                  → participant["fixed_amount"] = 10000
      - {"type": "fixed_amount", "amount": 10000} → participant["fixed_amount"] = 10000
    """
    excs = participant.get("exceptions", [])
    remaining = []
    for e in excs:
        val = None
        if "fixed_amount" in e:
            val = e["fixed_amount"]
        elif e.get("type") == "fixed_amount" and e.get("amount") is not None:
            val = e["amount"]
        if val is not None:
            participant["fixed_amount"] = val
        else:
            remaining.append(e)
    participant["exceptions"] = remaining


def _merge_surcharge_exceptions(participant: dict) -> None:
    """한 참여자의 할증(지각 등) 예외가 둘 이상이면 가장 구체적인 것 하나로 병합한다.

    우선순위: surcharge_amount(고정금액) > surcharge_rate(비율) > null.
    감액(discount) 예외는 항목별로 여럿 존재할 수 있으므로 건드리지 않는다.
    """
    excs = participant.get("exceptions", [])
    surcharge = [e for e in excs if "surcharge_rate" in e or "surcharge_amount" in e]
    if len(surcharge) <= 1:
        return

    def _score(e: dict) -> int:
        if e.get("surcharge_amount") is not None:
            return 3
        if e.get("surcharge_rate") is not None:
            return 2
        return 1  # null 할증

    best = max(surcharge, key=_score)
    others = [e for e in excs if e is not best and "surcharge_rate" not in e and "surcharge_amount" not in e]
    participant["exceptions"] = others + [best]


# ── 피드백 루프용 결정적 헬퍼 (LLM 미사용 — 단위 테스트 가능) ──────────────

def _participant_overlap(old_names: list, new_names: list) -> float:
    """두 참여자 명단의 자카드 유사도(0.0~1.0). 둘 중 하나가 비면 0.0."""
    old, new = set(old_names), set(new_names)
    if not old or not new:
        return 0.0
    return len(old & new) / len(old | new)


def _build_complaint_clarification(cr: dict) -> str:
    """complaint(불만) 의도일 때, 직전 결과에서 사용자가 의아해할 만한 규칙을
    먼저 짚어 되묻는 메시지를 결정적으로 생성한다 (자가 진단, LLM 미사용).
    """
    fallback = (
        "계산 결과에서 어떤 부분이 이상한지 알려주시면 바로 반영할게요.\n"
        "예) 'A 금액이 너무 높아' 또는 'D가 술을 조금 마셨는데 안 반영됐어'"
    )
    if not cr:
        return fallback

    hints = []
    floor = cr.get("floor_applied") or []
    if floor:
        hints.append(
            f"{', '.join(floor)}님은 최소 부담 하한선(균등액의 30%)에 걸려 금액이 올라갔어요. 이 부분이 의아하셨을까요?"
        )
    surcharge_logs = cr.get("surcharge_logs") or {}
    if surcharge_logs:
        hints.append(
            f"{', '.join(surcharge_logs.keys())}님께 지각/할증이 더해졌어요. 할증 조건이 잘못됐을까요?"
        )
    discount_logs = cr.get("discount_logs") or {}
    multi = [n for n, logs in discount_logs.items() if len(logs) > 1]
    if multi:
        hints.append(
            f"{', '.join(multi)}님은 여러 항목에서 감액이 겹쳐 적용됐어요. 이 부분일까요?"
        )

    if not hints:
        return fallback
    return (
        "혹시 이런 부분이 궁금하셨나요?\n- "
        + "\n- ".join(hints)
        + "\n구체적으로 어디가 잘못됐는지 알려주시면 바로 고칠게요."
    )


def _build_change_summary(prev_amounts: dict, participants_out: list) -> str:
    """직전 결과 대비 최종 금액 변동을 결정적으로 요약한다 (변경 사항 하이라이트).

    prev_amounts: {이름: 직전 최종금액}, participants_out: 새 calculation_result["participants"].
    변동이 없으면 빈 문자열을 반환한다.
    """
    if not prev_amounts:
        return ""
    lines = []
    for p in participants_out:
        name = p["name"]
        new_amt = p["final_amount"]
        old_amt = prev_amounts.get(name)
        if old_amt is None or old_amt == new_amt:
            continue
        delta = new_amt - old_amt
        sign = "+" if delta > 0 else "−"
        lines.append(f"{name}: {old_amt:,}원 → {new_amt:,}원 ({sign}{abs(delta):,}원)")
    return "\n".join(lines)


def input_parsing_node(state: SettlementState) -> dict:
    content = _call_llm(_INPUT_PARSING_SYSTEM, state["raw_input"], tag="INPUT_PARSING")
    parsed = _extract_json(content)
    parsed = _post_validate_exceptions(parsed)
    return {"parsed_json": parsed}


def safety_check_node(state: SettlementState) -> dict:
    pj = state.get("parsed_json", {})

    def _exit(error: str) -> dict:
        return {"safety_error": error}

    if not pj.get("total_amount") or not pj.get("participants"):
        return _exit("total_amount 또는 participants 정보가 누락되었습니다.")

    # ── 중복 참여자 감지 ──
    names = [p["name"] for p in pj.get("participants", [])]
    if len(names) != len(set(names)):
        dups = [n for n in set(names) if names.count(n) > 1]
        return _exit(f"중복된 참여자 이름이 있습니다: {', '.join(dups)}")

    # ── 총액 vs 항목 합계 검증 ──
    items_sum = sum(i["amount"] for i in pj.get("items", []))
    if items_sum and abs(items_sum - pj["total_amount"]) > 1:
        return _exit(f"항목 합계({items_sum:,}원)가 총액({pj['total_amount']:,}원)과 일치하지 않습니다.")

    # ── items 없는데 예외 조건에 target_items 지정된 경우 ──
    if not pj.get("items"):
        for p in pj.get("participants", []):
            for exc in p.get("exceptions", []):
                if exc.get("target_items"):
                    return _exit(
                        "비용 항목(주류, 안주 등)이 입력되지 않았는데 예외 조건에 항목이 지정되어 있습니다.\n"
                        "항목별 금액을 함께 알려주세요. 예) \"주류 3만원 / 안주 5만원\""
                    )

    # ── target_items 이름이 실제 items에 존재하는지 검증 (silent failure 차단) ──
    # 정규화(_normalize_target_items)로도 못 푼 항목명은 계산으로 넘기지 않고 되묻는다.
    item_names = {i["name"] for i in pj.get("items", [])}
    if item_names:
        unknown = []
        for p in pj.get("participants", []):
            for exc in p.get("exceptions", []):
                for t in exc.get("target_items", []):
                    if t not in item_names:
                        unknown.append((p["name"], t))
        if unknown:
            detail = ", ".join(f"{name}의 '{t}'" for name, t in unknown)
            names_str = ", ".join(sorted(item_names))
            return _exit(
                f"예외 조건의 항목 이름이 입력한 비용 항목과 일치하지 않습니다: {detail}\n"
                f"입력된 항목: {names_str}\n"
                "어느 항목에 대한 조건인지 정확한 항목명으로 다시 알려주세요."
            )

    # ── discount_rate null 감지 ──
    null_discount_names = []
    for p in pj.get("participants", []):
        for exc in p.get("exceptions", []):
            if "discount_rate" in exc and exc["discount_rate"] is None:
                null_discount_names.append(p["name"])
    if null_discount_names:
        names_str = ", ".join(dict.fromkeys(null_discount_names))
        return _exit(
            f"{names_str}의 감액 정도가 명확하지 않습니다.\n"
            "구체적으로 어느 정도 먹었는지 알려주세요.\n"
            "예) \"거의 안 먹었어\" / \"절반 정도 먹었어\" / \"조금만 먹었어\""
        )

    # ── 할증 비율/금액 미지정 감지 ──
    missing_rate_names = []
    for p in pj.get("participants", []):
        for exc in p.get("exceptions", []):
            rate_null = "surcharge_rate" in exc and exc["surcharge_rate"] is None
            amount_null = "surcharge_amount" in exc and exc["surcharge_amount"] is None
            if rate_null and "surcharge_amount" not in exc:
                missing_rate_names.append(p["name"])
            elif amount_null:
                missing_rate_names.append(p["name"])

    if missing_rate_names:
        names_str = ", ".join(dict.fromkeys(missing_rate_names))
        return _exit(
            f"{names_str}에 대한 할증(지각 등) 비율 또는 금액이 지정되지 않았습니다.\n"
            "예) \"지각자는 20% 더 내기로 했어\" 또는 \"지각비 5000원\" 형태로 알려주세요."
        )

    # ── 선결제(prepaid)·지원금(subsidy) 검증 (SPONSOR 레이어) ──
    total_amount = pj.get("total_amount", 0)
    subsidy = pj.get("subsidy", 0) or 0
    if subsidy < 0 or subsidy >= total_amount:
        return _exit(
            "지원금 금액이 올바르지 않습니다 (총액 미만의 양수여야 합니다).\n"
            "예) \"동아리에서 2만원 지원받았어\""
        )
    net_total = total_amount - subsidy
    prepaid_sum = sum(p.get("prepaid", 0) or 0 for p in pj.get("participants", []))
    if prepaid_sum > net_total:
        return _exit(
            f"선결제 합({prepaid_sum:,}원)이 정산 대상액({net_total:,}원)을 초과합니다.\n"
            "선결제 금액을 다시 확인해주세요."
        )
    # 0 < prepaid_sum < net_total 은 정상 — 현장 결제분(미정산)으로 안내된다.

    return _exit("")


def route_request_node(state: SettlementState) -> dict:
    """전략 분기 (결정적). parsed_json만 보면 판정이 확정되므로 LLM을 쓰지 않는다.

    - 선결제(prepaid) 또는 지원금(subsidy)이 있으면 SPONSOR (예외 파이프라인의 상위집합)
    - 그 외 예외 조건이 하나라도 있으면 EXCEPTION
    - 아무 예외도 없으면 SIMPLE
    """
    pj = state.get("parsed_json", {})
    participants = pj.get("participants", [])
    has_subsidy = bool(pj.get("subsidy", 0))
    has_prepaid = any(p.get("prepaid", 0) for p in participants)
    has_exception = any(p.get("exceptions") for p in participants)

    if has_subsidy or has_prepaid:
        strategy = "SPONSOR"
    elif has_exception:
        strategy = "EXCEPTION"
    else:
        strategy = "SIMPLE"
    return {"strategy": strategy}


def calculation_node(state: SettlementState) -> dict:
    return {"calculation_result": calculate(state["parsed_json"])}


def _build_explanation(cr: dict, parsed_json: dict) -> str:
    participants = cr.get("participants", [])
    discount_logs = cr.get("discount_logs", {})
    surcharge_logs = cr.get("surcharge_logs", {})
    surcharge_deductions = cr.get("surcharge_deductions", {})
    total = parsed_json.get("total_amount", 0)
    n = len(participants)

    # parsed_json에서 예외 조건 타입 조회
    discount_exc_type = {}
    surcharge_exc_type = {}
    for p in parsed_json.get("participants", []):
        for exc in p.get("exceptions", []):
            if "discount_rate" in exc:
                discount_exc_type[p["name"]] = exc.get("type", "감액")
            if "surcharge_rate" in exc or "surcharge_amount" in exc:
                surcharge_exc_type[p["name"]] = exc.get("type", "할증")

    lines = [f"총 {total:,}원 / {n}명", ""]
    step = 1

    if discount_logs:
        lines.append(f"{step}. 감액 적용")
        for name, logs in discount_logs.items():
            exc_type = discount_exc_type.get(name, "감액")
            lines.append(f"  - {name} ({exc_type})")
            for log in logs:
                lines.append(f"    · {log}")
        lines.append("")
        lines.append("  감액 후 부담액:")
        for p in participants:
            lines.append(f"    - {p['name']}: {p['breakdown']['step1_amount']:,}원")
        lines.append("")
        step += 1

    if surcharge_logs:
        lines.append(f"{step}. 할증 적용")
        for name, logs in surcharge_logs.items():
            exc_type = surcharge_exc_type.get(name, "할증")
            lines.append(f"  - {name} ({exc_type})")
            for log in logs:
                lines.append(f"    · {log}")
            if name in surcharge_deductions:
                d = surcharge_deductions[name]
                targets_str = ", ".join(d["targets"])
                lines.append(f"    · 차감 → {targets_str} 각 {d['per_person']:,}원")
        lines.append("")
        step += 1

    lines.append(f"{step}. 최종 정산 금액")
    for p in participants:
        lines.append(f"  - {p['name']}: {p['final_amount']:,}원")
    lines.append(f"  합계: {sum(p['final_amount'] for p in participants):,}원 ✓")

    # ── 선결제·지원금·송금 안내 (SPONSOR) ──
    settlement = cr.get("settlement")
    if settlement:
        if settlement.get("subsidy"):
            lines.append("")
            lines.append(
                f"※ 지원금 {settlement['subsidy']:,}원 차감 "
                f"(정산 대상액 {settlement['net_total']:,}원)"
            )

        if settlement.get("has_prepaid"):
            prepaid_positions = [
                pos for pos in settlement.get("positions", []) if pos.get("prepaid")
            ]
            if prepaid_positions:
                lines.append("")
                lines.append("선결제")
                for pos in prepaid_positions:
                    lines.append(f"  - {pos['name']}: {pos['prepaid']:,}원 선결제")

            step += 1
            lines.append("")
            lines.append(f"{step}. 송금 안내 (순정산: 부담액 − 선결제)")
            for t in settlement.get("transfers", []):
                lines.append(f"  - {t['from']} → {t['to']}: {t['amount']:,}원")
            if settlement.get("balanced"):
                lines.append("  ✓ 선결제로 완전 정산")
            else:
                unsettled_total = sum(u["amount"] for u in settlement.get("unsettled", []))
                lines.append(f"  ⚠️ 미정산 잔액 {unsettled_total:,}원 (현장 결제분)")

    floor = cr.get("floor_applied", [])
    if floor:
        lines.append(f"\n※ 최소 부담 하한선(30%) 적용: {', '.join(floor)}")

    return "\n".join(lines)


def report_generation_node(state: SettlementState) -> dict:
    cr = state.get("calculation_result", {})
    pj = state.get("parsed_json", {})
    calc_explanation = _build_explanation(cr, pj) if cr else ""

    # ── 변경 사항 하이라이트: 피드백 수정 시 직전 결과 대비 금액 변동 (결정적) ──
    change_summary = ""
    prev_calc = state.get("prev_calc") or {}
    if prev_calc and cr.get("participants"):
        prev_amounts = {p["name"]: p["final_amount"] for p in prev_calc.get("participants", [])}
        change_summary = _build_change_summary(prev_amounts, cr["participants"])
        if change_summary:
            calc_explanation = (
                "[직전 결과 대비 변경]\n" + change_summary + "\n\n" + calc_explanation
            )

    settlement = cr.get("settlement")
    if settlement and settlement.get("has_prepaid"):
        # SPONSOR(선결제): 부담액이 아닌 송금 목록을 LLM에 전달 (산술 금지 유지)
        ctx_parts = []
        if settlement.get("subsidy"):
            ctx_parts.append(f"[지원금]\n  {settlement['subsidy']:,}원 반영")
        prepaid_lines = [
            f"  {pos['name']}: {pos['prepaid']:,}원 선결제"
            for pos in settlement.get("positions", []) if pos.get("prepaid")
        ]
        if prepaid_lines:
            ctx_parts.append("[선결제]\n" + "\n".join(prepaid_lines))
        transfer_lines = [
            f"  {t['from']} → {t['to']}: {t['amount']:,}원"
            for t in settlement.get("transfers", [])
        ]
        ctx_parts.append("[송금 목록]\n" + ("\n".join(transfer_lines) or "  (없음)"))
        if not settlement.get("balanced"):
            unsettled_total = sum(u["amount"] for u in settlement.get("unsettled", []))
            ctx_parts.append(f"[미정산 잔액]\n  {unsettled_total:,}원 (현장 결제분)")
        share_context = "[입력]\n" + "\n".join(ctx_parts)
        share_message = _call_llm(
            _SHARE_MESSAGE_SPONSOR_SYSTEM, share_context, temperature=0.2, tag="SHARE_MSG_SPONSOR"
        )
    else:
        finals = "\n".join(
            f"  {p['name']}: {p['final_amount']:,}원"
            for p in cr.get("participants", [])
        )
        share_context = f"[최종 정산 금액]\n{finals}"
        share_message = _call_llm(
            _SHARE_MESSAGE_SYSTEM, share_context, temperature=0.3, tag="SHARE_MSG"
        )
    return {
        "calc_explanation": calc_explanation,
        "final_report": share_message,
        "change_summary": change_summary,
    }


def feedback_intent_node(state: SettlementState) -> dict:
    """피드백 입력의 의도를 분류한다 (modify_exception / reset / complaint).

    - complaint: 직전 결과를 자가 진단해 되묻기 메시지를 만들어 흐름을 종료한다.
    - reset: 기존 이력을 비우고 새 정산(input_parsing)으로 라우팅한다.
    - modify_exception: 기존 흐름(feedback_parsing)으로 진행한다.
    """
    raw = state["raw_input"]
    content = _call_llm(_FEEDBACK_INTENT_SYSTEM, raw, temperature=0, tag="FEEDBACK_INTENT")
    try:
        intent = (_extract_json(content) or {}).get("intent", "modify_exception")
    except Exception:
        intent = "modify_exception"
    if intent not in ("modify_exception", "reset", "complaint"):
        intent = "modify_exception"

    if intent == "complaint":
        return {
            "feedback_intent": "complaint",
            "clarification_needed": _build_complaint_clarification(state.get("prev_calc", {})),
        }
    if intent == "reset":
        # 새 정산으로 전환 — 기존 이력은 비운다 (parsed_json은 input_parsing이 덮어씀)
        return {"feedback_intent": "reset", "feedback_history": []}
    return {"feedback_intent": "modify_exception"}


def feedback_parsing_node(state: SettlementState) -> dict:
    old_pj = state.get("parsed_json", {})
    old_names = [p["name"] for p in old_pj.get("participants", [])]
    history = state.get("feedback_history") or []
    history_text = "\n".join(history) if history else "(없음)"
    context = (
        f"기존 정산 정보:\n{json.dumps(old_pj, ensure_ascii=False)}\n"
        f"이전 피드백 이력:\n{history_text}\n"
        f"새 피드백: {state['raw_input']}"
    )
    content = _call_llm(_FEEDBACK_PARSING_SYSTEM, context, tag="FEEDBACK_PARSING")
    updated_parsed = _extract_json(content)
    updated_parsed = _post_validate_exceptions(updated_parsed)
    new_names = [p["name"] for p in updated_parsed.get("participants", [])]

    # ── 오분류 가드(reset guard): 피드백이라지만 명단이 직전과 절반 이상 다르면
    # 새 정산으로 간주한다. 기존 조건 오염을 막기 위해 원문을 input 프롬프트로 새로 파싱한다.
    if old_names and _participant_overlap(old_names, new_names) < 0.5:
        fresh = _post_validate_exceptions(
            _extract_json(_call_llm(_INPUT_PARSING_SYSTEM, state["raw_input"], tag="INPUT_PARSING_RESET"))
        )
        return {"parsed_json": fresh, "feedback_history": [], "feedback_intent": "reset"}

    updated_history = list(history) + [state["raw_input"]]
    return {"parsed_json": updated_parsed, "feedback_history": updated_history}
