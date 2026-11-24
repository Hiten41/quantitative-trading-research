"""Publish the locked comparison, daily PnL and sensitivity checks from replay output."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from mmbacktest.report import COLOR, LABEL, markdown_table


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(out: Path):
    from plotly.subplots import make_subplots

    lock = load(out / "selection.json")
    name = lock["strategy"]
    blocks = [load(out / "holdout.json"), load(out / "confirmation/holdout.json")]
    labels = ["First holdout", "Confirmation"]
    candidate = [b["results"]["reference"]["policies"][name] for b in blocks]
    winning = sum(x["after_proxy_funding_and_close"] > 0 for x in candidate)
    total = sum(x["after_proxy_funding_and_close"] for x in candidate)
    chosen = next(
        r
        for r in lock["trials"]
        if r["strategy"] == name
        and r["signal_half_life_s"] == lock["config"]["signal_half_life_s"]
        and r["entry_width_bps"] == lock["config"]["entry_width_bps"]
    )

    def fmt(x):
        return f"{x:,.2f}"

    main_rows = []
    daily_rows = []
    stress_rows = []
    fig = make_subplots(rows=2, cols=1, subplot_titles=labels, vertical_spacing=0.14)
    for i, (label, block, selected) in enumerate(zip(labels, blocks, candidate, strict=True), 1):
        phase = block["results"]["reference"]
        for policy, record in phase["policies"].items():
            m = record["metrics"]
            main_rows.append(
                [
                    label,
                    LABEL[policy],
                    fmt(m["gross_pnl"]),
                    fmt(m["fees"]),
                    fmt(m["net_pnl"]),
                    fmt(record["estimated_funding"]),
                    fmt(record["closing_cost"]),
                    fmt(record["after_proxy_funding_and_close"]),
                    m["fill_count"],
                    fmt(m["max_drawdown"]),
                ]
            )
            file = (
                out.parents[1] / "runs/adaptation" / phase["run_id"] / (policy + "-samples.csv.gz")
            )
            with gzip.open(file, "rt") as stream:
                rows = list(csv.DictReader(stream))
            ts = [datetime.fromtimestamp(int(row["ts"]) / 1e9, UTC) for row in rows]
            pnl = [float(row["equity"]) - phase["config"]["capital"] for row in rows]
            fig.add_scatter(
                x=ts,
                y=pnl,
                mode="lines",
                name=LABEL[policy],
                legendgroup=policy,
                showlegend=i == 1,
                line={"color": COLOR[policy], "width": 2 if policy == name else 1},
                row=i,
                col=1,
            )
        for row in selected["daily"]:
            daily_rows.append(
                [label, datetime.fromtimestamp(row["ts"] / 1e9, UTC).date(), fmt(row["pnl"])]
            )
        for tag, r in block["results"].items():
            selected_case = r["policies"][name]
            stress_rows.append(
                [
                    label,
                    tag,
                    r["config"]["maker_fee_bps"],
                    r["config"]["participation"],
                    fmt(selected_case["after_proxy_funding_and_close"]),
                ]
            )
    fig.update_yaxes(title_text="Net PnL (USDT)")
    fig.update_layout(
        template="plotly_white",
        height=760,
        hovermode="x unified",
        title="Held-out ETHUSDT market-making PnL",
        legend={"orientation": "h", "y": -0.1},
    )
    fig.write_html(
        out / "equity.html",
        include_plotlyjs=True,
        config={"responsive": True, "displaylogo": False},
    )
    comparison = markdown_table(
        [
            "Window",
            "Policy",
            "Gross",
            "Maker fees",
            "Marked net",
            "Funding estimate",
            "Close cost",
            "Adjusted net",
            "Fills",
            "Max drawdown",
        ],
        main_rows,
    )
    stresses = markdown_table(
        ["Window", "Scenario", "Maker fee · bp", "Participation", "Adjusted net · USDT"],
        stress_rows,
    )
    daily = markdown_table(["Window", "UTC day", "Marked net change · USDT"], daily_rows)
    conclusion = (
        "Both independent blocks were positive."
        if winning == 2
        else "The results differed in sign across the two independent blocks."
        if winning == 1
        else "Both independent blocks lost money."
    )
    turnover = sum(x["metrics"]["traded_notional"] for x in candidate)
    fees = sum(x["metrics"]["fees"] for x in candidate)
    break_even = (total + fees) * 10000 / turnover
    note = f"""# Can anchored quotes earn back their execution costs?

