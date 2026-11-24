"""Clocked two-wallet simulation. Each segment ends before the next actionable event."""

from __future__ import annotations

import bisect
import math

import numpy as np

from .sources import MINUTE


def price_bar(bar, sign, c, closing=False, force_price=None):
    _op, _high, _low, close, volume, quote, buy, buy_quote = bar
    if volume <= 0 or quote <= 0:
        if force_price is None:
            return None
        volume, quote, buy, buy_quote = 1.0, close, 1.0, close
    ref = quote / volume
    size, cash = (buy, buy_quote) if sign == 1 else (volume - buy, quote - buy_quote)
    if size <= 0 or cash <= 0:
        if force_price is None:
            return None
        size, cash = 1.0, ref
    if force_price is None and size * c["participation"] + 1e-12 < c["qty"]:
        return None
    side = max(0, sign * (cash / size - ref))
    spread = side + ref * c["halfspread_bps"] / 10000
    factor = c.get("exit_factor", 1) if closing else 1
    slip = ref * c["slippage_bps"] * c.get("slip_factor", 1) * factor / 10000
    raw = ref + sign * (spread + slip)
    if force_price is not None:
        raw = max(raw, force_price + slip)
    price = (math.ceil(raw / c["tick"]) if sign == 1 else math.floor(raw / c["tick"])) * c["tick"]
    return ref, price, spread, slip, sign * (price - ref) - spread - slip


def signal(history, clock, c):
    j = bisect.bisect_right(history, (clock - c["publication_delay_minutes"] * MINUTE, math.inf))
    sample = history[max(0, j - c["lookback"]) : j]
    if len(sample) < c["lookback"]:
        return None
    return (
        sum(r for _, r in sample) / len(sample),
        sample[-1][0],
        sum(r < 0 for _, r in sample[-3:]),
    )


def settlement(ev, q, choice, credit):
    rate = ev["rate"]
    key = {"lower": "low" if rate >= 0 else "high", "upper": "high" if rate >= 0 else "low"}.get(
        choice, "mark"
    )
    cash = q * rate * ev[key]
    return cash * (credit if cash > 0 else 1)


