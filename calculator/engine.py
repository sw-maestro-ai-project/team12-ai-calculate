import copy


def _validate(parsed_json: dict) -> None:
    # 계산 시작 전 입력 데이터의 모순·누락을 일괄 검증한다.
    # 정상 흐름에서는 ai/의 safety_check_node가 upstream에서 대부분 차단하지만,
    # 엔진을 독립적으로 호출할 때도 잘못된 값이 계산에 진입하지 않도록 방어한다.
    if "total_amount" not in parsed_json:
        raise ValueError("total_amount is required")
    if "participants" not in parsed_json or not parsed_json["participants"]:
        raise ValueError("participants is required")

    # 중복 참여자 검증 — 같은 이름이 두 번 들어오면 amounts dict에서 덮어써진다
    names = [p["name"] for p in parsed_json["participants"]]
    if len(names) != len(set(names)):
        dups = [n for n in set(names) if names.count(n) > 1]
        raise ValueError(f"중복된 참여자 이름: {', '.join(dups)}")

    items = parsed_json.get("items", [])
    if items:
        # 항목 합계가 total_amount와 다르면 1원이라도 불일치 → 계산 전체가 틀어진다
        items_sum = sum(item["amount"] for item in items)
        if items_sum != parsed_json["total_amount"]:
            raise ValueError(
                f"총액 불일치: items 합계({items_sum}) ≠ total_amount({parsed_json['total_amount']})"
            )

        # target_items에 존재하지 않는 항목명이 있으면 감액/할증이 조용히 누락된다 (silent failure 차단)
        item_names = {item["name"] for item in items}
        for p in parsed_json["participants"]:
            for exc in p.get("exceptions", []):
                for t in exc.get("target_items", []):
                    if t not in item_names:
                        raise ValueError(
                            f"{p['name']}의 target_items '{t}'가 항목 목록에 없습니다"
                        )

    # ── 지원금(subsidy)·선결제(prepaid) 검증 ──
    total_amount = parsed_json["total_amount"]
    subsidy = parsed_json.get("subsidy", 0) or 0
    if subsidy < 0:
        raise ValueError("subsidy는 0 이상이어야 합니다")
    if subsidy >= total_amount:
        # 지원금이 총액 이상이면 정산 대상액(net_total)이 0 이하가 되어 계산이 성립하지 않는다
        raise ValueError(
            f"지원금({subsidy:,}원)이 총액({total_amount:,}원) 이상일 수 없습니다"
        )
    net_total = total_amount - subsidy

    prepaid_sum = 0
    for p in parsed_json["participants"]:
        prepaid = p.get("prepaid", 0) or 0
        if prepaid < 0:
            raise ValueError(f"{p['name']}의 prepaid({prepaid})는 0 이상이어야 합니다")
        prepaid_sum += prepaid
    if prepaid_sum > net_total:
        # 선결제 합이 정산 대상액을 초과하면 net_amount가 음수가 되어 송금 방향이 역전된다
        raise ValueError(
            f"선결제 합({prepaid_sum:,}원)이 정산 대상액({net_total:,}원)을 초과합니다"
        )

    # ── 최종 금액 직접 지정(fixed_amount) 검증 ──
    fixed_sum = 0
    n_fixed = 0
    for p in parsed_json["participants"]:
        fa = p.get("fixed_amount")
        if fa is None:
            continue
        if fa < 0:
            raise ValueError(f"{p['name']}의 fixed_amount({fa})는 0 이상이어야 합니다")
        fixed_sum += fa
        n_fixed += 1
    if n_fixed:
        if fixed_sum > net_total:
            raise ValueError(
                f"고정 금액 합({fixed_sum:,}원)이 정산 대상액({net_total:,}원)을 초과합니다"
            )
        # 전원 고정인데 합계가 안 맞으면 총액 검증에서 영원히 실패한다
        if n_fixed == len(parsed_json["participants"]) and fixed_sum != net_total:
            raise ValueError(
                f"전원 고정 금액 합({fixed_sum:,}원)이 정산 대상액({net_total:,}원)과 일치해야 합니다"
            )

    # ── 예외 조건 rate 범위 검증 ──
    for p in parsed_json["participants"]:
        for exc in p.get("exceptions", []):
            for key in ("discount_rate", "surcharge_rate"):
                if key in exc:
                    rate = exc[key]
                    if rate is None:
                        raise ValueError(
                            f"{p['name']}의 {key}가 null입니다. "
                            "비율을 명시해 주세요 (예: '지각자는 20% 더 내기로 했어')"
                        )
                    if not (0.0 <= rate <= 1.0):
                        raise ValueError(f"{key} {rate}가 유효 범위(0.0~1.0)를 벗어남")
            if "surcharge_amount" in exc:
                amt = exc["surcharge_amount"]
                if amt is None:
                    raise ValueError(
                        f"{p['name']}의 surcharge_amount가 null입니다. "
                        "지각비 금액을 명시해 주세요 (예: '지각비 5000원')"
                    )
                if amt < 0:
                    raise ValueError(f"surcharge_amount {amt}는 0 이상이어야 합니다")


