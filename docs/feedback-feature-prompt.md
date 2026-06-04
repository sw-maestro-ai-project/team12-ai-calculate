# 피드백 기능 구현 프롬프트

> 이 문서는 "계산이 잘못됐을 때 피드백" 기능을 구현하기 위한 설계 분석과 구현 지침이다.
> 코드를 작성하는 AI(또는 개발자)가 이 문서를 읽고 구현을 진행하면 된다.

---

## 1. 현재 코드의 피드백 흐름 (있는 것)

```
사용자 입력
    ↓
front/app.py: _invoke_graph(prompt)
    - 이전 assistant 메시지에 parsed_json.participants가 있으면 → 피드백 모드
    - 없으면 → 초기 계산 모드
    ↓
[피드백 모드] ai/graph.py → feedback_parsing_node
    - LLM이 기존 parsed_json에 새 조건만 반영
    - _post_validate_exceptions()로 분류 오류 보정
    ↓
safety_check → route_request → calculation → report_generation
```

**핵심 문제**: `_invoke_graph()`는 이전 `parsed_json`의 존재 여부만 보고 피드백/신규를 분기한다.
사용자가 "계산이 잘못됐어" 또는 "다시 처음부터"라고 해도 피드백 모드로 진입해버린다.

---

## 2. 현재 코드에서 부족한 부분 (개선 대상)

### 2-1. 의도 분류 없음 (`front/app.py`, `ai/graph.py`)

현재 피드백 모드 진입 조건은 `parsed_json`의 존재 여부 하나뿐이다.
다음 세 가지 의도를 구분하지 못한다:

| 의도 | 예시 표현 | 현재 처리 | 올바른 처리 |
|------|-----------|-----------|-------------|
| `modify_exception` | "A가 10% 더 낸다고 했어" | 피드백 모드 (정상) | 피드백 모드 |
| `reset` | "다시 처음부터 할게", "새로운 상황이야" | 피드백 모드 (잘못됨) | 초기 계산 모드 |
| `complaint_only` | "계산이 이상한 것 같아", "왜 이렇게 나왔어?" | 피드백 모드 (불완전) | 어떤 조건이 잘못됐는지 되물어야 함 |

### 2-2. `_FEEDBACK_PARSING_SYSTEM` 프롬프트가 조건 추가/수정만 다룸 (`ai/nodes.py` L96~L115)

- "계산이 잘못됐어" 같은 불만 표현에 대한 처리 로직 없음
- "A는 2만원만 내야 해" 같은 최종 금액 직접 지정을 처리 못 함
- 의도 분류 결과를 JSON에 포함시키는 구조가 없음

### 2-3. UI에 피드백 구분 없음 (`front/app.py`)

- 기존 계산 결과와 수정 후 결과를 나란히 비교하는 화면 없음
- "이 계산 조건 수정하기" vs "새 계산 시작하기"를 명확히 구분하는 버튼 없음
- 사용자가 피드백 모드인지 신규 계산 모드인지 알 수 없음

### 2-4. `SettlementState`에 피드백 의도 필드 없음 (`ai/state.py`)

피드백 의도(`feedback_intent`)가 state를 통해 노드 간에 전달되지 않아서,
`report_generation_node`가 "피드백으로 수정됐다"는 사실을 공유 메시지에 반영할 수 없다.

### 2-5. `calculator/engine.py`의 `recalculate()` 미사용

`recalculate(parsed_json, feedback_json)` 함수가 존재하지만 호출되지 않는다.
LLM의 `feedback_parsing_node`가 `parsed_json` 자체를 수정한 뒤 `calculate()`를 다시 호출하는 방식으로 우회 처리 중이다. 이 함수는 현재 설계에서 불필요하므로 삭제하거나 명시적으로 사용하는 것 중 하나를 선택해야 한다.

---

## 3. 구현 목표

1. **의도 분류**: 피드백 입력이 조건 수정인지 / 새 계산 요청인지 / 단순 불만 표현인지를 판단한다.
2. **complaint 처리**: 사용자가 "계산이 이상해" 같은 말만 할 경우, 어떤 부분이 잘못됐는지 되묻는 응답을 반환한다.
3. **reset 처리**: 사용자가 새 계산을 원할 경우, 기존 `parsed_json`을 버리고 초기 계산 모드로 진입한다.
4. **UI 개선**: 사용자가 현재 모드(피드백 vs 신규)를 인식할 수 있도록 한다.

---

## 4. 구현 상세 명세

