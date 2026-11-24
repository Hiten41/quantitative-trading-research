"""Publish all figures and paper tables from the reconciled experiment."""

from __future__ import annotations

import json
from pathlib import Path

from plotly.offline import get_plotlyjs

TITLE = "Profitability and Risk of Delta-Neutral Funding Carry in Cryptocurrency Perpetual Futures: An Out-of-Sample Historical Study"


def fmt(v):
    if v is None:
        return "—"
    return f"{v:,.3f}" if isinstance(v, (float,)) else str(v)


def table(header, rows):
    return "\n".join(
        "| " + " | ".join(map(fmt, row)) + " |" for row in [header, ["---"] * len(header), *rows]
    )


def dashboard(dest, res, streams):
    # Inline data and Plotly make the file portable, even with networking disabled.
    charts = [
        "Net equity",
        "Settlement cash flows",
        "Spot–perpetual basis",
        "Cumulative funding",
        "Net PnL by window",
        "Funding and costs",
        "Drawdown",
        "Futures margin utilization",
        "Negative funding streak",
        "Sensitivity to assumptions",
        "Break-even gross receipts",
        "Liquidation and hedge-risk events",
    ]
    compact = {}
    for w in res["windows"]:
        policies = {}
        for pol in "ABC":
            series = {}
            for key, fields in {
                "curves": ["time", "equity", "drawdown", "funding", "margin", "basis_bps"],
                "settlements": ["time", "rate", "cashflow", "short", "mark", "source", "gap_ms"],
                "fills": ["time", "venue", "side", "qty"],
            }.items():
                rows = [r for r in streams[key] if r["window"] == w and r["policy"] == pol]
                series[key] = {f: [r[f] for r in rows] for f in fields}
            policies[pol] = series
        compact[w] = policies
    metrics = [
        {
            k: r[k]
            for k in (
                "window",
                "policy",
                "scenario",
                "net",
                "funding_received",
                "funding_paid",
                "total_cost",
                "drawdown",
                "holding_hours",
                "breakeven_gross_receipts",
            )
        }
        for r in res["metrics"]
    ]
    data = {
        "windows": res["windows"],
        "series": compact,
        "metrics": metrics,
        "risks": streams.get("risk_events", []),
    }
    tpl = (Path(__file__).with_name("study_dashboard.html")).read_text(encoding="utf8")
    sections = "".join(
        f'<section><h2>{title}</h2><div class="chart-controls"></div><p class="chart-note" hidden></p><div class="plot" id="plot{i}"></div></section>'
        for i, title in enumerate(charts)
    )
    html = (
        tpl.replace("<!--library-->", get_plotlyjs())
        .replace("<!--charts-->", sections)
        .replace(
            "<!--app-->", Path(__file__).with_name("study_dashboard.js").read_text(encoding="utf8")
        )
        .replace("<!--data-->", json.dumps(data, separators=(",", ":"), allow_nan=False))
    )
    (dest / "index.html").write_text(html, encoding="utf8")


def holdout_figure(dest, metrics):
    """A portable print figure using the same output rows as the interactive report."""
    rows = [
        r
        for r in metrics
        if r["window"].startswith("holdout") and r["scenario"] == "base" and r["policy"] == "A"
    ]
    keys = ["funding_net", "total_cost", "net"]
    names = ["Net funding", "Fees and execution", "Net PnL"]
    colors = ["#74d7c1", "#f1bc83", "#c4c9ff"]
    scale = 200 / max(abs(r[k]) for r in rows for k in keys)
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 270" role="img"><title>Fixed hedge funding, costs and net PnL in the two holdouts</title><rect width="800" height="270" fill="#131f30"/><g fill="#eef3fb" font-family="Segoe UI, Arial" font-size="15">',
        '<text x="20" y="30" font-size="20">Fixed hedge in the 2025 holdouts (USDT)</text>',
        '<path d="M450 48V245" stroke="#a6b2be"/>',
    ]
    for j, r in enumerate(rows):
        for i, (k, label, color) in enumerate(zip(keys, names, colors, strict=True)):
            y = 65 + j * 100 + i * 21
            val = r[k] * (-1 if k == "total_cost" else 1)
            x = min(450, 450 + val * scale)
            svg.append(
                f'<text x="20" y="{y + 9}">{r["window"] if i == 0 else ""}</text><text x="115" y="{y + 9}">{label}</text><rect x="{x}" y="{y - 4}" width="{abs(val) * scale}" height="15" fill="{color}"/><text x="735" y="{y + 9}" text-anchor="end">{val:+.2f}</text>'
            )
    svg.append("</g></svg>")
    (dest / "holdout-pnl.svg").write_text("".join(svg), encoding="utf8")