def _calc_step1(items: list, participants: list) -> tuple[dict, dict]:
    """Step 1: 항목별 eligible 참여자 기준 1인 부담액 계산 + discount_rate 감액 + 감액분 재분배

    핵심 원칙: 감액분은 소멸하지 않는다. 누군가 덜 내면 그 차액은 같은 항목을 공유하는
    비감액 참여자들에게 균등 재분배된다 (총 항목 비용 보존).

    Returns:
        amounts: 참여자별 누적 부담액 (Step 1 결과, 실수)
        discount_logs: 참여자별 감액 설명 문장 목록 (report_generation에서 계산 근거로 사용)
    """
    amounts = {p["name"]: 0.0 for p in participants}
    discount_logs: dict[str, list[str]] = {}

    for item in items:
        item_name = item["name"]
        item_amount = item["amount"]

        # 이 항목에서 완전 제외(discount_rate=1.0)되는 참여자와 부분 감액 참여자를 분리한다
        excluded = set()
        partial_discounts = {}

        for p in participants:
            for exc in p.get("exceptions", []):
                if item_name in exc.get("target_items", []) and "discount_rate" in exc:
                    rate = exc["discount_rate"]
                    if rate >= 1.0:
                        excluded.add(p["name"])
                    else:
                        partial_discounts[p["name"]] = rate

        # eligible = 이 항목 비용을 나눌 실참여자 (완전 제외자 제외)
        eligible = [p for p in participants if p["name"] not in excluded]
        if not eligible:
            # 모든 참여자가 완전 제외되면 해당 항목 비용을 아무도 안 낸다 → 건너뜀
            continue

        # eligible 인원 기준으로 1인 몫을 계산 (완전 제외자는 분모에서도 빠진다)
        per_person = item_amount / len(eligible)

        # 완전 제외자 로그 (부담액 0원이 된 근거)
        for name in excluded:
            discount_logs.setdefault(name, []).append(
                f"{item_name}: 1인 몫 {round(item_amount / (len(eligible) + 1)):,}원 → 완전 제외 (0원)"
            )

        for p in eligible:
            discount = partial_discounts.get(p["name"], 0.0)
            # 부분 감액: per_person × (1 - discount_rate) 만큼 부담
            amounts[p["name"]] += per_person * (1 - discount)

            if discount > 0:
                discounted_amt = per_person * discount
                final_amt = per_person * (1 - discount)
                discount_logs.setdefault(p["name"], []).append(
                    f"{item_name}: 1인 몫 {round(per_person):,}원 × (1-{discount}) = {round(final_amt):,}원"
                    f" (감액분 {round(discounted_amt):,}원)"
                )

        # 부분 감액분을 비감액 eligible 참여자에게 균등 재분배
        # (감액자가 덜 낸 금액 = 총 감액분 → 비감액자가 나눠 추가 부담)
        total_discount_amount = sum(
            per_person * rate
            for name, rate in partial_discounts.items()
            if name not in excluded
        )
        non_discounted = [p for p in eligible if p["name"] not in partial_discounts]
        if total_discount_amount > 0 and non_discounted:
            redistribute = total_discount_amount / len(non_discounted)
            for p in non_discounted:
                amounts[p["name"]] += redistribute

    return amounts, discount_logs


