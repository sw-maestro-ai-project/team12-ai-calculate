# Sponsor 분기 개발 명세

## 목적

`12조_프로젝트 기획서_AI_정산비서.docx`에 있는 흐름 그대로 `SPONSOR` 분기를 현재 LangGraph에 반영하기 위한 문서다.

새로운 정산 정책을 만들지 않는다. 기획서에 있는 `SIMPLE / EXCEPTION / SPONSOR` 전략 분기 중 누락된 `SPONSOR` 흐름만 현재 코드에 맞게 추가한다.

## 기획서 기준 흐름

기획서의 시스템 관점 워크플로우는 다음 흐름이다.

```text
사용자 입력 수신
-> Input Parsing Node
-> Safety Check Node
-> Route Request Node (SIMPLE / EXCEPTION / SPONSOR 분기)
-> Calculation Node
-> Report Generation Node
-> Streamlit 화면 출력
```

현재 코드도 거의 같은 흐름이다.

```text
START
  -> input_parsing 또는 feedback_parsing
  -> safety_check
  -> route_request
  -> calculation
  -> report_generation
  -> END
```

추가해야 하는 것은 새로운 서비스 구조가 아니라, `route_request`가 기획서대로 `SPONSOR`를 반환할 수 있게 하고, `SPONSOR` 전략일 때 선결제 정보를 계산 결과와 설명에 반영하는 것이다.

## 기획서 기준 SPONSOR 정의

기획서의 SPONSOR는 다음 상황이다.

```text
SPONSOR: 일부 인원 선결제 포함 복합 정산
```

기획서에는 문제 배경과 KPI에서 `선결제/지원금`도 함께 언급된다. 다만 대표 시나리오 B와 SPONSOR 정의에는 `A가 50,000원을 먼저 결제했다`는 선결제 흐름이 구체적으로 제시되어 있다. 따라서 이 문서는 기획서에 명시된 선결제 흐름을 우선 반영하고, 지원금은 별도 산식이 기획서에 확정되어 있지 않으므로 새로운 계산 정책을 만들지 않는다.

대표 시나리오:

```text
친구 5명(A, B, C, D, E)이 총 120,000원을 사용했고,
A가 50,000원을 먼저 결제했다.
비용은 주류 50,000원, 안주 50,000원, 공통 비용 20,000원이다.
D는 술을 마시지 않았고,
E는 늦게 도착해 안주를 거의 먹지 않았다.
```

따라서 SPONSOR 분기는 다음을 의미한다.

- 선결제 정보가 입력에 포함되어 있다.
- 선결제 정보와 기존 예외 조건이 함께 있을 수 있다.
- 선결제 정보는 감액/할증 예외가 아니다.
- 기존 계산 엔진은 참여자별 부담 금액을 계산한다.
- SPONSOR 처리는 선결제 정보를 결과 설명과 공유 메시지에 반영한다.

## 하지 않을 것

기획서에 없는 새 정책을 만들지 않는다.

- `grant`, `treat`, `external_payer`, `external_grant` 같은 새 타입을 만들지 않는다.
- 지원금/쐈다/전액 부담은 기획서에 구체 계산 산식이 없으므로 새 정책으로 자동 계산하지 않는다.
- 기존 `calculator/engine.py`의 계산 규칙을 크게 바꾸지 않는다.
- `front/app.py`에 새 화면 구조를 강제로 만들지 않는다.
- 테스트 작성은 이번 요청 범위에서 제외한다.

## parsed_json에 추가할 선결제 필드

기획서의 선결제 정보를 담기 위해 `parsed_json`에 `payments`만 추가한다.

```json
{
  "total_amount": 120000,
  "items": [
    { "name": "주류", "amount": 50000 },
    { "name": "안주", "amount": 50000 },
    { "name": "공통비", "amount": 20000 }
  ],
  "participants": [
    { "name": "A", "exceptions": [] },
    { "name": "B", "exceptions": [] },
    { "name": "C", "exceptions": [] },
    {
      "name": "D",
      "exceptions": [
        { "type": "술 미섭취", "target_items": ["주류"], "discount_rate": 1.0 }
      ]
    },
    {
      "name": "E",
      "exceptions": [
        { "type": "소량 섭취", "target_items": ["안주"], "discount_rate": 0.7 }
      ]
    }
  ],
  "payments": [{ "payer": "A", "amount": 50000 }]
}
```

