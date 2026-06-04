import copy


def _validate(parsed_json: dict) -> None:
    if "total_amount" not in parsed_json:
        raise ValueError("total_amount is required")
    if "participants" not in parsed_json or not parsed_json["participants"]:
        raise ValueError("participants is required")

    # 중복 참여자 검증
    names = [p["name"] for p in parsed_json["participants"]]
    if len(names) != len(set(names)):
        dups = [n for n in set(names) if names.count(n) > 1]
        raise ValueError(f"중복된 참여자 이름: {', '.join(dups)}")

    items = parsed_json.get("items", [])
    if items:
        items_sum = sum(item["amount"] for item in items)
        if items_sum != parsed_json["total_amount"]:
            raise ValueError(
                f"총액 불일치: items 합계({items_sum}) ≠ total_amount({parsed_json['total_amount']})"
            )

        # 방어적 가드: target_items 이름이 실제 항목명에 없으면 차단
        # (정상 흐름에선 ai/의 safety_check_node가 upstream에서 막지만,
        #  엔진을 독립 호출/재사용할 때 silent failure를 방지한다)
        item_names = {item["name"] for item in items}
        for p in parsed_json["participants"]:
            for exc in p.get("exceptions", []):
                for t in exc.get("target_items", []):
                    if t not in item_names:
                        raise ValueError(
                            f"{p['name']}의 target_items '{t}'가 항목 목록에 없습니다"
                        )

    # ── 지원금(subsidy)·선결제(prepaid) 검증 (SPONSOR 레이어) ──
    total_amount = parsed_json["total_amount"]
    subsidy = parsed_json.get("subsidy", 0) or 0
    if subsidy < 0:
        raise ValueError("subsidy는 0 이상이어야 합니다")
    if subsidy >= total_amount:
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
        raise ValueError(
            f"선결제 합({prepaid_sum:,}원)이 정산 대상액({net_total:,}원)을 초과합니다"
        )

    # ── 최종 금액 직접 지정(fixed_amount) 검증 (피드백 Direct Override) ──
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
        if n_fixed == len(parsed_json["participants"]) and fixed_sum != net_total:
            raise ValueError(
                f"전원 고정 금액 합({fixed_sum:,}원)이 정산 대상액({net_total:,}원)과 일치해야 합니다"
            )

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
    """Step 1: 항목별 eligible 기준 1인 부담액 계산 + discount_rate 감액 + 감액분 재분배

    Returns:
        amounts: 참여자별 누적 부담액
        discount_logs: 참여자별 감액 설명 문장 목록 (LLM 역산 방지용)
    """
    amounts = {p["name"]: 0.0 for p in participants}
    discount_logs: dict[str, list[str]] = {}

    for item in items:
        item_name = item["name"]
        item_amount = item["amount"]

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

        eligible = [p for p in participants if p["name"] not in excluded]
        if not eligible:
            continue

        per_person = item_amount / len(eligible)

        # 완전 제외자 로그
        for name in excluded:
            discount_logs.setdefault(name, []).append(
                f"{item_name}: 1인 몫 {round(item_amount / (len(eligible) + 1)):,}원 → 완전 제외 (0원)"
            )

        for p in eligible:
            discount = partial_discounts.get(p["name"], 0.0)
            amounts[p["name"]] += per_person * (1 - discount)

            if discount > 0:
                discounted_amt = per_person * discount
                final_amt = per_person * (1 - discount)
                discount_logs.setdefault(p["name"], []).append(
                    f"{item_name}: 1인 몫 {round(per_person):,}원 × (1-{discount}) = {round(final_amt):,}원"
                    f" (감액분 {round(discounted_amt):,}원)"
                )

        # 감액분은 소멸하지 않고 비감액 eligible 참여자에게 재분배
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
    """Step 2(할증) → Step 2.5(지원금) → Step 3(하한선) → Step 4(반올림/검증) → Step 5(송금)"""
    N = len(participants)
    prepaid_map = prepaid_map or {}

    # Step 1 결과 스냅샷 — 할증 설명 시 LLM에 제공할 중간값
    step1_amounts = dict(amounts)

    # ── Step 2: 할증(surcharge) 적용 ──
    # 비할증자에게만 차감 분배. 전원 할증이면 본인 제외 전체에 분배
    surcharged_names = {
        p["name"] for p in participants
        if any("surcharge_rate" in e or "surcharge_amount" in e
               for e in p.get("exceptions", []))
    }
    # 수식 설명 로그 — Python이 미리 생성해 LLM 역산 오류 방지
    surcharge_logs: dict[str, list[str]] = {}
    surcharge_deductions: dict[str, dict] = {}

    for p in participants:
        for exc in p.get("exceptions", []):
            surcharge = 0.0
            s1 = step1_amounts[p["name"]]  # 할증 전 개인 부담액

            if "surcharge_rate" in exc:
                surcharge = s1 * exc["surcharge_rate"]
                surcharge_logs[p["name"]] = [
                    f"할증 전 부담액: {round(s1):,}원",
                    f"추가 부담: {round(s1):,} × {exc['surcharge_rate']} = {round(surcharge):,}원",
                    f"최종: {round(s1):,} + {round(surcharge):,} = {round(s1 + surcharge):,}원",
                ]
            elif "surcharge_amount" in exc:
                surcharge = float(exc["surcharge_amount"])
                surcharge_logs[p["name"]] = [
                    f"할증 전 부담액: {round(s1):,}원",
                    f"추가 부담(고정): {int(surcharge):,}원",
                    f"최종: {round(s1):,} + {int(surcharge):,} = {round(s1 + surcharge):,}원",
                ]

            if surcharge:
                amounts[p["name"]] += surcharge
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
    # 외부 지원금만큼 총 부담을 줄인다. 각자 부담액을 net_total/total_amount 비율로 축소.
    net_total = total_amount - subsidy
    if subsidy > 0:
        factor = net_total / total_amount
        for n in amounts:
            amounts[n] *= factor

    # ── Step 2.7: 최종 금액 직접 지정(fixed_amount) 강제 (피드백 Direct Override) ──
    # 사용자가 "A는 2만원만 내" 처럼 특정인의 최종 부담을 못박은 경우.
    # 해당 인원은 그 값으로 고정하고, 차액을 나머지(비고정) 참여자에게 비례 재분배한다.
    # 고정값이 명시되면 사용자 지정이 우선하므로 30% 하한선은 적용하지 않는다.
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
                factor = remaining / free_sum
                for n in free:
                    amounts[n] *= factor
            else:
                share = remaining / len(free)
                for n in free:
                    amounts[n] = share

    # ── Step 3: 하한선 적용 (균등 분담액의 30%) ──
    # subsidy가 없으면 net_total == total_amount 이므로 기존과 동일.
    # fixed_amount가 지정된 경우엔 사용자 지정값을 보존하기 위해 하한선을 건너뛴다.
    base = net_total / N
    floor = base * 0.3
    floor_applied = []
    total_floor_extra = 0.0

    if not has_fixed:
        for p in participants:
            name = p["name"]
            if amounts[name] == 0.0:
                continue  # 전액 제외(discount_rate=1.0) → 하한선 미적용
            if amounts[name] < floor:
                total_floor_extra += floor - amounts[name]
                amounts[name] = floor
                floor_applied.append(name)

        if total_floor_extra > 0:
            non_floored = [p["name"] for p in participants if p["name"] not in floor_applied]
            if non_floored:
                total_non_floored = sum(amounts[n] for n in non_floored)
                for n in non_floored:
                    if total_non_floored > 0:
                        amounts[n] -= total_floor_extra * amounts[n] / total_non_floored
                    else:
                        amounts[n] -= total_floor_extra / len(non_floored)

    # ── Step 4: 반올림 및 총액 검증 ──
    int_amounts = {p["name"]: round(amounts[p["name"]]) for p in participants}
    diff = net_total - sum(int_amounts.values())
    rounding_adjusted = None
    if diff != 0:
        # 반올림 차액 보정 대상에서 고정 금액(fixed_amount) 참여자는 제외한다
        # (사용자 지정값이 ±1원 틀어지지 않도록). 비고정 참여자가 없으면 전체 대상.
        candidates = [p["name"] for p in participants if p["name"] not in fixed_map] or [
            p["name"] for p in participants
        ]
        fracs = {n: amounts[n] - int(amounts[n]) for n in candidates}
        adj = (
            max(fracs, key=lambda n: fracs[n])
            if diff > 0
            else min(fracs, key=lambda n: fracs[n])
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
                "base": int(base),
                "step1_amount": round(step1_amounts[name]),
            },
        }
        # 순정산액(net)은 선결제가 있을 때만 의미가 있다 (= 부담액 − 선결제)
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

    debtor(net>0, 더 낼 사람) → creditor(net<0, 받을 사람) 순으로 큰 금액끼리 매칭한다.
    sum(prepaid) == net_total 이면 완전 매칭(balanced), 미달이면 잔액이 unsettled로 남는다.

    선결제가 전혀 없으면(지원금만 있는 경우) 송금 정산이 성립하지 않으므로
    transfers/unsettled를 비우고 balanced=True로 둔다 (각자 자기 몫을 현장 결제).
    """
    positions = [
        {
            "name": p["name"],
            "burden": p["final_amount"],
            "prepaid": prepaid_map.get(p["name"], 0),
            "net": p["final_amount"] - prepaid_map.get(p["name"], 0),
        }
        for p in participants_out
    ]

    if not has_prepaid:
        return {
            "subsidy": subsidy,
            "net_total": net_total,
            "has_prepaid": False,
            "balanced": True,
            "positions": positions,
            "transfers": [],
            "unsettled": [],
        }

    # net>0: 더 내야 함(debtor) / net<0: 받아야 함(creditor)
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

    # 받는 사람이 모두 소진됐는데 남은 debtor = 현장 결제분(미정산)
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
    _validate(parsed_json)

    total_amount = parsed_json["total_amount"]
    participants = parsed_json["participants"]
    items = parsed_json.get("items", [])
    subsidy = parsed_json.get("subsidy", 0) or 0
    prepaid_map = {p["name"]: p.get("prepaid", 0) or 0 for p in participants}
    N = len(participants)

    # ── Step 1: 항목별 실참여자 기준 비용 분할 ──
    if items:
        amounts, discount_logs = _calc_step1(items, participants)
    else:
        base = total_amount / N
        amounts = {p["name"]: base for p in participants}
        discount_logs = {}

    return _apply_steps_2_to_4(
        amounts, participants, total_amount, discount_logs,
        subsidy=subsidy, prepaid_map=prepaid_map,
    )


def recalculate(parsed_json: dict, feedback_json: dict) -> dict:
    modified = copy.deepcopy(parsed_json)

    target_name = feedback_json["name"]
    additional_exc = feedback_json.get("additional_exception")

    if additional_exc:
        for p in modified["participants"]:
            if p["name"] == target_name:
                p.setdefault("exceptions", []).append(additional_exc)
                break

    return calculate(modified)