def _apply_steps_2_to_4(
    amounts: dict,
    participants: list,
    total_amount: int,
    discount_logs: dict | None = None,
    *,
    subsidy: int = 0,
    prepaid_map: dict | None = None,
) -> dict:
    """Step 1 결과를 받아 Step 2~4를 순서대로 적용하고 최종 결과를 반환한다.

    Step 2   : 할증(surcharge) — 지각 등 패널티를 더하고 비할증자에게 차감 분배
    Step 2.5 : 지원금(subsidy) — 총 부담을 net_total 기준으로 비례 축소
    Step 2.7 : 최종 금액 고정(fixed_amount) — 사용자가 직접 지정한 부담액 강제 적용
    Step 3   : 하한선 — 균등 분담액의 30% 미만이면 끌어올리고 차액은 나머지에 비례 차감
    Step 4   : 반올림 및 총액 검증 — 합계가 net_total과 정확히 일치하도록 보정
    Step 5   : 송금 안내 — 선결제가 있을 때만 _build_settlement 호출
    """
    N = len(participants)
    prepaid_map = prepaid_map or {}

    # Step 1 결과를 스냅샷으로 보존 — Step 2 할증 설명에서 "할증 전 부담액"으로 참조
    step1_amounts = dict(amounts)

    # ── Step 2: 할증(surcharge) 적용 ──
    # 할증자 집합을 미리 구해야 "비할증자에게 차감" 로직에서 올바른 대상을 고를 수 있다
    surcharged_names = {
        p["name"] for p in participants
        if any("surcharge_rate" in e or "surcharge_amount" in e
               for e in p.get("exceptions", []))
    }
    surcharge_logs: dict[str, list[str]] = {}       # 계산 근거용 설명 문장
    surcharge_deductions: dict[str, dict] = {}      # 차감 대상과 1인당 차감액

    for p in participants:
        for exc in p.get("exceptions", []):
            surcharge = 0.0
            s1 = step1_amounts[p["name"]]  # 할증 전 개인 부담액(Step 1 결과)

            if "surcharge_rate" in exc:
                # 비율 할증: 할증 전 부담액의 N% 추가 부담
                surcharge = s1 * exc["surcharge_rate"]
                surcharge_logs[p["name"]] = [
                    f"할증 전 부담액: {round(s1):,}원",
                    f"추가 부담: {round(s1):,} × {exc['surcharge_rate']} = {round(surcharge):,}원",
                    f"최종: {round(s1):,} + {round(surcharge):,} = {round(s1 + surcharge):,}원",
                ]
            elif "surcharge_amount" in exc:
                # 고정 할증: 정해진 금액을 그대로 추가 부담
                surcharge = float(exc["surcharge_amount"])
                surcharge_logs[p["name"]] = [
                    f"할증 전 부담액: {round(s1):,}원",
                    f"추가 부담(고정): {int(surcharge):,}원",
                    f"최종: {round(s1):,} + {int(surcharge):,} = {round(s1 + surcharge):,}원",
                ]

            if surcharge:
                amounts[p["name"]] += surcharge
                # 할증분을 비할증자에게 균등 차감 분배 (총액 보존)
                # 비할증자가 아무도 없으면(전원 할증) 본인 제외 전체에 분배
                non_surcharged = [q for q in participants
                                  if q["name"] != p["name"]
                                  and q["name"] not in surcharged_names]
                targets = non_surcharged or [q for q in participants if q["name"] != p["name"]]
                if targets:
                    deduction = surcharge / len(targets)
                    for o in targets:
                        amounts[o["name"]] -= deduction
                    surcharge_deductions[p["name"]] = {
                        "targets": [o["name"] for o in targets],
                        "per_person": round(deduction),
                    }

    # ── Step 2.5: 지원금(subsidy) 비례 축소 ──
    # 외부 지원금(동아리비, 협찬 등)만큼 총 부담을 줄인다.
    # 각 참여자의 부담액을 (net_total / total_amount) 비율로 일괄 축소해 합계를 net_total에 맞춘다.
    net_total = total_amount - subsidy
    if subsidy > 0:
        factor = net_total / total_amount
        for n in amounts:
            amounts[n] *= factor

    # ── Step 2.7: 최종 금액 직접 지정(fixed_amount) 강제 ──
    # 사용자가 "A는 2만원만 내" 처럼 특정인 부담액을 못박은 경우.
    # 고정 참여자의 부담액을 지정값으로 덮어쓰고, 나머지(net_total - 고정합)를
    # 비고정 참여자의 현재 비율에 따라 비례 재분배한다.
    # 사용자 지정이 우선하므로 고정 참여자에게는 30% 하한선을 적용하지 않는다.
    fixed_map = {
        p["name"]: p["fixed_amount"]
        for p in participants
        if p.get("fixed_amount") is not None
    }
    has_fixed = bool(fixed_map)
    if has_fixed:
        for name, amt in fixed_map.items():
            amounts[name] = float(amt)
        free = [p["name"] for p in participants if p["name"] not in fixed_map]
        remaining = net_total - sum(fixed_map.values())
        if free:
            free_sum = sum(amounts[n] for n in free)
            if free_sum > 0:
                # 비고정 참여자들의 현재 비율을 유지하면서 remaining에 맞게 스케일 조정
                factor = remaining / free_sum
                for n in free:
                    amounts[n] *= factor
            else:
                # 비고정 참여자 합이 0이면 균등 분배 (엣지 케이스)
                share = remaining / len(free)
                for n in free:
                    amounts[n] = share

    # ── Step 3: 최소 부담 하한선 (균등 분담액의 30%) ──
    # 예외 조건으로 부담이 너무 낮아진 경우를 방지한다.
    # - 부담액이 정확히 0원인 참여자(완전 제외)는 하한선을 면제한다 (실제 소비가 없는 경우).
    # - 하한선 미달분은 하한선이 적용되지 않은 나머지 참여자에게 비례 차감한다.
    # - fixed_amount가 지정된 참여자는 사용자 지정값을 보존하기 위해 건너뛴다.
    base = net_total / N
    floor = base * 0.3
    floor_applied = []
    total_floor_extra = 0.0

    if not has_fixed:
        for p in participants:
            name = p["name"]
            if amounts[name] == 0.0:
                continue  # 완전 제외자(discount_rate=1.0) → 하한선 면제
            if amounts[name] < floor:
                total_floor_extra += floor - amounts[name]
                amounts[name] = floor
                floor_applied.append(name)

        if total_floor_extra > 0:
            # 하한선 차액을 비적용자에게 비례 차감 (현재 부담 비율 유지)
            non_floored = [p["name"] for p in participants if p["name"] not in floor_applied]
            if non_floored:
                total_non_floored = sum(amounts[n] for n in non_floored)
                for n in non_floored:
                    if total_non_floored > 0:
                        amounts[n] -= total_floor_extra * amounts[n] / total_non_floored
                    else:
                        amounts[n] -= total_floor_extra / len(non_floored)

    # ── Step 4: 반올림 및 총액 검증 ──
    # 실수 연산 후 int로 반올림하면 합계가 net_total과 ±N원 차이가 날 수 있다.
    # 소수 부분이 가장 큰(올림이 유리한) 또는 가장 작은(내림이 유리한) 참여자 1명에게
    # 차액 전체를 보정해 합계를 정확히 맞춘다.
    int_amounts = {p["name"]: round(amounts[p["name"]]) for p in participants}
    diff = net_total - sum(int_amounts.values())
    rounding_adjusted = None
    if diff != 0:
        # 고정 금액(fixed_amount) 참여자는 보정 대상에서 제외 (지정값 보존)
        candidates = [p["name"] for p in participants if p["name"] not in fixed_map] or [
            p["name"] for p in participants
        ]
        fracs = {n: amounts[n] - int(amounts[n]) for n in candidates}
        adj = (
            max(fracs, key=lambda n: fracs[n])   # diff > 0: 소수 최대(올림 쪽)에 추가
            if diff > 0
            else min(fracs, key=lambda n: fracs[n])  # diff < 0: 소수 최소(내림 쪽)에서 차감
        )
        int_amounts[adj] += diff
        rounding_adjusted = adj

    total_verified = sum(int_amounts.values()) == net_total

    # ── 결과 조립 ──
    has_prepaid = any(prepaid_map.get(p["name"], 0) for p in participants)
    has_sponsor = subsidy > 0 or has_prepaid

    participants_out = []
    for p in participants:
        name = p["name"]
        entry = {
            "name": name,
            "final_amount": int_amounts[name],
            "breakdown": {
                "base": int(base),              # 균등 분담액 (총액 ÷ N)
                "step1_amount": round(step1_amounts[name]),  # 감액 후·할증 전 부담액
            },
        }
        # net_amount(순정산액 = 부담액 − 선결제)는 선결제가 있을 때만 의미가 있다
        if has_prepaid:
            entry["net_amount"] = int_amounts[name] - prepaid_map.get(name, 0)
        participants_out.append(entry)

    result = {
        "participants": participants_out,
        "total_verified": total_verified,
        "floor_applied": floor_applied,
        "rounding_adjusted": rounding_adjusted,
    }
    if discount_logs:
        result["discount_logs"] = discount_logs
    if surcharge_logs:
        result["surcharge_logs"] = surcharge_logs
    if surcharge_deductions:
        result["surcharge_deductions"] = surcharge_deductions
    # 선결제·지원금이 있는 경우에만 송금 안내(settlement) 블록을 생성한다
    if has_sponsor:
        result["settlement"] = _build_settlement(
            participants_out, prepaid_map, subsidy, net_total, has_prepaid
        )
    return result