def simulate(config, kinds, events, prior, start, policy, details=False):
    c = config
    if (
        policy not in ("A", "B", "C")
        or c["hedge_delay"] < 1
        or c["transfer_delay"] < 1
        or not 0 < c["participation"] <= 1
    ):
        raise ValueError("invalid policy, order clock or participation")
    s, p, m = (kinds[k] for k in ("spot1m", "perp1m", "mark"))
    n = len(m) - 1
    cutoff = n - c["exit_lead"]
    q = c["qty"]
    capital = c["capital"]
    if q <= 0 or abs(q / c["lot"] - round(q / c["lot"])) > 1e-8 or q * m[0, 3] < c["min_notional"]:
        raise ValueError("invalid order size")
    collateral = min(
        capital,
        q * m[0, 3] * (1 + c["collateral_reserve"]) / c["leverage"] * c.get("collateral_factor", 1),
    )
    spot_cash = capital - collateral
    wallet = collateral
    spot = short = entry = spot_entry = 0.0
    transit = 0.0
    arrival = n + 2
    last_exit = -(10**8)
    opened = 0
    expense = 0.0
    entry_equity = capital
    pending = None
    due = n + 2
    transfers = liqs = warnings = rejections = 0
    warned = halt = False
    history = sorted(prior + [(v["time"], v["rate"]) for v in events.values()])
    future_events = sorted(events)
    eq = np.full(n + 1, float(capital))
    fundpath = np.zeros(n + 1)
    utilization = np.zeros(n + 1)
    exposure = np.zeros(n + 1)
    unhedged = np.zeros(n + 1)
    fills = []
    flows = []
    risks = []
    states = []
    decisions = []
    spot_reference = futures_reference = spread_cost = slip_cost = round_cost = impact_cost = 0.0
    spot_fees = futures_fees = penalty = received = paid = est_net = 0.0
    mismatch = turnover = committed = 0.0
    anchor = None
    min_buffer = min_distance = math.inf
    max_lev = max_ratio = 0.0
    count_pos = count_neg = count_zero = 0
    policy_options = c.get("policies", {}).get(policy, {})
    rules = c["signals"] | policy_options.get("signals", {})
    risk = c["risk"] | policy_options.get("risk", {})
    fees = np.asarray(c["fees_bps"]) * c.get("fee_factor", 1) / 10000
    initial_margin = c.get("initial_margin", 0.1)
    if initial_margin < c["mmr"]:
        raise ValueError("maintenance exceeds initial margin")

    def time(i):
        return start + (i - 1) * MINUTE

    def equity(i):
        return spot_cash + spot * s[i, 3] + wallet + short * (entry - m[i, 3]) + transit

    def mark_risk(i, message):
        risks.append(
            {
                "time": time(i),
                "event": message,
                "spot_qty": spot,
                "short_qty": short,
                "wallet": wallet,
                "transit": transit,
            }
        )

    def fill(i, venue, sign, forced=False):
        nonlocal \
            spot_cash, \
            wallet, \
            spot, \
            short, \
            entry, \
            spot_entry, \
            spot_reference, \
            futures_reference
        nonlocal \
            spot_fees, \
            futures_fees, \
            spread_cost, \
            slip_cost, \
            round_cost, \
            impact_cost, \
            penalty, \
            mismatch, \
            anchor, \
            turnover, \
            committed
        bar = (s if venue == "spot" else p)[i]
        closing = (venue == "spot" and sign == -1) or (venue == "perp" and sign == 1)
        quote = price_bar(bar, sign, c, closing, max(m[i, 1], bar[1]) if forced else None)
        if quote is None:
            if forced:
                raise ValueError("missing forced-execution price proxy")
            return False
        ref, px, cross, slip, residual = quote
        fee = q * px * fees[int(venue == "perp")]
        if q * px < c["min_notional"]:
            return False
        if venue == "spot":
            if sign == 1 and spot_cash < q * px + fee:
                return False
            spot_cash -= sign * q * px + fee
            spot += sign * q
            spot_fees += fee
            spot_reference -= sign * q * ref
            spot_entry = px if sign == 1 else 0.0
            if p[i, 4] <= 0:
                raise ValueError("missing synchronized futures benchmark")
            if anchor is None:
                anchor = p[i, 5] / p[i, 4]
            else:  # delayed sale following forced futures cover
                mismatch += q * (p[i, 5] / p[i, 4] - anchor)
                anchor = None
        else:
            if sign == -1 and wallet - fee < q * max(px, m[i, 3]) * initial_margin:
                return False
            if sign == 1:
                wallet += q * (entry - px)
            short -= sign * q
            wallet -= fee
            futures_fees += fee
            futures_reference -= sign * q * ref
            entry = px if sign == -1 else 0.0
            if anchor is None:
                anchor = ref
            else:
                mismatch -= sign * q * (ref - anchor)
                anchor = None
            if forced:
                charge = q * px * c["liquidation_penalty"]
                wallet -= charge
                penalty += charge
        spread_cost += q * cross
        slip_cost += q * slip
        impact = max(0.0, max(m[i, 1], bar[1]) + slip - (ref + cross + slip)) if forced else 0.0
        impact_cost += q * impact
        round_cost += q * (residual - impact)
        turnover += q * px
        committed = max(
            committed, collateral + transfers * c["transfer_amount"] + spot * spot_entry
        )
        if details:
            fills.append(
                {
                    "time": time(i) + MINUTE,
                    "venue": venue,
                    "side": "buy" if sign == 1 else "sell",
                    "qty": q,
                    "reference": ref,
                    "price": px,
                    "fee": fee,
                    "spread": q * cross,
                    "slippage": q * slip,
                    "residual": q * residual,
                    "forced": forced,
                }
            )
        return True

    def schedule(action, i):
        nonlocal pending, due
        pending = action
        due = i

    i = 1
    while i <= n:
        clock = time(i)
        if arrival == i:
            wallet += transit
            transit = 0.0
            arrival = n + 2
            mark_risk(i, "transfer available")
        # Decisions observe only completed bars. Settlements become signals after publication lag.
        if pending is None and (i == 1 or clock % (60 * MINUTE) == 0 or i == cutoff):
            sig = signal(history, clock, rules)
            prev = equity(i - 1)
            basis = (p[i - 1, 3] / s[i - 1, 3] - 1) * 10000
            ratio = (
                short * m[i - 1, 3] * c["mmr"] / max(1e-12, wallet + short * (entry - m[i - 1, 3]))
            )
            if short:
                reason = "scheduled" if i >= cutoff else ""
                if policy != "A" and not reason:
                    if (
                        i - opened >= 60 * rules["max_hold_hours"]
                        or expense >= rules["max_expense"]
                    ):
                        reason = "holding or expense limit"
                    elif (
                        sig
                        and i - opened >= 60 * rules["min_hold_hours"]
                        and sig[0] * 10000 <= rules["exit_bps"]
                    ):
                        reason = "funding exit"
                    if policy == "C" and (
                        ratio >= risk["margin_ratio"]
                        or abs(basis) > risk["max_abs_basis_bps"]
                        or entry_equity - prev >= risk["max_loss"]
                        or (sig and sig[2] >= risk["deterioration_negative_count"])
                    ):
                        reason = "risk exit"
                if reason:
                    schedule("sell", i)
                    if details:
                        decisions.append(
                            {
                                "time": clock,
                                "action": reason,
                                "last_signal": sig[1] if sig else None,
                            }
                        )
            elif not spot and not halt and i < cutoff - c["hedge_delay"] - 2:
                expected = 0.0 if sig is None else sig[0] * rules["forecast_events"]
                hurdle = rules["cost_buffer"] * (
                    2 * sum(fees)
                    + 4
                    * (c["halfspread_bps"] + c["slippage_bps"] * c.get("slip_factor", 1))
                    / 10000
                )
                eligible = (
                    i == 1
                    if policy == "A"
                    else (
                        sig
                        and sig[0] * 10000 >= rules["entry_bps"]
                        and expected >= hurdle
                        and i - last_exit >= 60 * rules["cooldown_hours"]
                    )
                )
                if policy == "C":
                    eligible = (
                        eligible
                        and abs(basis) <= risk["max_abs_basis_bps"]
                        and collateral + q * s[i - 1, 3] <= capital * risk["allocation_fraction"]
                        and sig[2] < risk["deterioration_negative_count"]
                    )
                if (
                    eligible
                    and spot_cash >= q * s[i - 1, 3] * (1 + fees[0])
                    and wallet >= q * m[i - 1, 3] * (initial_margin + fees[1])
                ):
                    schedule("buy", i)
                    entry_equity = prev
                    if details:
                        decisions.append(
                            {
                                "time": clock,
                                "action": "entry",
                                "last_signal": sig[1] if sig else None,
                            }
                        )
        ev = events.get(i)
        if ev:
            flow = settlement(ev, short, c.get("mark_choice", "point"), c.get("credit", 1))
            alternatives = [short * ev["rate"] * ev[k] for k in ("low", "high")]
            cash = {"lower": min(alternatives), "upper": max(alternatives)}.get(
                c.get("mark_choice"), short * ev["rate"] * ev["mark"]
            )
            if abs(flow - (min(0, cash) + max(0, cash) * c.get("credit", 1))) > 1e-10:
                raise ArithmeticError("signed funding audit failed")
            wallet += flow
            received += max(0.0, flow)
            paid += max(0.0, -flow)
            expense += max(0.0, -flow)
            if short:
                count_pos += int(flow > 0)
                count_neg += int(ev["rate"] < 0)
                count_zero += int(ev["rate"] == 0)
            if ev["source"] != "REST settlement":
                est_net += flow
            if details:
                flows.append(dict(**ev, short=short, cashflow=flow))
        # Advance across minutes with identical positions, stopping at the first actionable risk crossing.
        nextev = bisect.bisect_right(future_events, i)
        next_hour = i + 60 - (clock // MINUTE) % 60
        stop = min(
            n + 1,
            next_hour,
            future_events[nextev] if nextev < len(future_events) else n + 1,
            cutoff if cutoff > i else n + 1,
            arrival,
            due if due > i else (i + 1 if pending else n + 1),
        )
        j = stop - 1
        cash_before = spot_cash + wallet + transit
        spot_before, short_before, entry_before = spot, short, entry
        if short:
            highs = m[i : j + 1, 1]
            notional = short * highs
            fe = wallet + short * (entry - highs)
            ratios = notional * c["mmr"] / np.maximum(fe, 1e-12)
            breach = fe <= notional * (c["mmr"] + c["liquidation_penalty"])
            trigger = breach.copy()
            if not warned:
                trigger |= ratios >= c["margin_warning"]
            can_transfer = (
                not transit
                and transfers < c["transfer_limit"]
                and spot_cash >= c["transfer_amount"]
            )
            if can_transfer:
                trigger |= ratios >= c["transfer_trigger"]
            if np.any(trigger):
                j = i + int(np.argmax(trigger))
            k = j - i
            min_buffer = min(min_buffer, float(np.min((fe - notional * c["mmr"])[: k + 1])))
            threshold = (wallet + short * entry) / (
                short * (1 + c["mmr"] + c["liquidation_penalty"])
            )
            min_distance = min(min_distance, float(np.min((threshold / highs[: k + 1] - 1) * 100)))
            max_lev = max(
                max_lev, float(np.max(notional[: k + 1] / np.maximum(fe[: k + 1], 1e-12)))
            )
            utilization[i : j + 1] = ratios[: k + 1]
            max_ratio = max(max_ratio, float(np.max(ratios[: k + 1])))
            if ratios[k] >= c["margin_warning"] and not warned:
                warnings += 1
                warned = True
                mark_risk(j, "margin warning")
            if breach[k]:
                mark_risk(j, "liquidation")
                if not fill(j, "perp", 1, True):
                    raise ValueError("unfilled liquidation")
                liqs += 1
                halt = True
                if spot:
                    schedule("emergency sell", j + 1)
                else:
                    pending = None
                    due = n + 2
            elif can_transfer and ratios[k] >= c["transfer_trigger"]:
                spot_cash -= c["transfer_amount"]
                transit = c["transfer_amount"]
                transfers += 1
                committed = max(
                    committed, collateral + transfers * c["transfer_amount"] + spot * spot_entry
                )
                arrival = j + max(1, c["transfer_delay"])
                mark_risk(j, "transfer requested")
        if due == j:
            action = pending
            venue = "perp" if action in ("short", "cover") else "spot"
            side = 1 if action in ("buy", "cover") else -1
            if fill(j, venue, side):
                if action in ("buy", "sell"):
                    schedule("short" if action == "buy" else "cover", j + c["hedge_delay"])
                else:
                    pending = None
                    due = n + 2
                    if action == "short":
                        opened = j
                        expense = 0.0
                        warned = False
                    else:
                        last_exit = j
            else:
                rejections += 1
                if (
                    action == "short"
                    and wallet < q * m[j, 3] * (initial_margin + fees[1])
                    or action == "buy"
                    and spot_cash < q * s[j, 3] * (1 + fees[0])
                    or action in ("buy", "short")
                    and j >= cutoff - 1
                ):
                    if spot:
                        schedule("emergency sell", j + 1)
                    else:
                        pending = None
                        due = n + 2
                else:
                    due = j + 1
        # Portfolio after fills at the bar close; prior rows use the state at segment start.
        if j > i:
            eq[i:j] = (
                cash_before + spot_before * s[i:j, 3] + short_before * (entry_before - m[i:j, 3])
            )
            exposure[i:j] = short_before
            unhedged[i:j] = abs(spot_before - short_before)
        eq[j] = equity(j)
        costs_so_far = (
            spot_fees + futures_fees + spread_cost + slip_cost + round_cost + impact_cost + penalty
        )
        marked_pnl = (
            spot_reference
            + spot * s[j, 3]
            + futures_reference
            - short * m[j, 3]
            + received
            - paid
            - costs_so_far
        )
        if abs(marked_pnl - (eq[j] - capital)) > 1e-7:
            raise ArithmeticError("open portfolio attribution failed")
        exposure[j] = short
        unhedged[j] = abs(spot - short)
        fundpath[i : j + 1] = received - paid
        if details:
            states.append(
                {
                    "time": time(j) + MINUTE,
                    "spot_cash": spot_cash,
                    "spot_qty": spot,
                    "futures_qty": -short,
                    "futures_wallet": wallet,
                    "transit": transit,
                    "spot_unrealized": spot * (s[j, 3] - spot_entry),
                    "futures_unrealized": short * (entry - m[j, 3]),
                    "spot_realized": spot_reference
                    + spot * spot_entry
                    - (
                        sum(
                            f["spread"] + f["slippage"] + f["residual"]
                            for f in fills
                            if f["venue"] == "spot"
                        )
                    ),
                    "futures_realized": wallet
                    - collateral
                    - transfers * c["transfer_amount"]
                    + transit
                    - received
                    + paid
                    + futures_fees
                    + penalty,
                    "spot_mark": s[j, 3],
                    "perp_mark": m[j, 3],
                    "notional": short * m[j, 3],
                    "initial_margin": short * m[j, 3] * initial_margin,
                    "maintenance": short * m[j, 3] * c["mmr"],
                    "available_margin": wallet
                    + short * (entry - m[j, 3])
                    - short * m[j, 3] * initial_margin,
                    "equity": eq[j],
                }
            )
        i = j + 1
    if spot or short:
        raise ValueError("terminal liquidation incomplete")
    pnl = eq[-1] - capital
    costs = spot_fees + futures_fees + spread_cost + slip_cost + round_cost + impact_cost + penalty
    if abs(pnl - (spot_reference + futures_reference + received - paid - costs)) > 1e-7:
        raise ArithmeticError("terminal attribution failed")
    dd = np.maximum.accumulate(eq) - eq
    daily = np.diff(eq[np.unique(np.r_[0, np.arange(1440, n, 1440), n])])
    metric = {
        "net": float(pnl),
        "spot_pnl": spot_reference,
        "futures_pnl": futures_reference,
        "basis_pnl": spot_reference + futures_reference - mismatch,
        "legging": mismatch,
        "funding_received": received,
        "funding_paid": paid,
        "funding_net": received - paid,
        "estimated_funding": est_net,
        "fees_spot": spot_fees,
        "fees_perp": futures_fees,
        "spread": spread_cost,
        "slip": slip_cost,
        "tick_rounding": round_cost,
        "forced_impact": impact_cost,
        "liquidation_fee": penalty,
        "total_cost": costs,
        "zero_funding": pnl - received + paid,
        "breakeven_net_funding": costs - spot_reference - futures_reference,
        "breakeven_gross_receipts": costs - spot_reference - futures_reference + paid,
        "allocated": capital,
        "committed": committed,
        "return_pct": 100 * pnl / capital,
        "committed_return_pct": 100 * pnl / committed if committed else None,
        "capital_util_pct": 100 * committed / capital,
        "drawdown": float(max(dd)),
        "pnl_to_drawdown": pnl / max(dd) if max(dd) > 0 else None,
        "max_margin_ratio": max_ratio,
        "min_margin_buffer": None if math.isinf(min_buffer) else min_buffer,
        "min_liq_distance_pct": None if math.isinf(min_distance) else min_distance,
        "max_effective_leverage": max_lev,
        "warnings": warnings,
        "liquidations": liqs,
        "transfers": transfers,
        "rejected_minutes": rejections,
        "holding_hours": float(np.sum(exposure > 0) / 60),
        "positive": count_pos,
        "negative": count_neg,
        "zero": count_zero,
        "negative_pct": 100 * count_neg / max(1, count_pos + count_neg + count_zero),
        "avg_basis_bps": float(np.mean((p[1:, 3] / s[1:, 3] - 1) * 10000)),
        "max_hedge_deviation": float(max(unhedged)),
        "unhedged_minutes": int(np.sum(unhedged > 0)),
        "executed_notional": turnover,
        "daily_profit_factor": float(daily[daily > 0].sum() / -daily[daily < 0].sum())
        if np.any(daily < 0)
        else None,
        "spot_cash": spot_cash,
        "futures_wallet": wallet,
        "in_transit": transit,
    }
    curves = []
    if details:
        for k in np.unique(np.r_[0, np.arange(60, n, 60), n]):
            curves.append(
                {
                    "time": time(int(k)) + MINUTE,
                    "equity": eq[k],
                    "drawdown": dd[k],
                    "funding": fundpath[k],
                    "margin": utilization[k],
                    "basis_bps": (p[k, 3] / s[k, 3] - 1) * 10000,
                }
            )
    return metric, {
        "curves": curves,
        "fills": fills,
        "settlements": flows,
        "risk_events": risks,
        "balances": states,
        "decisions": decisions,
    }
