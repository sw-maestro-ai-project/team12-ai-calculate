# target_items 이름 불일치 검증 — "조용히 틀리는" 정산 차단

> 이 문서는 자연어 파싱에서 가장 위험한 *silent failure*(틀렸는데 틀린 줄 모르는 결과)를
> 막기 위한 구현 지침이다. 코드를 작성하는 AI(또는 개발자)가 이 문서를 읽고 구현하면 된다.
> 관련: `ai/CLAUDE.md`, `calculator/CLAUDE.md`, 루트 `CLAUDE.md`.

---

## 1. 문제 정의

예외 조건의 감액/할증은 `target_items`에 적힌 **항목 이름**으로 적용 대상을 찾는다.
그런데 이 매칭이 **정확한 문자열 일치**라서, LLM이 항목명을 실제 `items[].name`과
다르게 쓰면 감액/할증이 **조용히 적용되지 않는다.**

`calculator/engine.py` `_calc_step1` (해당 로직):

```python
for exc in p.get("exceptions", []):
    if item_name in exc.get("target_items", []) and "discount_rate" in exc:
        ...  # ← target_items에 item_name이 "정확히" 들어있어야만 적용
```

- 항목명이 `"주류"`인데 LLM이 `target_items: ["술"]`로 쓰면 → **감액 미적용.**
- 에러도, 경고도 없다. D가 술을 안 마셨는데도 술값을 그대로 부담하는 결과가
  **정상인 척 출력**된다.

`ai/nodes.py` `safety_check_node`는 다음을 검증한다:
- 총액/참여자 누락, 중복 이름, 항목 합계 ≠ 총액
- **items가 아예 없는데** target_items가 지정된 경우
- `discount_rate` null, 할증 rate/amount 미지정
- (SPONSOR) 지원금·선결제 범위

그러나 **"target_items에 적힌 이름이 실제 items에 존재하는지"는 검증하지 않는다.**
→ LLM이 항목명 매핑을 틀리는 순간, 검증을 그냥 통과한다.

### 재현 예시

```
입력: "총 8만원, A·B·C·D. 주류 3만 / 안주 5만. D는 술 안 마셨어."

LLM이 아래처럼 파싱하면 (항목명을 '술'로 오기):
  D.exceptions = [{"type":"술 미섭취", "target_items":["술"], "discount_rate":1.0}]

기대: D는 주류(3만) 전액 제외 → D 부담 ↓
실제: "술"이 items("주류","안주")에 없음 → 감액 미적용 → D가 전액 부담 (조용히 틀림)
```

> 정산 서비스에서 *틀렸는데 틀린 줄 모르는 것*이 가장 치명적이다. 이 구멍을 닫는 것이 목표다.

---

## 2. 개선 목표

1. **검증(필수, P0):** `target_items`의 모든 이름이 실제 `items[].name`에 존재하는지 확인한다.
   하나라도 없으면 계산으로 넘기지 않고 `safety_error`로 되묻는다 → silent failure를
   *명시적 재입력 요청*으로 전환한다.
2. **정규화(권장, P1):** 흔한 동의어(술→주류, 고기/음식→안주 등)는 검증 전에 코드가
   실제 항목명으로 교정해, 불필요한 재입력 요청(오탐)을 줄인다.

> 노드 역할 분리 원칙(코치 피드백 반영):
> - **정규화(교정)** 는 `_post_validate_exceptions`(보정 노드)가 담당한다.
> - **검증(차단)** 은 `safety_check_node`(검증 노드)가 담당한다.
> - 두 책임을 한 곳에 섞지 않는다.

---

## 3. 구현 상세 명세

### 3-1. `ai/nodes.py` — `safety_check_node`에 target_items 검증 추가 (P0, 필수)

기존 "items 없는데 target_items 지정" 검증 **직후**에 다음을 추가한다.
items가 있을 때, 각 예외의 `target_items` 이름이 실제 항목명 집합에 포함되는지 확인한다.

```python
# ── target_items 이름이 실제 items에 존재하는지 검증 ──
item_names = {i["name"] for i in pj.get("items", [])}
if item_names:  # items가 있을 때만 검증 (items 없는 케이스는 위에서 이미 처리)
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
```

- `_exit`는 `safety_check_node` 내부의 기존 헬퍼(`{"safety_error": ...}` 반환)를 그대로 쓴다.
- 효과: 잘못된 항목명 → 계산 진입 전 차단 → 사용자에게 정확한 항목명 재입력 안내.

### 3-2. `ai/nodes.py` — `_post_validate_exceptions`에 동의어 정규화 추가 (P1, 권장)

검증(3-1) **이전 단계**인 `_post_validate_exceptions`에서, 흔한 동의어를 실제 항목명으로
교정한다. 이렇게 하면 LLM이 "술/고기" 등으로 써도 검증에서 튕기지 않는다.