### 4-1. `ai/state.py` — `feedback_intent` 필드 추가

```python
class SettlementState(TypedDict, total=False):
    raw_input: str
    parsed_json: dict
    strategy: str
    calculation_result: dict
    feedback_history: list
    calc_explanation: str
    final_report: str
    safety_error: str
    feedback_intent: str   # 추가: "modify_exception" | "reset" | "complaint"
    clarification_needed: str  # 추가: complaint일 때 사용자에게 보낼 되묻기 메시지
```

---

### 4-2. `ai/nodes.py` — `_FEEDBACK_INTENT_SYSTEM` 프롬프트 추가

피드백 진입 시 가장 먼저 의도를 분류하는 LLM 호출용 시스템 프롬프트.

```
목적: 사용자 피드백 텍스트의 의도를 3가지 중 하나로 분류한다.

분류 기준:
- "modify_exception": 기존 계산에 조건을 추가하거나 수정하는 요청
  예) "A가 10% 더 낸다고 했어", "D는 안주도 적게 먹었어", "C 지각비 5000원"
  
- "reset": 기존 계산과 무관하게 새로운 정산을 시작하는 요청
  예) "다시 처음부터 할게", "새로운 정산이야", "방금 건 취소하고", "3명이서 5만원인데"
  
- "complaint": 계산 결과에 불만이나 의문을 표현하지만, 구체적인 수정 조건이 없음
  예) "계산이 이상한 것 같아", "왜 이렇게 나왔어?", "이거 맞아?", "다시 계산해봐"

반환 형식 (JSON만 출력, 설명 없음):
{"intent": "modify_exception" | "reset" | "complaint"}
```

---

### 4-3. `ai/nodes.py` — `feedback_intent_node` 구현

```python
def feedback_intent_node(state: SettlementState) -> dict:
    raw = state["raw_input"]
    response = _call_llm(_FEEDBACK_INTENT_SYSTEM, raw, temperature=0, tag="feedback_intent")
    result = _extract_json(response)
    intent = result.get("intent", "modify_exception")
    
    if intent == "complaint":
        clarification = "어떤 부분이 잘못됐는지 알려주세요. 예) 'A 금액이 너무 높아' 또는 'D가 술을 조금 마셨는데 안 반영됐어'"
        return {"feedback_intent": "complaint", "clarification_needed": clarification}
    
    return {"feedback_intent": intent}
```

---

### 4-4. `ai/graph.py` — 그래프 수정

**`_route_entry` 변경**: 이전과 동일하게 `parsed_json` 유무로 분기하되,
`feedback_intent_node`를 `feedback_parsing_node` 앞에 삽입한다.

**새 라우팅 함수 `_route_after_intent`**:

```python
def _route_after_intent(state: SettlementState) -> str:
    intent = state.get("feedback_intent", "modify_exception")
    if intent == "reset":
        return "input_parsing"       # 기존 parsed_json 무시, 새 파싱
    if intent == "complaint":
        return "end"                 # clarification_needed 반환
    return "feedback_parsing"        # 기본: 조건 수정
```

**수정된 그래프 흐름**:

```
START
  ├─ [parsed_json 없음] ──► input_parsing
  └─ [parsed_json 있음] ──► feedback_intent_node
                                  │
                    ┌─────────────┼─────────────┐
                    ↓             ↓             ↓
               "reset"     "complaint"   "modify_exception"
                    │             │             │
             input_parsing       END      feedback_parsing
                    │                          │
                    └───────────┬──────────────┘
                                ↓
                          safety_check
                                ...
```

**주의**: `intent == "reset"`이면 `raw_input`을 새 정산 텍스트로 인식하고 `input_parsing`으로 보낸다.
이때 `parsed_json`은 state에 남아 있지만 `input_parsing_node`가 새로운 `parsed_json`으로 덮어쓴다.

---

### 4-5. `front/app.py` — `_invoke_graph` 수정

`complaint` 의도로 끝난 경우 (`clarification_needed`가 state에 있으면) 이를 UI에 표시한다.

```python
def _invoke_graph(prompt: str) -> dict:
    prev = next(
        (m for m in reversed(st.session_state.messages) if m["role"] == "assistant"),
        None,
    )
    try:
        if prev and prev.get("parsed_json", {}).get("participants"):
            result = graph.invoke({
                "raw_input": prompt,
                "parsed_json": prev["parsed_json"],
                "strategy": prev.get("strategy", ""),
                "feedback_history": prev.get("feedback_history") or [],
            })
        else:
            result = graph.invoke({
                "raw_input": prompt,
                "feedback_history": [],
            })
        return dict(result)
    except Exception as e:
        return {"error": str(e)}
```