The selected maker earned **{chosen["net_pnl"]:+.2f} USDT after maker fees in development**. Its funding- and close-adjusted outcomes were **{candidate[0]["after_proxy_funding_and_close"]:+.2f} USDT** on September 7–11 and **{candidate[1]["after_proxy_funding_and_close"]:+.2f} USDT** on September 13–17, 2023 UTC. {conclusion} The arithmetic sum is {total:+.2f} USDT. Both blocks began flat with 10,000 USDT, so the sum describes two separately initialized replays.

[Interactive marked PnL curves](equity.html) · [Candidate selection](selection.json) · [First holdout details](holdout.json) · [Confirmation details](confirmation/holdout.json)

## What changed

The original strategy constantly moved its quotes with the midpoint. The anchored maker submits passive bids 30 bp below a trailing midpoint average and offers 30 bp above it. Its exponential reference has a one-hour half-life and warms up for one hour after startup or a missed refresh. A buy rests at or below the published best bid, and a sell rests at or above the published best ask. The quote stays within the original 0.5 ETH hard position cap, with up to 0.1 ETH per order.

After a fill, the maker tries to reduce inventory near the reference price. If the midpoint moves more than three band widths from the reference, the maker stops adding exposure against the move and places a passive reducing quote at the touch. It resumes that side after price crosses back through the reference. Cancellations and exits remain exposed to latency and strict trade-through. Small residual positions can persist when the venue minimum notional prevents another order.

## Selection and sample integrity

The first development round tested eight combinations of trend filtering, directional passive quoting, half-life and width. All lost money after fees. Before inspecting either evaluation result, four anchored variants were added on the same September 2–6 development data. The selected parameters maximized marked net PnL less estimated terminal close cost and half of maximum drawdown among candidates with fills on both sides and at least 20 touched orders. Two of the twelve configurations earned positive development PnL. Development returns are selection-biased. [First-round record](first-round.json)

September 7–11 was the first fresh holdout. The second five-day window was designated before reading the first holdout PnL. September 12 failed the existing 120-second continuity check. Its raw trades and bookTicker both show about 1,191 seconds without records beginning near 08:33:48 UTC. The confirmation uses the first subsequent contiguous five days, September 13–17. No model parameter changed between these windows. Both results use real Binance USD-M perpetual L1 and trade archives. [First data manifest](quality.json) · [Confirmation data manifest](confirmation/quality.json)

## Performance

All cash values are USDT. Gross PnL includes terminal marked inventory. Marked net deducts the 1 bp maker fee. The adjusted column adds the historical funding-rate estimate and deducts the estimated cost of a terminal unwind. Flat cash would earn 0 USDT under the same zero-interest assumption.

{comparison}

Across both windows the selected policy paid {fees:.2f} USDT on {turnover:,.2f} USDT of traded notional. Holding the observed fills and funding estimate fixed, the per-side maker fee that would have produced zero adjusted PnL was {break_even:.3f} bp. The run assumed 1.000 bp. This threshold describes the sample and is not an available fee quote.

## Cost and fill sensitivity

{stresses}

The fee and participation scenarios rerun the complete policy. Funding cash flow uses the signed position immediately before each settlement. When Binance omits its settlement mark, the calculation uses the last published midpoint strictly before settlement. A terminal close uses the displayed bid for long inventory or the ask for short inventory plus an assumed 5 bp taker fee, conditional on enough displayed size. It is an estimate without additional impact or latency. The main replay uses equal 50 ms entry latency, 80 ms cancellation latency, a 5 s refresh and 15 s lifetime across all policies.

## Day-to-day variation

{daily}

The daily series uses marked equity before the separate funding sensitivity and terminal unwind. Fixed-horizon markouts, exact order counts and independent trade-ID audits are in the linked JSON results. Two five-day blocks on one instrument provide limited evidence about live profitability.
"""
    (out / "findings.md").write_text(note, encoding="utf-8")


if __name__ == "__main__":
    write_report(Path(__file__).resolve().parents[1] / "reports/adaptation")