```python
# 동의어 → 실제 항목명 후보 (부분 문자열로 매칭)
ITEM_SYNONYMS = {
    "주류": ["술", "맥주", "소주", "주류", "음주"],
    "안주": ["안주", "음식", "고기", "food", "메뉴"],
}

def _normalize_target_items(parsed: dict) -> dict:
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
        # 2) 부분 문자열 일치(예: "주류값" → "주류")
        for name in item_names:
            if name in token or token in name:
                return name
        return token  # 매핑 실패 → 원본 유지 (이후 safety_check가 차단)

    for p in parsed.get("participants", []):
        for exc in p.get("exceptions", []):
            if exc.get("target_items"):
                exc["target_items"] = [_map_one(t) for t in exc["target_items"]]
    return parsed
```

`input_parsing_node`/`feedback_parsing_node`에서 기존 `_post_validate_exceptions(parsed)`
호출 뒤에 `parsed = _normalize_target_items(parsed)`를 이어 붙인다. (또는
`_post_validate_exceptions` 마지막에서 호출한다.)

> 주의: `ITEM_SYNONYMS`는 항목명이 **실제로 존재할 때만** 매핑한다. 임의로 항목을
> 만들어내지 않는다. 정규화로도 못 풀면 원본을 그대로 두어 3-1의 검증이 차단하게 한다.

### 3-3. (선택) `calculator/engine.py` — 방어적 가드

엔진은 `safety_check_node` 통과를 전제로 동작하므로 **필수 변경은 아니다.** 다만 엔진을
독립 호출(테스트·재사용)할 때를 위해 `_validate`에 동일 검증을 추가해 두면 더 안전하다.

```python
# _validate 내부, items 검증 부근
item_names = {i["name"] for i in items}
if item_names:
    for p in parsed_json["participants"]:
        for exc in p.get("exceptions", []):
            for t in exc.get("target_items", []):
                if t not in item_names:
                    raise ValueError(
                        f"{p['name']}의 target_items '{t}'가 항목 목록에 없습니다"
                    )
```

---

## 4. 설계 원칙 준수 사항

- LLM은 추출/분류만, 금액 산술은 `calculator/`가 담당하는 원칙은 유지된다.
- 본 변경은 **새 LLM 호출을 추가하지 않는다.** 전부 결정적 코드 검증/정규화다.
- 노드 역할을 명확히 분리한다: 정규화=`_post_validate_exceptions`(보정),
  검증=`safety_check_node`(차단). 검증 실패는 `safety_error`로 사용자에게 되묻는다.
- 동의어 사전은 실재하는 항목명에만 매핑한다 — 없는 항목을 생성하지 않는다.

---

## 5. 테스트 시나리오

### 5-1. 검증 (P0)

```
시나리오 V1 — 항목명 오기 차단
  items: 주류, 안주 / D.target_items: ["술"]  (정규화 비활성 가정)
  기대: safety_error 발생, "항목 이름이 일치하지 않습니다: D의 '술'" 안내

시나리오 V2 — 정상 입력 통과
  items: 주류, 안주 / D.target_items: ["주류"]
  기대: 통과 → 정상 계산 (회귀 없음)

시나리오 V3 — items 없는 균등 분배
  items 없음, 예외도 target_items 없음
  기대: 기존과 동일하게 통과 (본 검증은 items 있을 때만 동작)
```

### 5-2. 정규화 (P1)

```
시나리오 N1 — 동의어 교정
  items: 주류, 안주 / D.target_items: ["술"]
  기대: 정규화가 ["주류"]로 교정 → safety_check 통과 → 감액 정상 적용

시나리오 N2 — 부분 문자열 교정
  items: 공통비 / C.target_items: ["공통"]
  기대: ["공통비"]로 교정 후 통과

시나리오 N3 — 매핑 불가
  items: 주류, 안주 / D.target_items: ["택시비"]
  기대: 정규화 실패 → 원본 유지 → safety_check가 차단(V1과 동일)
```

> `ai/` 파싱 계층은 현재 단위 테스트가 없으므로, `_post_validate_exceptions` /
> `_normalize_target_items` / `safety_check_node`를 **LLM 없이** 결정적으로 검증하는
> 테스트를 함께 추가하는 것을 권장한다 (입력 dict → 기대 결과/에러).

---

## 6. 구현 범위 요약 (파일별)

| 파일 | 변경 내용 | 우선순위 |
|------|-----------|----------|
| `ai/nodes.py` | `safety_check_node`에 target_items 존재 검증 추가 | **P0 (필수)** |
| `ai/nodes.py` | `_normalize_target_items` 추가 + 파싱 노드에서 호출 | P1 (권장) |
| `calculator/engine.py` | `_validate`에 방어적 동일 검증 (선택) | P2 (선택) |
| `ai/tests/` (신규) | 정규화·검증의 결정적 단위 테스트 | P1 (권장) |

## 7. 완료 기준 (DoD)

- 잘못된 항목명(items에 없는 `target_items`)이 들어오면 **계산되지 않고** safety_error로 차단된다.
- 흔한 동의어(술→주류 등)는 정규화로 자동 교정되어 불필요한 재입력 요청이 발생하지 않는다.
- 정상 입력의 기존 계산 결과는 변하지 않는다 (회귀 없음).
- 새 LLM 호출이 추가되지 않는다.
