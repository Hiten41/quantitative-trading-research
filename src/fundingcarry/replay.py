"""Two cash wallets, aggressive depth-priced executions, and signed funding events."""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import math
import zipfile
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import numpy as np

from .sources import HOUR, MINUTE, SYMBOL, Bars, Book, books_at, iso, ms

BPS = 10_000


def spot_purchase(hedge: float, fee: float, step: float) -> tuple[float, float, float]:
    """Buy enough ETH to pay the commission in ETH and still hedge the full futures quantity."""
    q, f, s = (Decimal(str(x)) for x in (hedge, fee, step))
    if not (q > 0 and s > 0 and 0 <= f < 1):
        raise ValueError("invalid size, lot or commission")
    gross = (q / ((1 - f) * s)).to_integral_value(rounding=ROUND_CEILING) * s
    return float(gross), float(gross * f), float(gross * (1 - f) - q)


@dataclass(frozen=True)
class Fill:
    venue: str
    side: str
    quantity: float
    arrival_ms: int
    book_local_us: int
    book_exchange_us: int
    raw_vwap: float
    price: float
    levels_used: int
    available_eth: float


def execute(book: Book, venue: str, side: str, qty: float, t: int, c: dict, slip: float) -> Fill:
    if not 0 <= t * 1000 - book.local_us <= c["max_book_age_ms"] * 1000:
        raise ValueError("future-dated or stale recorded book")
    if side not in ("buy", "sell") or qty <= 0 or slip < 0 or not 0 < c["depth_participation"] <= 1:
        raise ValueError("invalid market-order inputs")
    levels = book.asks if side == "buy" else book.bids
    remaining = qty
    notional = 0.0
    for i, (p, q) in enumerate(levels, 1):
        take = min(remaining, q * c["depth_participation"])
        remaining -= take
        notional += take * p
        if remaining <= 1e-12:
            vwap = notional / qty
            price = vwap * (1 + (1 if side == "buy" else -1) * slip / BPS)
            return Fill(
                venue,
                side,
                qty,
                t,
                book.local_us,
                book.exchange_us,
                vwap,
                price,
                i,
                sum(size for _, size in levels) * c["depth_participation"],
            )
    raise ValueError(f"insufficient recorded depth for {venue} {side} at {iso(t)}")