변경 사항: 기존 코드 그대로 유지하되 `_render_result`에서 `clarification_needed`를 처리한다.

---

### 4-6. `front/app.py` — `_render_result` 수정

```python
def _render_result(msg: dict) -> None:
    if msg.get("error"):
        st.error(f"오류: {msg['error']}")
        return
    if msg.get("safety_error"):
        st.warning(f"⚠️ {msg['safety_error']}\n\n입력 내용을 수정해 다시 시도해주세요.")
        return
    
    # complaint 의도: 되묻기 메시지만 표시
    if msg.get("clarification_needed"):
        st.info(f"💬 {msg['clarification_needed']}")
        return
    
    # 이하 기존 결과 렌더링 코드...
```

---

### 4-7. `front/app.py` — 피드백 모드 안내 UI 추가

사용자가 현재 어느 모드인지 알 수 있도록, 채팅 입력 영역 위에 모드 안내 캡션을 표시한다.

```python
# 입력 폼 직전에 삽입
prev_assistant = next(
    (m for m in reversed(st.session_state.messages) if m["role"] == "assistant"),
    None,
)
if prev_assistant and prev_assistant.get("parsed_json", {}).get("participants"):
    st.caption("💬 이전 계산에 조건을 추가하거나 수정할 수 있습니다. 새로 시작하려면 '처음부터 다시'라고 입력하세요.")
```

---

## 5. 구현 범위 요약 (파일별)

| 파일 | 변경 내용 |
|------|-----------|
| `ai/state.py` | `feedback_intent`, `clarification_needed` 필드 추가 |
| `ai/nodes.py` | `_FEEDBACK_INTENT_SYSTEM` 프롬프트 추가, `feedback_intent_node` 함수 추가 |
| `ai/graph.py` | `feedback_intent_node` 삽입, `_route_after_intent` 함수 추가, 엣지 수정 |
| `front/app.py` | `_render_result`에 `clarification_needed` 처리 추가, 피드백 모드 안내 캡션 추가 |

`calculator/engine.py`는 변경하지 않는다. `recalculate()` 함수는 현재 설계에서 사용되지 않으므로 그대로 둔다.

---

## 6. 테스트 시나리오

### 시나리오 1: 조건 수정 (기존 동작 유지 확인)
```
입력 1: "A, B, C, D 4명이서 8만원. 주류 3만, 안주 5만. D 술 미섭취."
→ 정상 계산 (EXCEPTION)

입력 2: "근데 C가 20분 늦게 왔어"
→ intent: modify_exception → feedback_parsing → C에게 surcharge 추가
```

### 시나리오 2: 새 계산 요청
```
입력 1: "A, B, C 3명이서 6만원."
→ 정상 계산

입력 2: "아 잠깐, 완전히 다른 상황인데. E, F 2명이서 3만원 균등 분배야."
→ intent: reset → input_parsing → 기존 parsed_json 무시, 새 계산
```

### 시나리오 3: 단순 불만 표현
```
입력 1: "A, B, C, D 4명이서 8만원..."
→ 정상 계산

입력 2: "계산이 이상한 것 같은데?"
→ intent: complaint → clarification_needed 표시:
   "어떤 부분이 잘못됐는지 알려주세요. 예) 'A 금액이 너무 높아' 또는 'D가 술을 조금 마셨는데 안 반영됐어'"
```

### 시나리오 4: 불만 후 구체적 수정
```
입력 2: "계산이 이상한 것 같은데?"
→ clarification 응답

입력 3: "D 금액이 너무 높아. D가 안주도 적게 먹었어"
→ intent: modify_exception → D에게 안주 discount_rate 추가
```

---

## 7. 설계 원칙 준수 사항

- LLM은 의도 분류와 조건 rate 결정만 수행한다. 금액 산술은 여전히 `calculator/`가 담당한다.
- `feedback_intent_node`는 새 LLM 호출을 1회 추가한다. 기존 `feedback_parsing_node` 호출은 유지한다.
- `complaint` 의도 처리는 `graph.invoke()` 반환값에 `clarification_needed`를 담아 front/가 렌더링한다. 별도 스트리밍이나 중간 응답 메커니즘은 불필요하다.
- 모든 노드 간 데이터 교환은 `SettlementState`를 통해서만 이루어진다.