def publish(dest, result, streams, c):
    dashboard(dest, result, streams)
    holdout_figure(dest, result["metrics"])
    audits = result["regression"]
    rows = list(audits.values())
    keys = list(rows[0])
    audit_text = (
        """# Independent audit of the original ETH carry replay

Two fresh replays of the published configuration recovered the original results. The reproduction command checks the original input hashes and the complete numerical JSON, then rebuilds the cash journals and every intermediate hourly equity observation from the fills, archived rates, REST records and separate valuation bars.

"""
        + table(
            ["Value", "Primary", "Confirmation"],
            [
                [
                    k,
                    *[
                        f"{v[k]:.7f}" if k in ("buy_gross", "buy_fee_eth", "dust_eth") else v[k]
                        for v in rows
                    ],
                ]
                for k in keys
            ],
        )
        + """

All money amounts are USDT except the ETH quantities. Both wallets started at 5,000. The gross spot buy paid commission in ETH, retained 0.5 ETH for the hedge, and left sub-lot dust valued at zero. The short sold 0.5 linear ETHUSDT contracts measured in ETH equivalent. Spot sale fees and both futures fees were paid from the respective quote wallets. Every leg crosses recorded five-level depth at its own arrival time and pays an additional two basis points. The independent journal valued all 89 estimated payments at 4.604 USDT in total. Omitting these receipts left about 115.27 USDT combined. Switching from the adverse minute mark to the prior close changed combined PnL by about 0.0014 USDT. The two raw price PnLs already include the quoted spread. Subtracting another spread would duplicate a cost.

For each window the sum of spot and futures reference-price PnL plus signed funding, less both fees, slippage and dust, equals the sum of the terminal cash wallets minus 10,000. Both tradable positions close. The spot dust remains physically possible and is written off. Hourly spot close and previous completed minute futures mark produce the reported intermediate equity. The original 25% collateral-to-notional floor rejects unsupported paths and does not simulate liquidation.

Archive rates are decimal fractions per settlement. A short receives quantity × rate × settlement mark. UTC archive and REST timestamps align to the same eight-hour slots, including millisecond variations. All settlements fall strictly after the short fill and before its cover. The predetermined schedule consumes no funding signal. Both quarters happened to have exclusively positive rates. The dated REST records omit the mark for 89 events. The legacy calculation uses the adverse extremum of the settlement minute. That extremum becomes available after settlement. It affects valuation only. The independent causal alternative uses the last completed mark close. The lower/upper sensitivity uses the published minute range, conditional on that range representing the exchange's settlement mark. It cannot establish a guaranteed bound on an unpublished internal price. The new engine includes the causal estimate in its envelope.

The estimated-income row measures all income attributed to those payments. Subtract it from net for the same-path result excluding them. The causal-net and upper-net rows distinguish the uncertainty in their price from the much larger effect of removing those payments entirely. The combined-stress row doubles fees, raises additional slippage to 10 bp per leg and halves positive receipts. Without funding the two cash journals lose money in aggregate.

[Original execution, funding events and balances](results.json) · [Data provenance](sources.json) · [Expanded experiment](final/research.md)
"""
    )
    (dest.parent / "audit.md").write_text(audit_text, encoding="utf8")
    windows = result["windows"]
    base = [r for r in result["metrics"] if r["scenario"] == "base"]
    lookup = {(r["window"], r["policy"]): r for r in base}

    def matrix(keys):
        return table(
            ["Window", "Policy", *keys],
            [[r["window"], r["policy"], *[r[k] for k in keys]] for r in base],
        )

    comparison = table(
        [
            "Window",
            "Start UTC",
            "End UTC (exclusive)",
            "Negative / all",
            "Observed marks",
            "A",
            "B",
            "C",
        ],
        [
            [
                w,
                v["start"][:16],
                v["end"][:16],
                f"{v['negative']} / {v['settlements']}",
                v["observed"],
                *[lookup[w, p]["net"] for p in "ABC"],
            ]
            for w, v in windows.items()
        ],
    )
    hold = [r for r in base if r["window"].startswith("holdout")]

    def total(key, pol, values):
        return sum(r[key] for r in values if r["policy"] == pol)

    reg = result["regimes"]
    adverse = next(r for r in reg if r["month"] == windows["negative"]["start"][:7])
    mixed = next(r for r in reg if r["month"] == windows["mixed"]["start"][:7])
    grid = result["metrics"]
    stress = table(
        ["Scenario", "OOS A", "OOS B", "OOS C", "Losing runs / 24", "Simulated liquidations"],
        [
            [
                scenario,
                *[
                    total(
                        "net",
                        p,
                        [
                            r
                            for r in grid
                            if r["scenario"] == scenario and r["window"].startswith("holdout")
                        ],
                    )
                    for p in "ABC"
                ],
                sum(r["net"] < -1e-9 for r in grid if r["scenario"] == scenario),
                sum(r["liquidations"] for r in grid if r["scenario"] == scenario),
            ]
            for scenario in c["scenarios"]
        ],
    )
    liquidation = table(
        ["Window", "Scenario", "Policy", "Forced closes", "Net USDT"],
        [
            [r["window"], r["scenario"], r["policy"], r["liquidations"], r["net"]]
            for r in grid
            if r["liquidations"]
        ],
    )
    refs = "[Binance Public Data](https://github.com/binance/binance-public-data) · [Binance funding-history specification](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History) · [USD-M leverage brackets](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets)"
    receipt = total("funding_received", "A", hold)
    paid = total("funding_paid", "A", hold)
    holdcost = total("total_cost", "A", hold)
    peak = max(r["drawdown"] for r in base)
    estimates = table(
        [
            "Window (fixed policy)",
            "Estimated events",
            "PnL excluding estimated payments",
            "Lower net",
            "Causal net",
            "Upper net",
        ],
        [
            [
                w,
                rec["settlements"] - rec["observed"],
                lookup[w, "A"]["net"] - lookup[w, "A"]["estimated_funding"],
                *[
                    next(
                        r["net"]
                        for r in grid
                        if r["window"] == w and r["policy"] == "A" and r["scenario"] == sc
                    )
                    for sc in ("lower_mark", "base", "upper_mark")
                ],
            ]
            for w, rec in windows.items()
            if rec["settlements"] > rec["observed"]
        ],
    )
    text = f"""# {TITLE}

## 1. Abstract

The original two depth-priced regressions earned {sum(r["net"] for r in rows):.2f} USDT on separate 10,000 USDT allocations. On two frozen 2025 monthly holdouts the fixed 0.5 ETH hedge lost {abs(total("net", "A", hold)):.2f} USDT in aggregate. Both adaptive rules stayed in cash in these holdouts. The experiment finds funding income in favorable markets and no evidence of a consistently positive out-of-sample risk-adjusted return.

## 2. Introduction

A long spot holding offsets the directional exposure of a linear perpetual short of equal size. The short still faces the funding calendar, changes in the futures premium, separate collateral requirements and execution risk. These cash flows can exceed the small income earned from holding the hedge. [Interactive figures](index.html)

## 3. Research Questions

H1 is supported in the original quarters and some additional windows. H2 is supported by the selected adverse month. H3 has a qualified result. Abstention avoided losses in both holdouts and missed the positive development and validation months. H4 is supported by the zero-funding and break-even accounting below. H5 is supported by simulated liquidations under thinner collateral. H6 fails as a general proposition. Net outcomes change with the funding and execution assumptions. No probability of future profit is estimated.

## 4. Data Sources and Provenance

The discovery file covers {reg[0]["month"]} through {reg[-1]["month"]} ({len(reg)} available monthly ETHUSDT funding archives). Each ZIP was listed in the official bucket and verified against its exchange SHA-256 checksum. Market inputs are separate Binance Spot and USD-M trade klines, USD-M mark-price klines at one minute and actual archive/REST funding rates. Spot timestamps in newer archives use microseconds. Both time units are normalized to UTC. ZIP time grids, OHLC, calendar uniqueness and agreement of REST and archive rates are checked. The legacy regressions additionally use hourly spot closes and Tardis public historical Binance book snapshots. REST snapshots and depth files have separate content hashes. [Source manifest](sources.json) · {refs}

Where REST supplies markPrice we use it. Otherwise the point is the preceding completed minute mark close. The event export includes source, timestamp, gap in milliseconds, point and range. The conditional envelope spans the causal estimate and the published settlement minute low/high. Negative rates reverse the cash-flow ordering of the price endpoints. Estimates are especially important in 2022, when all selected settlement notionals required proxies. [Settlement ledger](settlements.csv.gz)

## 5. Historical Funding Regimes

{c["selection"]} The fixed development, validation and holdout dates are excluded from diagnostic ranking. The full-month calendar includes its opening midnight settlement, while the monthly strategy starts at 01:00. September 2022 had {adverse["negative_pct"]:.1f}% negative settlements, a {adverse["longest_negative"]}-event negative run, and cumulative rate {100 * adverse["cumulative"]:.3f}%. June 2022 had {mixed["negative_pct"]:.1f}% negative and a {mixed["longest_negative"]}-event run. Cumulative rates sum decimal rates without compounding or notional weighting. The monthly export shows every candidate and its summary statistics. [Regime scan](regimes.csv)

## 6. Strategy Formulation

A opens 0.5 ETH on each leg and holds until scheduled exit unless liquidated. B reviews hourly, uses the last nine settled rates with a one-minute publication delay, and extrapolates their mean for 42 settlements. Entry requires at least 0.5 bp per settlement and expected funding above 115% of modeled round-trip fees, half-spreads and slippage. Exit at a nonpositive mean after 24 hours, 4 USDT cumulative funding expense, or 720 hours. Cooldown is 48 hours. C also requires peak planned initial allocation below 50% of capital and absolute basis within 30 bp. It exits on a 22% maintenance-to-equity ratio, three recent negative rates, an excessive basis or a 15 USDT holding loss. There is no prediction feed or optimizer. Optional policies.B.signals, policies.C.signals and policies.C.risk overrides configure the adaptive rules independently. This study uses the common signal settings. [Frozen configuration](../../../configs/carry_study.json)

## 7. Execution and Transaction Costs

Expanded runs use separate venue trade bars for every policy, including on the regression calendars. The first order works over the decision minute and books at its close. The hedge works over a later minute. Aggressor-side volume and quote turnover form a side VWAP, floored adversely at the all-trade VWAP. We add 0.5 bp half-spread, 2 bp slippage per leg, outward 0.01 USDT tick rounding and 10/5 bp spot/perpetual fees in quote equivalent. Each 0.5 ETH order must satisfy a 0.001 ETH lot, 5 USDT notional and 2% of side volume. An unfilled order waits for another minute. These are modeled filters and fee assumptions. Minute averages cannot identify the actual order book or the queue. A forced cover uses at least the worse of the minute trade and mark highs plus slippage, bypasses participation and pays the liquidation penalty. Exact depth is used in the unchanged benchmark reproduction.

## 8. Portfolio Accounting

Spot cash pays the spot principal and fee. The futures wallet pays fees and includes realized short price PnL and signed funding. Total equity equals spot cash plus ETH at the spot close plus the futures wallet plus open short PnL at the USD-M mark plus any in-transit funds. Open and closed wallets are checked against reference-price attribution at every state transition. Within each unchanged-position interval, the two inventories are marked at each minute close. Transfers debit spendable cash at request and credit the futures wallet only on arrival. With no trade, returns on committed capital and profit factor are undefined. Committed capital is peak earmarked initial futures collateral plus requested transfers and open spot acquisition principal during a traded path. Allocated returns always use the scenario's full initial cash. Daily profit factor divides summed gains by absolute summed losses in successive 24-hour equity observations including the terminal stub.

## 9. Margin and Liquidation Modeling

Base futures collateral is 106% of initial target notional. Leverage scenarios divide that amount by 2, 3 or 5. Other capital starts in spot cash. The model uses 10% initial margin, 1% maintenance and a 0.5% liquidation penalty. Historical account-specific tiers and liquidation prints could not be verified. These configurable approximations apply to the small simulated position only. At every minute high the engine compares futures equity with maintenance plus the penalty reserve. A breach forces a short cover and an emergency spot sale if needed. It can happen before an hourly strategy review. Warnings start at a 50% maintenance/equity ratio, counted once per short. At 25% the strategy can request one 100 USDT transfer with an hour's delay. When a trigger coincides with a breach, liquidation takes priority. Within a settlement minute the model posts funding before testing the adverse high and books normal fills at close. Intraminute ordering remains uncertain.

{liquidation}

## 10. Experimental Design

Development was June 2023, validation July 2024, and the two chronological holdouts March and October 2025. The same configuration ran once across these dates with no return-based parameter selection. The two prior quarters are regression benchmarks. The 2022 months were selected retrospectively on funding and characterize risk, so they cannot estimate unconditional returns. The date range is exclusive at the end. Each monthly sample starts at 01:00 UTC and closes out near month end. For the expanded quarter simulations the scheduled exit decision is 15 minutes before the last modeled minute to accommodate the stressed leg delay. Each run starts with fresh capital.

{comparison}

## 11. Baseline Results

The separate depth-based reproduction yielded {rows[0]["net"]:.4f} and {rows[1]["net"]:.4f} USDT. The A column above shows the same calendar evaluated under the common minute execution model and collateral assumptions. The depth replay scheduled its exit at 11:55 UTC. The common minute model scheduled its exit at 11:40 UTC, used side-volume trade prices on successive minutes and charged the opening spot fee in quote currency. Using the prior completed mark for missing settlement prices changed the first quarter's funding by about 0.0014 USDT. Neither fixed-policy base case required a transfer or liquidation. All 549 original payments benefited the short. {sum(r["exact"] for r in rows)} marks were observed and {sum(r["estimated"] for r in rows)} estimated. Removing all funding gives {sum(r["no_funding"] for r in rows):.2f} USDT. [Independent audit and complete reconciliation](../audit.md)

## 12. Negative-Funding Regime Results

Across the eight nonoverlapping replay windows there were {sum(v["negative"] for v in windows.values())} distinct negative settlements. The adverse window included {windows["negative"]["negative"]} negative settlements and A earned {lookup["negative", "A"]["funding_net"]:.2f} USDT net funding. Its total net was {lookup["negative", "A"]["net"]:.2f}. B and C took no position in that window. In the mixed month the fixed hedge collected slightly positive net funding but incurred an overall loss. The time-varying ETH notional and the skipped opening settlement can change the sign relative to a full-month unweighted rate sum.

## 13. Funding-Aware Strategy Results

B's entry hurdle often kept the strategy in cash. It missed two positive fixed-policy months and avoided two losing holdouts. Its activity in the two favorable legacy quarters provides an in-sample comparison only. Repeated round trips raised expenses there.

## 14. Risk-Controlled Strategy Results

C matched B's base-case results across these windows. Its incremental controls showed no independent improvement on the frozen evaluation. The worst base-case minute-close portfolio drawdown was {peak:.2f} USDT. PnL divided by maximum drawdown describes each realized path. Flat paths have no defined ratio. Charts include hourly observations and the minute of each policy’s largest drawdown. The minute-high margin ratio uses maintenance divided by adverse futures equity, and minimum margin buffer is futures equity above maintenance at that high. [Full risk and performance metrics](metrics.csv)

{matrix(["drawdown", "pnl_to_drawdown", "max_margin_ratio", "min_margin_buffer", "warnings", "liquidations", "holding_hours"])}

## 15. PnL Attribution

Each row below reconciles as spot reference PnL + futures reference PnL + net funding − total cost. Basis and legging split the sum of the two price terms. Basis uses the futures VWAP synchronized to the spot fill minute, while legging captures the change to the later futures reference, including the reverse sequence in liquidation. These are explanatory subdivisions. Funding paid and costs are shown as positive expenses. Zero-funding removes cash flows on the identical realized trading path. With forced liquidations, that counterfactual does not re-simulate margin decisions.

{matrix(["spot_pnl", "futures_pnl", "basis_pnl", "legging", "funding_received", "funding_paid", "estimated_funding", "total_cost", "zero_funding", "net"])}

![Fixed hedge net funding, fees and execution, and net PnL for the two 2025 holdouts](holdout-pnl.svg)

In the two holdouts A received {receipt:.2f}, paid {paid:.2f} in funding and incurred {holdcost:.2f} USDT in execution and fees. The break-even gross receipt is total costs minus the two price PnLs plus funding paid. Gross funding exceeding commissions alone is insufficient.

On fixed-policy paths with missing marks, the following figures separate the effect of eliminating estimated payments from the smaller effect of repricing each payment within its conditional envelope. The remaining windows have observed settlement marks for every payment.

{estimates}

## 16. Robustness and Sensitivity

All scenarios rerun both decisions and wallets. Cost-sensitive hurdles can change positions. Receipt haircuts are hypothetical and preserve the actual signed funding signals and all debits. Margin rises to 5% in the higher-maintenance case. Reduced capital uses 3,000 USDT, reduced collateral uses 55% of the base reserve, slow hedging uses five minutes, and the closing stress quadruples additional closing slippage. Delayed transfers take 24 hours. Each row below shows the non-compounded sum of the two holdouts; the losing count covers all eight windows and three policies. The delayed 5x transfer scenario combines two stresses explicitly. The four additional exact-initial-exposure rows remove the 6% collateral reserve to test the literal 1x, 2x, 3x and 5x requested starting ratios. They are supplemental diagnostics and did not change any trading rule. [Supplemental margin settings](../../../configs/carry_margin_stress.json) [Every scenario and break-even point](metrics.csv) · [Sensitivity heatmap](index.html#plot9)

{stress}

## 17. Limitations

Two holdout months on one instrument offer little statistical power. The diagnostic months deliberately overrepresent adverse regimes. Fees, trade-bar executions, side-volume limits, collateral timing and liquidation are simulations. There is no yield on free cash, spot financing, clawback, liquidation queue, ADL, tax, venue default, market impact beyond the specified model or counterparty model. Using every minute close still misses intraminute portfolio drawdown. Public settlement marks are missing for early periods. Envelope endpoints use later observations for valuation sensitivity only. A stressed wallet rerun can change subsequent risk decisions; these envelope scenarios are retrospective sensitivities. The conditional bounds do not certify an unpublished exchange mark. No annualized Sharpe or statistical claim of alpha is warranted.

## 18. Conclusion

Positive funding supported the original two quarters. The frozen fixed policy lost money in both new holdouts. Avoiding exposure improved those outcomes but produced no positive holdout return. The holdout PnLs depend on fill prices estimated from one-minute trades. Those bars cannot establish the price or size available when each leg would have traded. Thinner futures collateral triggered simulated liquidations despite the matching spot position. The counts depend on assumed margin tiers and penalties and on testing each minute's high. Historical account-specific rules, intraminute ordering and liquidation fills could not be verified.

## 19. Reproducibility

From the repository root, install the pinned research environment and the editable package, then run `python scripts/reproduce_funding_carry.py`. Historical inputs are cached in `data/funding-carry`. The command verifies exchange checksums or saved content hashes, reproduces both original windows, runs the {len(grid)} policy/window/scenario combinations, audits the wallets and settlements, and writes the offline figures and tables. The run used {result["cached_input_bytes"] / 2**20:.1f} MiB of unique source files. Machine-specific runtime and library versions are in [study.json](study.json). [Input hashes](input_hashes.json) · [Funding](settlements.csv.gz) · [Fills](fills.csv.gz) · [Wallets](balances.csv.gz) · [Decisions](decisions.csv.gz) · [Hourly curves](curves.csv.gz) · [Stress events](risk_events.csv.gz)
"""
    (dest / "research.md").write_text(text, encoding="utf8")
    render_paper(dest / "research.html", TITLE, text, "index.html")
    render_paper(
        dest.parent / "audit.html", "Independent benchmark audit", audit_text, "final/index.html"
    )


def render_paper(path, title, source, dashboard_url):
    from html import escape

    import markdown

    source = source.replace("(../audit.md)", "(../audit.html)").replace(
        "(final/research.md)", "(final/research.html)"
    )
    body = markdown.markdown(source, extensions=["tables", "toc"])
    body = body.replace("<table>", '<div class="table-scroll" tabindex="0"><table>').replace(
        "</table>", "</table></div>"
    )
    style = """
    :root{color-scheme:light;font:17px/1.6 system-ui;color:#182636;background:#fbfcfc}
    body{max-width:82ch;margin:auto;padding:2rem clamp(1rem,5vw,4rem)}
    h1,h2{line-height:1.2;text-wrap:balance}h2{margin-top:2em}a{color:#12675e}
    img{max-width:100%}.table-scroll{overflow-x:auto}table{border-collapse:collapse;font-size:.9rem}
    th,td{padding:.35rem .65rem;border-bottom:1px solid #ccd5da;text-align:left}
    thead{background:#ecf3f2}code{overflow-wrap:anywhere}nav{margin-bottom:2rem}
    """
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{style}</style><body>"
        f'<nav><a href="{dashboard_url}">Research dashboard</a></nav><main>{body}</main></body></html>',
        encoding="utf8",
    )