def _build_settlement(
    participants_out: list,
    prepaid_map: dict,
    subsidy: int,
    net_total: int,
    has_prepaid: bool,
) -> dict:
    """Step 5: 순정산(net = 부담 − 선결제) 기반 송금 지시 생성 (그리디 매칭).

    각 참여자의 net값(= 최종 부담액 − 선결제)을 계산한다.
    - net > 0 : 아직 더 내야 하는 사람 (debtor)
    - net < 0 : 이미 더 낸 만큼 돌려받아야 하는 사람 (creditor)

    greedy 매칭: debtor와 creditor를 금액 내림차순으로 정렬해 큰 것끼리 먼저 매칭한다.
    선결제 합 < net_total이면 매칭 후 남은 debtor가 unsettled(현장 결제분)로 남는다.
    선결제가 전혀 없고 지원금만 있는 경우, 송금 정산이 성립하지 않아 transfers를 비워둔다.
    """
    positions = [
        {
            "name": p["name"],
            "burden": p["final_amount"],            # 최종 부담액
            "prepaid": prepaid_map.get(p["name"], 0),  # 선결제액
            "net": p["final_amount"] - prepaid_map.get(p["name"], 0),  # 순정산액
        }
        for p in participants_out
    ]

    if not has_prepaid:
        # 지원금만 있고 선결제가 없는 경우 — 각자 현장에서 자기 몫을 결제하면 되므로 송금 불필요
        return {
            "subsidy": subsidy,
            "net_total": net_total,
            "has_prepaid": False,
            "balanced": True,
            "positions": positions,
            "transfers": [],
            "unsettled": [],
        }

    # net > 0 (더 내야 함) / net < 0 (받아야 함) 분리 후 내림차순 정렬
    debtors = sorted(
        ([p["name"], p["net"]] for p in positions if p["net"] > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    creditors = sorted(
        ([p["name"], -p["net"]] for p in positions if p["net"] < 0),
        key=lambda x: x[1],
        reverse=True,
    )

    # 그리디 매칭: debtor[i]와 creditor[j] 중 작은 쪽만큼 송금, 소진된 쪽 인덱스 전진
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d, c = debtors[i], creditors[j]
        pay = min(d[1], c[1])
        if pay > 0:
            transfers.append({"from": d[0], "to": c[0], "amount": int(pay)})
        d[1] -= pay
        c[1] -= pay
        if d[1] <= 0:
            i += 1
        if c[1] <= 0:
            j += 1

    # 매칭 후 남은 debtor = 선결제로 커버되지 않은 현장 결제분(미정산)
    unsettled = [{"name": d[0], "amount": int(d[1])} for d in debtors[i:] if d[1] > 0]
    balanced = not unsettled

    return {
        "subsidy": subsidy,
        "net_total": net_total,
        "has_prepaid": True,
        "balanced": balanced,
        "positions": positions,
        "transfers": transfers,
        "unsettled": unsettled,
    }


def calculate(parsed_json: dict) -> dict:
    """정산 계산의 메인 진입점. parsed_json을 받아 참여자별 최종 부담액을 반환한다.

    흐름:
    1. _validate: 입력 검증 (모순·누락 차단)
    2. items가 있으면 _calc_step1으로 항목별 감액 계산, 없으면 균등 분배
    3. _apply_steps_2_to_4로 할증·지원금·하한선·반올림·송금 안내를 순서대로 처리
    """
    _validate(parsed_json)

    total_amount = parsed_json["total_amount"]
    participants = parsed_json["participants"]
    items = parsed_json.get("items", [])
    subsidy = parsed_json.get("subsidy", 0) or 0
    prepaid_map = {p["name"]: p.get("prepaid", 0) or 0 for p in participants}
    N = len(participants)

    # ── Step 1: 항목별 실참여자 기준 비용 분할 ──
    if items:
        # 항목이 있으면 각 항목별로 eligible 참여자를 구해 감액·재분배 계산
        amounts, discount_logs = _calc_step1(items, participants)
    else:
        # 항목이 없으면 총액을 인원수로 단순 균등 분배 (감액 로그 없음)
        base = total_amount / N
        amounts = {p["name"]: base for p in participants}
        discount_logs = {}

    return _apply_steps_2_to_4(
        amounts, participants, total_amount, discount_logs,
        subsidy=subsidy, prepaid_map=prepaid_map,
    )


def recalculate(parsed_json: dict, feedback_json: dict) -> dict:
    """특정 참여자에게 예외 조건을 추가한 뒤 calculate를 재호출한다.

    피드백으로 단일 참여자의 조건만 바뀌는 경우에 사용된다.
    원본 parsed_json을 deepcopy해서 수정하므로 원본은 변경되지 않는다.
    """
    modified = copy.deepcopy(parsed_json)

    target_name = feedback_json["name"]
    additional_exc = feedback_json.get("additional_exception")

    if additional_exc:
        for p in modified["participants"]:
            if p["name"] == target_name:
                p.setdefault("exceptions", []).append(additional_exc)
                break

    return calculate(modified)
