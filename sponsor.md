# Sponsor 레이어 기획서 — 선결제·지원금 정산 (SPONSOR 전략)

> AI 코딩 에이전트가 그대로 구현할 수 있도록 작성된 실행 기획서.
> 관련: 루트 `CLAUDE.md`, `ai/CLAUDE.md`, `calculator/CLAUDE.md`, `front/CLAUDE.md`.

## 0. 한 줄 요약

기존 부담액 계산(SIMPLE / EXCEPTION) 위에 **"이미 누가 냈는가(선결제)·외부 지원금"을
반영해 최종 송금 흐름(누가 → 누구에게 → 얼마)** 까지 산출하는 **SPONSOR 전략 레이어**를 추가한다.

## 1. 배경 — proposal vs 현재 코드

proposal.docx 원안은 전략이 **SIMPLE / EXCEPTION / SPONSOR (3분기)** 이고 "선결제/지원금 반영"이
KPI·시나리오 B에 명시돼 있으나, 현재 코드는 `payer/sponsor`가 **제거된 2분기** 상태다.
→ 원안의 SPONSOR 전략을, 현재의 레이어드 아키텍처를 깨지 않고 재도입한다.

## 2. 용어 고정

| 용어 | 키 | 정의 |
|------|----|------|
| 부담액 | `final_amount` | 소비·예외에 따라 각자 *내야 할* 금액 (기존 엔진 결과) |
| 선결제 | `prepaid` | 참여자가 *이미 가게에 낸* 돈. 부담액 불변, 정산 시 회수 |
| 지원금 | `subsidy` | *외부에서 들어와 총액을 깎는* 돈. 부담 총합 감소 |
| 순정산액 | `net_amount` | `부담액 − 선결제`. 양수=더 낼 돈, 음수=받을 돈 |
| 송금 지시 | `transfers` | net을 0으로 맞추는 *누가→누구에게→얼마* 목록 |

**선결제(내부 돈, 회수 대상) ≠ 지원금(외부 돈, 총액 차감)** — 절대 혼동 금지.

## 3. 설계 원칙 — 레이어 직교 분리

부담 계산과 송금 정산은 직교한다. 기존 엔진을 갈아엎지 않고 단계만 끼워 넣는다.

```
Layer 1 부담계산(기존)  Step1 감액 → Step2 할증
Layer 2 지원금(신규)    Step2.5 subsidy 비례 축소
Layer 1 마무리(기존)    Step3 하한선 → Step4 반올림/검증  (기준 total → net_total)
Layer 3 송금정산(신규)  Step5 net = 부담−선결제 → 송금 지시
```

**불변식 (구현·테스트 기준):**
1. **하위호환:** prepaid/subsidy 없으면 결과·기존 테스트 **100% 동일**.
2. **부담 보존:** `sum(final_amount) == total_amount − subsidy`.
3. **송금 균형:** `sum(prepaid) == net_total` 이면 `sum(net_amount) == 0` (송금만으로 완전 정산).
4. **LLM 계산 금지:** 금액 *추출*은 LLM, 송금 *계산*은 calculator/.

## 4. 데이터 모델

### 입력 `parsed_json` — 2개 필드만 추가 (둘 다 optional, 기본 0)

```jsonc
{
  "total_amount": 120000,
  "subsidy": 0,                                  // [신규] 외부 지원금
  "participants": [
    {"name": "A", "prepaid": 120000, "exceptions": []},   // [신규] prepaid
    {"name": "D", "exceptions": [{"type":"술 미섭취","target_items":["주류"],"discount_rate":1.0}]}
  ]
}
```

### 출력 `calculation_result` — prepaid/subsidy 있을 때만 추가

```jsonc
{
  "participants": [{"name":"A","final_amount":28250,"net_amount":-91750, ...}],
  "total_verified": true,            // 의미변경: sum == total_amount − subsidy
  "settlement": {                    // [신규]
    "subsidy": 0, "net_total": 120000, "balanced": true,
    "positions": [{"name":"A","burden":28250,"prepaid":120000,"net":-91750}, ...],
    "transfers": [{"from":"B","to":"A","amount":28250}, ...],
    "unsettled": []                  // 미정산 잔액(받는사람 미지정). balanced면 []
  }
}
```

## 5. 계산 엔진 (`calculator/engine.py`)

| 함수 | 변경 |
|------|------|
| `_validate` | `subsidy`(0≤, <total), `prepaid`(0≤) 검증. `sum(prepaid)>net_total`면 ValueError |
| `_apply_steps_2_to_4` | 인자 `subsidy,prepaid` 추가 → Step2.5·Step5 삽입 |
| `calculate` | subsidy/prepaid 추출·전달, settlement·net_amount 조립 |
| `_build_settlement` | **신규** |

**Step 2.5 (지원금, Step2 직후):**
```python
net_total = total_amount - subsidy
if subsidy > 0:
    factor = net_total / total_amount
    for n in amounts: amounts[n] *= factor
    base = net_total / N
else:
    base = total_amount / N
# 이후 Step3 하한선·Step4 검증은 net_total 기준. total_verified = (sum==net_total)
```

