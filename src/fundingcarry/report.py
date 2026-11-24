"""Publish a self-contained carry research note and hourly risk chart."""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

from mmbacktest.data.pipeline import save_json

from .sources import iso


def dollars(value):
    return f"{value:+,.2f}"


def publish(out: Path, result: dict, paths: dict, records: list) -> None:
    import plotly.graph_objects as go

    out.mkdir(parents=True, exist_ok=True)
    save_json(out / "results.json", result)
    save_json(out / "sources.json", records)
    cfg = result["config"]
    fig = go.Figure()
    with gzip.open(out / "hourly-equity.csv.gz", "wt", encoding="utf8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["window", "utc", "equity_usdt"])
        for label, path in paths.items():
            utc = [iso(row["time_ms"]) for row in path]
            pnl = [row["equity"] - cfg["capital_usdt"] for row in path]
            for timestamp, row in zip(utc, path, strict=True):
                writer.writerow([label, timestamp, f"{row['equity']:.10f}"])
            fig.add_scatter(name=label, x=utc, y=pnl, mode="lines")
    fig.update_layout(
        template="plotly_white",
        title="ETH spot–perpetual funding carry",
        yaxis_title="Net PnL on 10,000 USDT",
        xaxis_title="UTC",
        hovermode="x unified",
        legend={"orientation": "h"},
    )
    fig.write_html(
        out / "equity.html",
        include_plotlyjs=True,
        config={"responsive": True, "displaylogo": False},
    )
    windows = list(result["windows"].values())
    base = [w["cases"]["base"] for w in windows]

    def row(title, values):
        return "| " + " | ".join([title, *values]) + " |"

    comparisons = []
    for label, k in [
        ("Funding", "funding"),
        ("Spot–perpetual basis PnL before extra slippage", "basis_pnl_before_slippage"),
        ("Trading fees paid", "spot_and_futures_fees"),
        ("Additional slippage charged", "extra_slippage"),
        ("Residual ETH written off at cost", "dust_cost"),
        ("Net cash PnL", "net_pnl"),
        ("Sampled maximum drawdown", "sampled_max_drawdown"),
        ("Lowest futures wallet equity", "min_futures_equity_usdt"),
    ]:
        comparisons.append(row(label, [f"{x[k]:,.2f}" for x in base]))
    comparisons.append(row("Return on total capital", [f"{x['return_pct']:.3f}%" for x in base]))
    comparisons.append(row("Funding events", [str(x["funding_settlements"]) for x in base]))
    table = "\n".join(comparisons)
    stress = "\n".join(
        row(label, [dollars(w["cases"][key]["net_pnl"]) for w in windows])
        for key, label in [
            ("base", "Base assumptions"),
            ("higher_costs", "Double fees, 10 bp extra slippage per leg"),
            ("combined_stress", "Higher costs and half of positive funding"),
            ("no_positive_funding", "Base costs, zero positive funding"),
        ]
    )
    total = sum(x["net_pnl"] for x in base)
    details = []
    for w in windows:
        s = w["window"]
        details.append(f"- **{s['name']}.** Enter {s['enter_utc']} and exit {s['exit_utc']}.")
    schedule = "\n".join(details)
    exact = sum(b["funding_exact_marks"] for b in base)
    count = sum(b["funding_settlements"] for b in base)
    debits = sum(r["rate"] < 0 for w in windows for r in w["funding_events"])
    rec_error = max(abs(c["reconciliation_error"]) for w in windows for c in w["cases"].values())
    note = f"""# ETH spot–perpetual funding carry

The fixed-size hedge earned **{dollars(base[0]["net_pnl"])} USDT** in its first historical window and **{dollars(base[1]["net_pnl"])} USDT** in the next. Both positions were closed. On the same 10,000 USDT capital base the non-compounded sum is **{dollars(total)} USDT ({total / cfg["capital_usdt"] * 100:.2f}%)**. The unchanged confirmation rule uses a fresh 10,000 USDT allocation. Funding accounted for {sum(x["funding"] for x in base):,.2f} USDT across the two windows.

[Interactive hourly PnL](equity.html) · [Fills, payments and cash reconciliation](results.json) · [Source files and hashes](sources.json) · [Fixed research parameters](../../configs/funding_carry.json)

## Trading rule

Buy enough ETH on Binance Spot to own 0.5 ETH after the spot buy commission and short 0.5 ETH of the Binance USD-M ETHUSDT perpetual. Hold the matched quantity until the scheduled exit and close both legs. The rule takes every scheduled funding payment, including negative rates. It has no signal, parameter search or knowledge of future funding. A rise in ETH benefits the spot holding and costs the short roughly the same amount. Changes in the spot–perpetual price difference still affect the pair.

The calendar uses the first two complete calendar quarters after the September development sample, with boundary executions set around noon UTC when public depth records are available. The primary and confirmation dates, costs and stresses were fixed before evaluating either PnL. The preceding passive-quoting attempts remain visible in [the earlier study](../adaptation/findings.md).

{schedule}

## Results

| USDT unless stated | Primary | Confirmation |
| --- | ---: | ---: |
{table}

Drawdown uses hourly spot closes and perpetual mark closes, plus realized terminal cash. Intrahour basis losses may be larger. Idle cash earns zero. Brokerage balances and capital opportunity costs depend on the account.

## What would erase the profit?

| Net PnL, USDT | Primary | Confirmation |
| --- | ---: | ---: |
{stress}

There were {debits} negative funding rates among {count} settlements. This sample therefore provides little evidence about prolonged negative funding. The strategy would pay when the rate turns negative. Two contiguous quarters on one instrument are a limited test of persistence.

## Execution and accounting

- All four trades in each window use actual archived Binance spot or USD-M five-level order books captured by Tardis. Entry and exit use marketable orders. Spot executes 250 ms after the scheduled decision, futures 1,250 ms after it. Each order uses the most recent locally observable book at its arrival. The replay rejects quotes older than five seconds, locked or crossed books, and orders exceeding 10% of displayed size across the five levels. A depth-weighted price includes the spread and any walk through the book. Base runs charge another 2 bp adversely on each order.
- Assumed taker fees are 10 bp on spot and 5 bp on futures. The spot buy fee is deducted in ETH. The purchase rounds up to a 0.0001 ETH lot so that the full 0.5 ETH remains available to hedge and sell. The small residual is written off. The spot sale and futures trades pay fees in USDT. Account-specific discounts, leverage rewards and rebates are excluded.
- The spot and futures wallets start with 5,000 USDT each. No borrowing or collateral transfers occur. Every open-position minute is checked using its highest futures mark and the less favorable within-minute funding balance. The run requires futures equity above 25% of short notional throughout. This is a collateral stress check, not a replica of the venue’s liquidation engine.
- Realized funding rates and times are cross-checked between Binance’s checksummed monthly archives and its REST history. The replay verifies a complete eight-hour calendar and holds the short well clear of each boundary. {exact} payments use a reported settlement mark. For the remaining {count - exact}, it values a receipt at the archived minute’s lowest mark and a debit at the highest. An exchange settlement mark can differ from its published mark stream. These payments are estimates.
- Candle data only values held positions and bounds missing settlement marks. Executions come from recorded depth. Displayed liquidity can disappear before a real order matches, and actual two-leg timing, fees, venue solvency, capital costs and USDT exposure remain risks.
- Net cash is calculated independently by liquidating both wallets and by adding funding and basis PnL, less commissions, adverse slippage and residual write-off. Across the base and stress runs, the largest absolute reconciliation error was {rec_error:.2e} USDT.

[Binance funding mechanics](https://www.binance.com/en-AE/support/faq/detail/360033525031) · [Binance public archives](https://github.com/binance/binance-public-data) · [Tardis normalized market data](https://docs.tardis.dev/downloadable-csv-files/data-types)

## Reproduce

From the repository root, use the installed research environment.

```powershell
python -m fundingcarry
python -m pytest -q
```

The command verifies the published source hashes, downloads missing data into the ignored data directory, and rebuilds this report. The machine-readable results include each fill, both fees and settlement cash flows. No exchange account is needed.
"""
    (out / "findings.md").write_text(note, encoding="utf8")
