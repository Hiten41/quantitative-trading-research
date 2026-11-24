"""Publish a research note from the frozen run rather than hand-entered results."""

from __future__ import annotations

from collections import defaultdict

from .data.pipeline import save_json
from .report import HORIZONS, LABEL, comparison, date_time, fmt, markdown_table, read


def publish(r):
    c, m, q = r.config, r.metrics, r.quality
    fixed, adaptive, ablation = (m[k] for k in ("fixed", "adaptive", "no_skew"))
    cal = read(r.run / "calibration" / "metrics.json")
    blocks = read(r.run / "evaluation" / "paired-blocks.json")
    rows = comparison(m, r.base)
    summary = markdown_table(r.headers, rows)

    marks = []
    for n, v in m.items():
        for h in HORIZONS:
            z = v["markouts"]["all"][str(h)]
            marks.append(
                [
                    LABEL[n],
                    h,
                    z["valid"],
                    z["missing"],
                    fmt(z["immediate"]),
                    fmt(z["directional"]),
                    fmt(z["combined"]),
                    fmt(z["after_fee"]),
                    fmt(v["markouts"]["buy"][str(h)]["directional_bps"]),
                    fmt(v["markouts"]["sell"][str(h)]["directional_bps"]),
                ]
            )
    mark_table = markdown_table(
        [
            "Policy",
            "s",
            "Valid",
            "Missing",
            "Capture",
            "Directional",
            "Combined",
            "After fee",
            "Buy dir. bp",
            "Sell dir. bp",
        ],
        marks,
    )
    scores = defaultdict(float)
    for row in r.sensitivity:
        scores[row["parameter"], row["value"], row["strategy"]] += row["net_pnl"]
    cases = list(dict.fromkeys((p, v) for p, v, n in scores))
    sens = markdown_table(
        ["Parameter", "Value", "Fixed · USDT", "Skew · USDT"],
        [
            [
                p,
                v if p != "reference" else "default",
                fmt(scores.get((p, v, "fixed"))),
                fmt(scores.get((p, v, "adaptive"))),
            ]
            for p, v in cases
        ],
    )
    files = q["files"]
    trades = sum(f["events"] for f in files if f["kind"] == "trades")
    books = sum(f["events"] for f in files if f["kind"] == "bookTicker")
    gaps = sum(len(f.get("trade_id_gaps", [])) for f in files)
    regimes = []
    for i, day in enumerate(r.daily["fixed"]):
        group = [
            row
            for row in r.samples["fixed"]
            if day["ts"] <= row["ts"] < day["ts"] + 86_400_000_000_000
        ]
        mids = [row["mid"] for row in group]
        if mids:
            regimes.append(
                [
                    date_time(day["ts"])[:10],
                    fmt(100 * (mids[-1] / mids[0] - 1)),
                    fmt(100 * (max(mids) - min(mids)) / mids[0]),
                    *[fmt(r.daily[n][i]["pnl"]) for n in m],
                ]
            )
    regime_table = markdown_table(
        [
            "UTC session",
            "Mid return · %",
            "Mid range / open · %",
            *[LABEL[n] + " · USDT" for n in m],
        ],
        regimes,
    )
    quality_table = markdown_table(
        ["UTC day", "Trades", "Book updates", "Max trade gap · s", "Max book gap · s"],
        [
            [
                day,
                *[
                    next(f[k] for f in files if f["day"] == day and f["kind"] == kind)
                    for k, kind in (
                        ("events", "trades"),
                        ("events", "bookTicker"),
                        ("max_gap_seconds", "trades"),
                        ("max_gap_seconds", "bookTicker"),
                    )
                ],
            ]
            for day in sorted({f["day"] for f in files})
        ],
    )
    reconciliation = markdown_table(
        [
            "Policy",
            "Financing cash",
            "Position · ETH",
            "Final equity",
            "Realized gross",
            "Unrealized",
            "Fees",
            "Funding applied",
            "Residual · USDT",
        ],
        [
            [
                LABEL[n],
                *[
                    fmt(v[k])
                    for k in (
                        "cash_equivalent",
                        "terminal_inventory",
                        "net_equity",
                        "realized_gross",
                        "unrealized",
                        "fees",
                        "funding",
                    )
                ],
                f"{v['reconciliation_error']:.2e}",
            ]
            for n, v in m.items()
        ],
    )
    loss = sum(v["net_pnl"] < 0 for v in m.values())
    diff = adaptive["net_pnl"] - fixed["net_pnl"]
    skew_effect = adaptive["net_pnl"] - ablation["net_pnl"]
    text = f"""# Does Inventory-Aware Market Making Improve Risk-Adjusted Performance in Cryptocurrency Markets?

## 1. Abstract

In a held-out replay of {c["symbol"]} perpetual futures from {c["evaluation_start"]} through {c["end"]} UTC, net PnL after maker fees was {fixed["net_pnl"]:+.2f} USDT for fixed quotes, {adaptive["net_pnl"]:+.2f} USDT for adaptive quotes with skew, and {ablation["net_pnl"]:+.2f} USDT for the identical adaptive widths without skew. {r.funding_note} These are simulated marked outcomes on real exchange messages.

## 2. Research question

Does moving the reservation price against accumulated inventory improve marked PnL per unit of exposure? The fixed-to-adaptive comparison changes both width and skew. The adaptive ablation keeps the width rule identical and removes only the reservation-price shift.

## 3. Data and quality checks

The replay consumes {trades:,} individual trade prints and {books:,} top-of-book updates from {c["start"]} through {c["end"]}. Each source passed archive size and SHA-256 checks, schema checks, monotonic time and ID checks, price/size checks, UTC coverage and interday continuity checks. The two feeds overlap on every day. Equal timestamps preserve sequence within each stream. The archive contains {gaps} raw trade-ID jumps. Each was corroborated against a separately checksummed sequence of consecutive aggregate prints. Only raw trades provide fill volume.

{quality_table}

The [dataset manifest](quality.json) records the actual file URLs, hashes, repeated timestamps, minute-level event-rate percentiles, unmatched day edges, contract snapshot and funding responses. Trades use transaction time. Strategies and marks see book updates only after their archived publication time. Exchange post-only checks use the matching-engine transaction clock. One cannot recover client receipt order from this replay. [Binance archive documentation](https://github.com/binance/binance-public-data)

## 4. Market microstructure assumptions

A passive buy requires a sell-initiated print below its price. A passive sell requires a buy-initiated print above its price. The print pays the resting limit price in the simulation. Each strategy independently consumes at most {c["participation"]:.0%} of any eligible historical print, rounded down to tradable lots. Each side has at most one outstanding order. Historical orders and trade prices do not respond to our quotes.

## 5. Strategy formulations

The baseline quotes mid ± mid × {c["spread_bps"]} / 20,000. At refreshes we calculate realized log variance from trailing squared log returns divided by elapsed seconds. Multiplication by current mid squared gives price variance rate v, in (USDT/ETH)²/s. The adaptive reservation price is mid − γ q v H. Signed inventory q is in ETH, H in seconds, and γ in inverse USDT. Its half-width is mid × (half-spread bp + fee bp) / 10,000 + a √(v H) + γ Q v H / 2, where Q is the nominal order size. This simplified rule combines a fee allowance, volatility cushion and inventory-risk term. The no-skew policy sets the reservation price to mid and retains every width term.

## 6. Event-driven simulation methodology

Funding and exchange-side expiry precede trades on a timestamp tie. Individual prints see the last strictly earlier book. After the prints, acknowledgements and quote decisions see the last book through that time. Activation at the same millisecond as a print cannot earn a fill on that print. An indexed L1 stream provides the same last visible state as stepping through every quote update between decision points. Drawdown revalues held inventory at every book update, including intermediate updates at tied timestamps.

New orders take {c["new_latency_ms"]} ms to activate and cancellation takes {c["cancel_latency_ms"]} ms. An order awaiting cancellation remains exposed. Replacements wait for acknowledgement and a subsequent scheduled decision. Exchange-local lifetime is {c["lifetime_ms"]} ms from activation. Books older than {c["stale_ms"]} ms suppress fills and new quotes. A stale decision requests cancellation and resets volatility history.

## 7. Execution and queue limitations

Strict trade-through is an eligibility proxy. Aggregate L1 size gives no identity, hidden depth, full queue or matching-engine priority. Quantity changes alone never produce fills. The optional at-price model starts behind the entire displayed touch at activation and credits only matching prints against that queue. Unknown depth receives no at-price allocation. Neither queue approximation nor no-fill mode contributes to the reported default strategy results.

## 8. Transaction costs and inventory accounting

All policies start flat with {c["capital"]:,.0f} USDT collateral. Each fill pays the configured {c["maker_fee_bps"]} bp maker fee. The fully collateralized research position limit of {c["inventory_limit"]} ETH makes peak position notional small relative to starting capital over this sample. There is no leverage multiplier in PnL. {r.funding_note} Exchange liquidation, mark-price margin mechanics and forced terminal liquidation are outside this replay.

The linear futures wallet equals initial collateral + average-cost realized PnL − fees + funding. Equity adds unrealized PnL marked to the observed L1 mid. Independently, equivalent financing cash equals initial capital minus signed execution notionals and fees plus funding. Equity equals that cash plus inventory times the mark. Residuals below compare both formulas in USDT.

{reconciliation}

## 9. Experimental setup

Calibration covered {c["start"]} through the UTC day before {c["evaluation_start"]}. Its default net PnLs were {cal["fixed"]["net_pnl"]:+.2f} USDT (fixed), {cal["adaptive"]["net_pnl"]:+.2f} (skew) and {cal["no_skew"]["net_pnl"]:+.2f} (ablation). Evaluation began flat with a fresh estimator at {c["evaluation_start"]} 00:00 UTC. Positions and active orders carried across days within each phase. Defaults were specified before computing performance. No parameter was selected by optimizing the holdout.

The common order size was {c["quantity"]} ETH, refresh {c["refresh_ms"]} ms, position cap {c["inventory_limit"]} ETH, and spread floor {c["spread_bps"]} bp full width. Adaptive parameters were γ = {c["gamma"]}, H = {c["horizon_s"]} s, a = {c["vol_width"]} and variance lookback {c["vol_window_s"]} s. Tick, lot, order size, static price and notional filters use a retrieved contract snapshot. Historical filter changes and the exchange’s dynamic mark-relative band cannot be fully reconstructed from L1.

## 10. Results

{summary}

Net PnL includes terminal unrealized value. Fill rate counts distinct submitted orders receiving at least one execution. Inventory averages and RMS use time integrals between fills. Utilization divides mean absolute inventory by the configured cap. The risk ratio divides net PnL by RMS inventory in ETH and is undefined for a flat strategy. It is sensitive to turnover and sample length.

Daily price returns and ranges use one-minute observed mids. Daily PnL uses consecutive UTC closing equity.

{regime_table}

![Held-out marked PnL](pnl.svg)

## 11. Adverse-selection analysis

Immediate capture compares the execution price with the strictly prior midpoint. Directional markout values signed movement from that mid to the most recent valid mid at or before the horizon. Combined values add capture. Fee-adjusted values deduct the execution fee once. Cash amounts below are sums in USDT over the valid subset at each horizon. Side-specific bp values normalize by that side’s valid executed notional. Out-of-sample and stale marks are missing. These overlapping diagnostics have a different accounting horizon from ledger PnL.

At one second, directional movement totaled {fmt(fixed["markouts"]["all"]["1"]["directional"])} USDT for the fixed maker and {fmt(adaptive["markouts"]["all"]["1"]["directional"])} USDT for the inventory-skewed maker. Including execution-price advantage and the maker fee gives {fmt(fixed["markouts"]["all"]["1"]["after_fee"])} and {fmt(adaptive["markouts"]["all"]["1"]["after_fee"])} USDT respectively for the same sets of fills.

{mark_table}

## 12. Sensitivity analysis

Each scenario changes one setting at a time and replays all {len(r.daily["fixed"])} held-out days from flat, carrying inventory between days. The reference row repeats the main evaluation. A fee change affects the adaptive width rule as well as realized costs. Of {len(scores)} policy–scenario combinations, {sum(v < 0 for v in scores.values())} lost money. Zero-fee net PnL was {scores.get(("maker_fee_bps", 0.0, "fixed"), 0):+.2f} USDT for fixed quoting and {scores.get(("maker_fee_bps", 0.0, "adaptive"), 0):+.2f} USDT for the adaptive policy. Cells marked n/a refer to parameters unused by the baseline.

{sens}

The paired adaptive-minus-fixed evaluation PnL difference was {diff:+.2f} USDT. Resampling {blocks["blocks"]} aligned six-hour PnL increments in 5,000 bootstrap replicates gave a descriptive 95% percentile interval of [{blocks["percentile_95"][0]:.2f}, {blocks["percentile_95"][1]:.2f}] USDT for the evaluation-period total. Adjacent blocks share inventory and regimes. The interval is conditional on these paths and the chosen block length.

## 13. Discussion

Adaptive quoting changed turnover from {fixed["traded_notional"]:,.0f} to {adaptive["traded_notional"]:,.0f} USDT and fees from {fixed["fees"]:.2f} to {adaptive["fees"]:.2f} USDT. Its shift component contributed a paired PnL difference of {skew_effect:+.2f} USDT relative to identical adaptive widths with zero skew. Mean absolute position was {adaptive["avg_abs_inventory"]:.3f} ETH with skew and {ablation["avg_abs_inventory"]:.3f} ETH without it. Lower inventory exposure and higher PnL are distinct empirical questions.

## 14. Limitations

The {len(q["coverage"])} observed UTC days and one instrument provide little evidence about persistent performance across regimes. The publication timestamp can lag exchange transactions. Measured transport delay, own market impact, queue cancellations, rebates by account tier, margin liquidation and terminal unwind costs would change a deployment study. Multi-day regime-stratified holdouts, recorded receive timestamps and exchange acknowledgements would make stronger follow-up tests.

## 15. Conclusion

{loss} of the {len(m)} evaluated policies lost money after the assumed maker fee. The observed fixed-to-skew difference combines a width change with inventory control. The more direct test of skew is its {skew_effect:+.2f} USDT difference against the adaptive ablation. This sample cannot establish a durable trading edge.

[Interactive research report](index.html) · [Machine-readable evaluation metrics](evaluation.json) · [Scenario results](sensitivity.json)
"""
    # Unit labels in the research template follow the requested linear base asset.
    import re

    text = re.sub(r"\bETH\b", r.base, text)
    (r.out / "research.md").write_text(text, encoding="utf-8")
    save_json(r.out / "quality.json", q)
    save_json(r.out / "evaluation.json", m)
    save_json(r.out / "sensitivity.json", r.sensitivity)