def funding_ledger(
    root: Path, records, window, marks: Bars, quantity: float, enter: int, leave: int
):
    def slot(t):
        anchor = round(int(t) / (8 * HOUR)) * 8 * HOUR
        if abs(int(t) - anchor) > 10_000:
            raise ValueError(f"unexpected funding timestamp {t}")
        return anchor

    archive = {}
    for src in records:
        if src["kind"] != "funding":
            continue
        with zipfile.ZipFile(root / src["path"]) as z, z.open(z.namelist()[0]) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                t = int(r["calc_time"])
                if enter < t <= leave:
                    k = slot(t)
                    if k in archive or int(r["funding_interval_hours"]) != 8:
                        raise ValueError("unexpected ETH funding frequency or duplicate")
                    archive[k] = float(r["last_funding_rate"])
    history = next(
        r for r in records if r.get("window") == window["name"] and r["kind"] == "history"
    )
    rows = json.loads((root / history["path"]).read_text())
    expected = set(range((enter // (8 * HOUR) + 1) * 8 * HOUR, leave + 1, 8 * HOUR))
    flows = []
    seen = set()
    for r in rows:
        t = int(r["fundingTime"])
        if not enter < t <= leave:
            continue
        k = slot(t)
        rate = float(r["fundingRate"])
        if (
            r["symbol"] != SYMBOL
            or k in seen
            or not math.isclose(rate, archive[k], rel_tol=0, abs_tol=1e-12)
        ):
            raise ValueError("REST and archive funding mismatch")
        seen.add(k)
        i = marks.at(t // MINUTE * MINUTE)
        given = float(r.get("markPrice") or 0.0)
        if not np.isfinite([rate, given]).all() or given < 0:
            raise ValueError("invalid settlement rate or mark")
        if given:
            px = given
            other = given
            source = "reported settlement mark"
        else:
            # Credit the lower end of the published minute, charge the upper end on a debit.
            px = float(marks.lows[i] if rate >= 0 else marks.highs[i])
            other = float(marks.highs[i] if rate >= 0 else marks.lows[i])
            source = "adverse one-minute mark"
        flows.append(
            {
                "time_ms": t,
                "utc": iso(t),
                "rate": rate,
                "mark": px,
                "source": source,
                "cashflow": quantity * rate * px,
                "opposite_extreme_cashflow": quantity * rate * other,
            }
        )
    if (
        seen != expected
        or set(archive) != expected
        or any(a["time_ms"] >= b["time_ms"] for a, b in itertools.pairwise(flows))
    ):
        raise ValueError("incomplete or unsorted funding calendar")
    return flows


def get_books(root, records, config):
    result = {}
    for r in records:
        if r["kind"] != "depth":
            continue
        spot = r["exchange"] == "binance"
        lag = config["spot_latency_ms" if spot else "futures_latency_ms"]
        requests = [
            ms(w[k]) + lag
            for w in config["windows"]
            for k in ("enter_utc", "exit_utc")
            if w[k][:10] == r["day"]
        ]
        got = books_at(root / r["path"], r["exchange"], requests, config["max_book_age_ms"])
        result.update({(r["exchange"], t): b for t, b in got.items()})
    return result


def simulate(
    c,
    w,
    books,
    flows,
    spot: Bars,
    marks: Bars,
    *,
    fee_multiple=1.0,
    slippage=2.0,
    credit_multiple=1.0,
):
    capital = c["capital_usdt"]
    collateral = c["futures_collateral_usdt"]
    hedge = c["hedge_eth"]
    if (
        Decimal(str(hedge)) % Decimal(str(c["futures_lot_eth"])) != 0
        or not 0 < collateral < capital
    ):
        raise ValueError("bad futures lots or wallet allocation")
    sf = c["spot_taker_bps"] * fee_multiple / BPS
    ff = c["futures_taker_bps"] * fee_multiple / BPS
    buy_qty, buy_fee_eth, dust = spot_purchase(hedge, sf, c["spot_lot_eth"])
    enter, leave = ms(w["enter_utc"]), ms(w["exit_utc"])

    def leg(venue, side, q, decision):
        lag = c["spot_latency_ms" if venue == "binance" else "futures_latency_ms"]
        t = decision + lag
        return execute(books[venue, t], venue, side, q, t, c, slippage)

    buy = leg("binance", "buy", buy_qty, enter)
    short = leg("binance-futures", "sell", hedge, enter)
    sell = leg("binance", "sell", hedge, leave)
    cover = leg("binance-futures", "buy", hedge, leave)
    spot_cash = capital - collateral - buy.quantity * buy.price
    fut_wallet = collateral - hedge * short.price * ff
    if spot_cash < 0 or fut_wallet < hedge * short.price:
        raise ValueError("insufficient prefunded cash at entry")
    cashflows = np.array(
        [r["cashflow"] * (credit_multiple if r["cashflow"] > 0 else 1.0) for r in flows]
    )
    ft = np.array([r["time_ms"] for r in flows], dtype=np.int64)
    cumulative = np.r_[0.0, np.cumsum(cashflows)]
    funding = float(cumulative[-1])
    # Reserve all dust at zero and charge the sale commission in quote currency.
    end_spot = spot_cash + hedge * sell.price * (1 - sf)
    end_futures = (
        fut_wallet + hedge * (short.price - cover.price) - hedge * cover.price * ff + funding
    )
    pnl = end_spot + end_futures - capital
    fees = buy_fee_eth * buy.price + hedge * (sell.price * sf + (short.price + cover.price) * ff)
    basis = hedge * (short.raw_vwap - buy.raw_vwap + sell.raw_vwap - cover.raw_vwap)
    slippage_cost = hedge * sum(abs(x.price - x.raw_vwap) for x in (buy, short, sell, cover))
    dust_cost = dust * buy.price
    err = pnl - (basis + funding - fees - slippage_cost - dust_cost)
    if abs(err) > 1e-8:
        raise ArithmeticError("cash wallets and PnL decomposition disagree")
    # Full-minute high is deliberately conservative on the entry and exit minutes.
    first = marks.at(short.arrival_ms // MINUTE * MINUTE)
    last = marks.at(cover.arrival_ms // MINUTE * MINUTE)
    tt = marks.times[first : last + 1]
    high = marks.highs[first : last + 1]
    before = cumulative[ft.searchsorted(tt, side="left")]
    after = cumulative[ft.searchsorted(tt + MINUTE, side="left")]
    min_equity = fut_wallet + hedge * (short.price - high) + np.minimum(before, after)
    ratio = float((min_equity / (hedge * high)).min())
    if ratio < 0.25:
        raise ValueError("25% prefunded futures collateral buffer breached; result unsupported")
    # Mark after the hourly bar has closed. No candle is used for a fill.
    hours = np.arange((short.arrival_ms // HOUR + 1) * HOUR, sell.arrival_ms, HOUR, dtype=np.int64)
    si = spot.times.searchsorted(hours - HOUR)
    mi = marks.times.searchsorted(hours - MINUTE)
    if not np.array_equal(spot.times[si], hours - HOUR) or not np.array_equal(
        marks.times[mi], hours - MINUTE
    ):
        raise ValueError("missing synchronized hourly valuation")
    equity = (
        spot_cash
        + hedge * spot.closes[si]
        + fut_wallet
        + hedge * (short.price - marks.closes[mi])
        + cumulative[ft.searchsorted(hours, side="right")]
    )
    curve = np.r_[capital, equity, capital + pnl]
    drawdown = float((np.maximum.accumulate(curve) - curve).max())
    metrics = {
        "net_pnl": pnl,
        "return_pct": 100 * pnl / capital,
        "ending_cash": capital + pnl,
        "ending_spot_cash": end_spot,
        "ending_futures_cash": end_futures,
        "basis_pnl_before_slippage": basis,
        "funding": funding,
        "funding_receipts": float(cashflows[cashflows > 0].sum()),
        "funding_debits": float(cashflows[cashflows < 0].sum()),
        "funding_settlements": len(flows),
        "funding_exact_marks": sum(x["source"] == "reported settlement mark" for x in flows),
        "spot_and_futures_fees": fees,
        "extra_slippage": slippage_cost,
        "dust_eth_written_off": dust,
        "dust_cost": dust_cost,
        "reconciliation_error": err,
        "sampled_max_drawdown": drawdown,
        "min_futures_equity_usdt": float(min_equity.min()),
        "min_futures_equity_over_notional": ratio,
        "fills": [asdict(f) for f in (buy, short, sell, cover)],
    }
    path = [
        {"time_ms": int(t), "equity": float(v)}
        for t, v in zip(np.r_[enter, hours, cover.arrival_ms], curve, strict=True)
    ]
    return metrics, path


def experiment(config, root, records):
    from .sources import read_bars

    books = get_books(root, records, config)
    marks = read_bars(root, records, "mark")
    spot = read_bars(root, records, "spot")
    result = {}
    paths = {}
    stress = config["stress"]
    for w in config["windows"]:
        lag = config["futures_latency_ms"]
        flows = funding_ledger(
            root,
            records,
            w,
            marks,
            config["hedge_eth"],
            ms(w["enter_utc"]) + lag,
            ms(w["exit_utc"]) + lag,
        )
        cases = {}
        for name, fee, slip, credit in (
            ("base", 1.0, config["slippage_bps_per_leg"], 1.0),
            ("higher_costs", stress["fee_multiplier"], stress["slippage_bps_per_leg"], 1.0),
            (
                "combined_stress",
                stress["fee_multiplier"],
                stress["slippage_bps_per_leg"],
                stress["positive_funding_multiplier"],
            ),
            ("no_positive_funding", 1.0, config["slippage_bps_per_leg"], 0.0),
        ):
            metrics, path = simulate(
                config,
                w,
                books,
                flows,
                spot,
                marks,
                fee_multiple=fee,
                slippage=slip,
                credit_multiple=credit,
            )
            cases[name] = metrics
            if name == "base":
                paths[w["name"]] = path
        result[w["name"]] = {"window": w, "cases": cases, "funding_events": flows}
    return {
        "protocol_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "config": config,
        "windows": result,
    }, paths