필드 의미:

| 필드       | 의미                    |
| ---------- | ----------------------- |
| `payments` | 선결제 목록             |
| `payer`    | 먼저 결제한 참여자 이름 |
| `amount`   | 먼저 결제한 금액        |

`payments`는 선결제 정보만 담는다. `participants[].exceptions`에는 넣지 않는다.

## Input Parsing Node 수정

`_INPUT_PARSING_SYSTEM`에 기획서의 선결제 추출 규칙만 추가한다.

```text
[선결제 조건]
- "A가 5만원 먼저 냈어", "A가 먼저 결제했어", "A가 계산했어"는 payments에 추출한다.
- payments 형식은 [{"payer": "A", "amount": 50000}]이다.
- 선결제 정보는 participants[].exceptions에 넣지 않는다.
- LLM은 선결제 송금액을 계산하지 않는다.
```

금액이 명확하지 않으면 `amount: null`로 둔다.

## Route Request Node 수정

기획서대로 전략을 3개로 확장한다.

현재:

```json
{"strategy": "SIMPLE" | "EXCEPTION"}
```

변경:

```json
{"strategy": "SIMPLE" | "EXCEPTION" | "SPONSOR"}
```

분기 규칙:

```text
- 선결제 정보(payments)가 있으면 SPONSOR
- 선결제 정보는 없고 술 미섭취/지각/소량 섭취 등 예외 조건이 있으면 EXCEPTION
- 선결제 정보도 예외 조건도 없으면 SIMPLE
```

주의:

- `SPONSOR`는 기획서의 전략 분기다.
- `SPONSOR`라고 해서 별도의 새로운 정산 정책을 만들지 않는다.
- `SPONSOR` 입력에도 예외 조건이 포함될 수 있으므로 기존 `calculation_node`는 그대로 사용한다.

## LangGraph 수정 방향

기획서 흐름을 유지한다.

```text
input_parsing 또는 feedback_parsing
  -> safety_check
  -> route_request
  -> calculation
  -> report_generation
```

단, `route_request`가 `SPONSOR`를 반환할 수 있어야 한다.

선택적으로 `sponsor` 노드를 추가한다면, 새 흐름을 만들기 위한 노드가 아니라 `SPONSOR` 전략의 후처리 노드로만 둔다.

```text
route_request
  -> calculation
  -> [strategy == SPONSOR] sponsor
  -> report_generation
```

즉:

- `SIMPLE`: `route_request -> calculation -> report_generation`
- `EXCEPTION`: `route_request -> calculation -> report_generation`
- `SPONSOR`: `route_request -> calculation -> sponsor -> report_generation`

기존 entry/feedback/safety 흐름은 변경하지 않는다.

## sponsor 노드 역할

`sponsor` 노드를 만든다면 역할은 작아야 한다.

입력:

- `state["strategy"]`
- `state["parsed_json"]["payments"]`
- `state["calculation_result"]`

역할:

- `strategy != "SPONSOR"`이면 `{}` 반환
- `strategy == "SPONSOR"`이면 선결제 정보를 `calculation_result`에 붙인다.
- 선결제 금액이 명확하지 않으면 `safety_error` 반환
- 선결제자가 참여자 목록에 없으면 `safety_error` 반환

권장 결과 추가 필드:

```json
{
  "calculation_result": {
    "participants": [
      { "name": "A", "final_amount": 25000 },
      { "name": "B", "final_amount": 30000 }
    ],
    "payments": [{ "payer": "A", "amount": 50000 }],
    "sponsor_summary": {
      "payer": "A",
      "paid_amount": 50000
    }
  }
}
```

중요:

- `sponsor` 노드는 `participants[].final_amount`를 수정하지 않는다.
- 기존 계산 결과를 재계산하지 않는다.
- 선결제 정보를 설명과 공유 메시지에 사용할 수 있게 붙이는 역할만 한다.

## Safety 처리

기존 `safety_check_node`는 그대로 유지한다.

선결제 관련 최소 검증:

| 조건                         | 메시지                                                                   |
| ---------------------------- | ------------------------------------------------------------------------ |
| `amount`가 null              | `선결제 금액이 명확하지 않습니다. 예) "A가 5만원 먼저 냈어"`             |
| `payer`가 null               | `선결제한 사람의 이름이 누락되었습니다. 예) "A가 5만원 먼저 냈어"`       |
| `payer`가 참여자 목록에 없음 | `선결제자 {payer}가 참여자 목록에 없습니다. 참여자 이름을 확인해주세요.` |

검증 위치는 둘 중 하나로 한다.

- 간단하게 하려면 `sponsor` 노드에서 처리
- 더 일찍 막고 싶으면 `safety_check_node`에 추가

이번 최소 변경에서는 `sponsor` 노드에서 처리하는 편이 기존 safety 흐름을 덜 건드린다.

## Report Generation Node 수정

기획서처럼 최종 결과에서 선결제, 항목 제외, 감액 이유를 설명해야 한다.

`_build_explanation()`에 sponsor 정보가 있으면 아래 문단만 추가한다.

```text
선결제 반영
- A가 50,000원을 먼저 결제했습니다.
- 위 선결제 정보는 최종 공유 메시지에 함께 표시됩니다.
```

공유 메시지에는 선결제 정보를 포함한다.

예:

```text
[정산 안내]
A가 50,000원을 먼저 결제했습니다.

개인별 부담액
- A: 25,000원
- B: 30,000원
- C: 30,000원
- D: 15,000원
- E: 20,000원
```

주의:

- LLM이 선결제 송금액을 새로 계산하지 않게 한다.
- 공유 메시지에는 계산 엔진이 만든 `final_amount`와 입력에서 추출한 `payments`만 전달한다.

## Feedback Parsing Node 수정

기존 피드백 흐름은 유지한다.

추가 규칙:

```text
- 사용자가 "A가 5만원 먼저 냈어"라고 추가하면 기존 parsed_json을 유지하고 payments만 추가한다.
- 사용자가 "A가 아니라 B가 냈어"라고 하면 기존 payments의 payer만 수정한다.
- 사용자가 "5만원이 아니라 6만원이야"라고 하면 기존 payments의 amount만 수정한다.
- sponsor 언급이 없으면 기존 payments를 유지한다.
- sponsor 수정 때문에 기존 participants/items/exceptions를 삭제하지 않는다.
```

## 실제 수정 파일

필수:

- `ai/nodes.py`
  - input parsing 프롬프트에 `payments` 추가
  - route request 프롬프트를 `SIMPLE | EXCEPTION | SPONSOR`로 확장
  - feedback parsing 프롬프트에 payments 유지/수정 규칙 추가
  - sponsor 정보가 있으면 report generation에 반영

선택:

- `ai/graph.py`
  - `SPONSOR` 전략 후처리를 명시적으로 분리하고 싶으면 `sponsor` 노드 추가

가능하면 변경하지 않음:

- `calculator/engine.py`
- `front/app.py`
- `calculator/tests/*`

## 완료 기준

- `route_request`가 기획서대로 `SIMPLE / EXCEPTION / SPONSOR`를 반환할 수 있다.
- 선결제 입력이 `payments`로 파싱된다.
- `payments`가 `participants[].exceptions`에 섞이지 않는다.
- `SPONSOR` 전략에서도 기존 `calculation_node`로 참여자별 부담액을 계산한다.
- 선결제 정보가 계산 근거와 공유 메시지에 포함된다.
- 기존 SIMPLE/EXCEPTION 흐름은 변경되지 않는다.
- 기획서에 없는 `grant`, `treat`, `external_payer` 같은 새 정책을 만들지 않는다.