**Step 5 (송금, `_build_settlement`):** 그리디 매칭
```python
# positions: net = burden - prepaid
debtors   = [[n, net] for net>0] desc;  creditors = [[n,-net] for net<0] desc
while debtors and creditors:           # net 합이 0이면 항상 완전 매칭
    pay = min(d.amt, c.amt); transfers += {from:d, to:c, amount:pay}; 차감/진행
unsettled = 남은 debtor (sum(prepaid)<net_total 일 때)
# subsidy>0 또는 any(prepaid)일 때만 settlement·net_amount 출력. 아니면 생략(불변식1)
```

## 6. AI 레이어 (`ai/nodes.py`)

- **`_INPUT_PARSING_SYSTEM`**: 선결제/지원금 추출 지시 추가.
  "먼저 냈다/카드로 긁었다/쐈다" → `prepaid`, "동아리 지원/회비/협찬" → `subsidy`.
  *받을 사람·송금액은 계산 금지.*
- **`route_request_node`**: 코드 후처리로 3분기 강제 — `subsidy>0 or any(prepaid)` → **SPONSOR**,
  elif 예외 → EXCEPTION, else SIMPLE. (SPONSOR는 예외 파이프라인 + 송금까지의 상위집합)
- **`safety_check_node`**: `subsidy≥total`, 음수, `sum(prepaid)>net_total` → 재입력 요청.
  `0<sum(prepaid)<net_total`은 에러 아님 → unsettled 안내.
- **`_build_explanation`**: 지원금/선결제/송금 섹션 추가 (순정산·송금 목록).
- **공유 메시지(SPONSOR 변형)**: 부담액 나열이 아닌 **송금 목록**을 LLM에 주고 작성. 산술 금지 유지.

## 7. 프론트 UX (`front/app.py`)

- 배지: `SPONSOR → "💳 선결제 정산"` (`.badge-sponsor`, indigo).
- 참여자 카드: `prepaid>0` 이면 `💳 선결제 N원` 노트.
- **송금 안내 섹션(핵심)**: `settlement.transfers`를 `B ──▶ A  28,250원` 카드로 렌더.
  `balanced` → `✅ 선결제로 완전 정산` / `unsettled` → `⚠️ 미정산 잔액 N원` 경고.
- 사이드바 예시 2종 추가: "💳 선결제 정산", "🎟️ 지원금 포함".

## 8. 워크드 예시 (테스트 기준값)

5명, 총 120,000 (주류5만/안주5만/공통비2만). D 술 미섭취(주류 1.0), E 안주 소량(0.7), **A 전액 선결제**.

| 참여자 | 부담액 | 선결제 | 순정산 |
|--------|-------|--------|--------|
| A | 28,250 | 120,000 | **−91,750** |
| B / C | 28,250 | 0 | 28,250 |
| D | 15,750 | 0 | 15,750 |
| E | 19,500 | 0 | 19,500 |
| 합 | 120,000 | 120,000 | **0** ✅ |

송금: `B→A 28,250 / C→A 28,250 / D→A 15,750 / E→A 19,500` (합 91,750), `balanced=true`.
(안주: E 0.7 → 3,000원, 감액분 7,000을 A·B·C·D에 1,750씩 재분배.)

> proposal 원문대로 A가 50,000만 선결제하면 `sum(prepaid)<net_total` → B→A 21,750 후
> 잔액 70,000은 `unsettled`(현장 결제분). 그래서 불변식3을 권장하고 미달 시 경고.

## 9. 엣지 케이스

| 케이스 | 기대 |
|--------|------|
| 선결제·지원금 없음 | 기존과 100% 동일 (settlement 미출력) |
| 1인 전액 선결제 | 전원 → 그 사람 송금, balanced |
| 선결제 미달 | 송금 후 `unsettled` 표기 (에러 아님) |
| 선결제 초과 | `_validate` ValueError |
| 선결제자 본인도 예외 | 부담에 예외 반영 → net 더 큰 음수 |
| 완전 제외자(0원) | net 0, 하한선 면제 유지 |

## 10. 범위 밖

실제 계좌 이체·페이 딥링크·OCR·로그인/장기저장은 제외. **송금 안내(계산·표시)까지만.**
송금 횟수 최소화 최적화도 제외 — MVP는 단순 그리디.

## 11. 수용 기준 (DoD)

1. prepaid/subsidy 없는 입력 결과·테스트 변경 전과 동일.
2. 전액 선결제 시 송금 정확 + `balanced=true`.
3. 지원금 시 부담 `total−subsidy`로 축소 + 총액 검증 통과.
4. 선결제 미달 시 unsettled graceful 처리 + 프론트 경고.
5. SPONSOR 배지·송금 카드·송금 중심 공유 메시지 노출.
6. §8 기준값이 `pytest calculator/tests/`와 일치.
</content>
