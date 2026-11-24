"""Independent cash journal for the unchanged depth-based regression."""

from __future__ import annotations

import json

import numpy as np

from .sources import HOUR, MINUTE, read_bars
from .study_data import rates


def audit(root, records, result, paths):
    marks = read_bars(root, records, "mark")
    spot = read_bars(root, records, "spot")
    archive = {
        round(t / MINUTE): r
        for rec in records
        if rec["kind"] == "funding"
        for t, r, _ in rates(root, rec)
    }
    output = {}
    for name, w in result["windows"].items():
        v = w["cases"]["base"]
        buy, short, sell, cover = v["fills"]
        qty = short["quantity"]
        c = result["config"]
        sf, ff = (c[x] / 10000 for x in ("spot_taker_bps", "futures_taker_bps"))
        buy_fee = buy["quantity"] * sf
        dust = buy["quantity"] - buy_fee - qty
        assert [x["side"] for x in (buy, short, sell, cover)] == ["buy", "sell", "sell", "buy"]
        assert (
            qty == sell["quantity"] == cover["quantity"] == c["hedge_eth"]
            and 0 <= dust < c["spot_lot_eth"]
        )
        for f in (buy, short, sell, cover):
            assert f["book_local_us"] <= f["arrival_ms"] * 1000
            expected = f["raw_vwap"] * (
                1 + (1 if f["side"] == "buy" else -1) * c["slippage_bps_per_leg"] / 10000
            )
            assert abs(f["price"] - expected) < 1e-8
        rest = next(r for r in records if r.get("window") == name)
        rows = json.loads((root / rest["path"]).read_text())
        flows = []
        for row, old in zip(rows, w["funding_events"], strict=True):
            t = int(row["fundingTime"])
            rate = float(row["fundingRate"])
            assert (
                short["arrival_ms"] < t <= cover["arrival_ms"]
                and abs(rate - archive[round(t / MINUTE)]) < 1e-12
            )
            i = marks.at(t // MINUTE * MINUTE)
            exact = float(row.get("markPrice") or 0)
            prior = marks.closes[i - 1]
            low = exact or marks.lows[i]
            high = exact or marks.highs[i]
            flow = qty * rate * (low if rate >= 0 else high)
            assert old["time_ms"] == t and abs(flow - old["cashflow"]) < 1e-10
            flows.append(
                {
                    "time": t,
                    "rate": rate,
                    "exact": bool(exact),
                    "cash": flow,
                    "causal": qty * rate * (exact or prior),
                    "low": min(qty * rate * low, qty * rate * high),
                    "high": max(qty * rate * low, qty * rate * high),
                }
            )
        funding = sum(x["cash"] for x in flows)
        spot_fee = buy_fee * buy["price"] + qty * sell["price"] * sf
        perp_fee = qty * (short["price"] + cover["price"]) * ff
        spot_raw = qty * (sell["raw_vwap"] - buy["raw_vwap"])
        perp_raw = qty * (short["raw_vwap"] - cover["raw_vwap"])
        slip = sum(qty * abs(f["price"] - f["raw_vwap"]) for f in v["fills"])
        dust_cost = dust * buy["price"]
        sc = c["capital_usdt"] - c["futures_collateral_usdt"] - buy["quantity"] * buy["price"]
        fc = c["futures_collateral_usdt"] - qty * short["price"] * ff
        end_s = sc + qty * sell["price"] * (1 - sf)
        end_f = fc + qty * (short["price"] - cover["price"]) - qty * cover["price"] * ff + funding
        net = end_s + end_f - c["capital_usdt"]
        pts = paths[name][1:-1]
        ts = np.array([x["time_ms"] for x in pts])
        vals = (
            sc
            + qty * spot.closes[spot.times.searchsorted(ts - HOUR)]
            + fc
            + qty * (short["price"] - marks.closes[marks.times.searchsorted(ts - MINUTE)])
        )
        vals += np.r_[0.0, np.cumsum([x["cash"] for x in flows])][
            np.searchsorted([x["time"] for x in flows], ts, side="right")
        ]
        assert np.max(abs(vals - [x["equity"] for x in pts])) < 1e-8
        assert (
            max(
                abs(net - v["net_pnl"]),
                abs(net - (spot_raw + perp_raw + funding - spot_fee - perp_fee - slip - dust_cost)),
                abs(end_s - v["ending_spot_cash"]),
                abs(end_f - v["ending_futures_cash"]),
            )
            < 1e-8
        )
        output[name] = {
            "net": net,
            "spot_raw": spot_raw,
            "perp_raw": perp_raw,
            "funding": funding,
            "spot_fee": spot_fee,
            "perp_fee": perp_fee,
            "slip": slip,
            "dust": dust_cost,
            "end_spot": end_s,
            "end_perp": end_f,
            "post_buy_cash": sc,
            "post_short_wallet": fc,
            "positive": sum(x["rate"] > 0 for x in flows),
            "negative": sum(x["rate"] < 0 for x in flows),
            "exact": sum(x["exact"] for x in flows),
            "estimated": sum(not x["exact"] for x in flows),
            "estimated_income": sum(x["cash"] for x in flows if not x["exact"]),
            "causal_net": net + sum(x["causal"] - x["cash"] for x in flows),
            "upper_net": net + sum(x["high"] - x["cash"] for x in flows),
            "no_funding": net - funding,
            "combined_stress": w["cases"]["combined_stress"]["net_pnl"],
            "hours_verified": len(pts),
            "buy_gross": buy["quantity"],
            "buy_fee_eth": buy_fee,
            "dust_eth": dust,
        }
    return output
