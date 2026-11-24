"""Static, offline Plotly publication. Never imports into the replay hot path."""

from __future__ import annotations

import csv
import gzip
import html
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from .engine import HORIZONS

LABEL = {"fixed": "Fixed spread", "adaptive": "Inventory skew", "no_skew": "Adaptive without skew"}
COLOR = {"fixed": "#246394", "adaptive": "#a44e25", "no_skew": "#348170"}
LABEL.update(
    anchored_maker="Anchored maker",
    selective_maker="Trend-filtered spread",
    pullback_maker="Passive trend",
)
COLOR.update(anchored_maker="#994267", selective_maker="#625186", pullback_maker="#277274")
METRICS = [
    ("net_pnl", "Net PnL · USDT"),
    ("gross_pnl", "Gross trading PnL · USDT"),
    ("fees", "Maker fees · USDT"),
    ("return_pct", "Return · %"),
    ("fill_count", "Fills"),
    ("order_fill_rate", "Submitted orders with a fill · %"),
    ("quantity_fill_rate", "Submitted quantity filled · %"),
    ("traded_notional", "Traded notional · USDT"),
    ("avg_abs_inventory", "Time-average |inventory| · ETH"),
    ("max_abs_inventory", "Maximum |inventory| · ETH"),
    ("utilization", "Inventory utilization · %"),
    ("max_drawdown", "Maximum drawdown · USDT"),
    ("pnl_per_rms_inventory", "PnL / RMS inventory · USDT per ETH"),
    ("spread_capture", "Immediate capture · USDT"),
    ("realized_gross", "Realized before fees · USDT"),
    ("unrealized", "Terminal unrealized · USDT"),
    ("terminal_inventory", "Terminal inventory · ETH"),
]


def date_time(ns):
    return datetime.fromtimestamp((int(ns) // 1_000_000) / 1000, UTC).isoformat(
        timespec="milliseconds"
    )


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def records(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return [
            {
                k: (int(v) if k in ("ts", "side", "order", "trade_id") else float(v)) if v else None
                for k, v in r.items()
            }
            for r in csv.DictReader(f)
        ]


def fmt(v):
    return "n/a" if v is None else f"{v:,.4f}".rstrip("0").rstrip(".")


def comparison(metrics, base="ETH"):
    rows = []
    for k, label in METRICS:
        values = []
        for m in metrics.values():
            v = m[k]
            if v is not None and k in ("order_fill_rate", "quantity_fill_rate", "utilization"):
                v *= 100
            values.append(fmt(v))
        rows.append([label.replace("ETH", base), *values])
    for h in HORIZONS:
        rows.append(
            [
                f"{h}s directional markout · USDT",
                *[fmt(m["markouts"]["all"][str(h)]["directional"]) for m in metrics.values()],
            ]
        )
    return rows


def table(headers, rows):
    def row(items, tag):
        return "<tr>" + "".join(f"<{tag}>{html.escape(str(x))}</{tag}>" for x in items) + "</tr>"

    return (
        "<div class='scroll'><table><thead>"
        + row(headers, "th")
        + "</thead><tbody>"
        + "".join(row(r, "td") for r in rows)
        + "</tbody></table></div>"
    )


def markdown_table(headers, rows):
    def row(vals):
        return "| " + " | ".join(str(v).replace("|", "\\|") for v in vals) + " |"

    return "\n".join([row(headers), row(["---"] * len(headers)), *(row(r) for r in rows)])


class ReportGenerator:
    def __init__(self, run: Path, output: Path):
        self.run = run
        self.out = output
        self.config = read(run / "config.json")
        self.quality = read(run / "quality.json")
        self.base = self.quality["contract"]["baseAsset"]
        rates = self.quality["funding_records"]
        if self.quality["funding_included"]:
            self.funding_note = "Funding uses the published rates and settlement marks."
        elif rates and all(not r.get("markPrice") for r in rates):
            self.funding_note = f"Binance returned {len(rates)} funding rates with empty settlement marks. Funding is excluded."
        else:
            self.funding_note = "Funding is excluded because the available records did not pass the completeness check."
        self.execution_note = {
            "through": "Strict price-through fills",
            "queue": "Price-through and approximate queue fills",
            "none": "No-fill diagnostic",
        }[self.config["fill_model"]]
        self.metrics = read(run / "evaluation" / "metrics.json")
        self.samples = {}
        self.fills = {}
        self.daily = {}
        for n in self.metrics:
            folder = run / "evaluation"
            self.samples[n] = records(folder / (n + "-samples.csv.gz"))
            self.fills[n] = records(folder / (n + "-fills.csv.gz"))
            self.daily[n] = read(folder / (n + "-daily.json"))
        p = run / "sensitivity.json"
        self.sensitivity = read(p) if p.exists() else []
        self.headers = ["Evaluation metric", *[LABEL[n] for n in self.metrics]]

    def charts(self):
        panels = []

        def panel(title, caption, fig, unit):
            fig.update_layout(
                template="plotly_white",
                height=600 if unit == "Scenario" else 340,
                margin={"l": 65, "r": 20, "t": 12, "b": 60},
                font={"family": "Arial, sans-serif", "size": 12, "color": "#243646"},
                yaxis_title=unit.replace("ETH", self.base),
                legend={"orientation": "h", "y": -0.22},
                hovermode="closest",
                paper_bgcolor="#ffffff",
            )
            key = "fig-" + str(len(panels))
            plot = fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                div_id=key,
                config={"responsive": True, "displaylogo": False, "scrollZoom": False},
            )
            panels.append(f"<section><h2>{title}</h2><p>{caption}</p>{plot}</section>")

        for field, title, unit in (
            ("equity", "Marked net PnL", "USDT"),
            ("inventory", "Signed inventory", "ETH"),
            ("drawdown", "Drawdown", "USDT below peak"),
        ):
            fig = go.Figure()
            for n, rows in self.samples.items():
                y = []
                peak = self.config["capital"]
                for r in rows:
                    peak = max(peak, r["equity"])
                    y.append(
                        r["equity"] - self.config["capital"]
                        if field == "equity"
                        else peak - r["equity"]
                        if field == "drawdown"
                        else r["inventory"]
                    )
                fig.add_scatter(
                    x=[date_time(r["ts"]) for r in rows],
                    y=y,
                    name=LABEL[n],
                    mode="lines",
                    line={"color": COLOR[n], "width": 1.5},
                )
            panel(
                title,
                "One-minute observations and UTC closes. The summary drawdown uses every book update and fill.",
                fig,
                unit,
            )
        fig = go.Figure()
        n = next(iter(self.metrics))
        samples = self.samples[n]
        fig.add_scatter(
            x=[date_time(r["ts"]) for r in samples],
            y=[r["mid"] for r in samples],
            name="Observed mid",
            mode="lines",
            line={"color": "#647280", "width": 1},
        )
        for n, rows in self.fills.items():
            every = max(1, math.ceil(len(rows) / 1400))
            shown = rows[::every]
            fig.add_scatter(
                x=[date_time(r["ts"]) for r in shown],
                y=[r["price"] for r in shown],
                name=LABEL[n],
                mode="markers",
                marker={
                    "size": 4,
                    "color": COLOR[n],
                    "opacity": 0.65,
                    "symbol": ["triangle-up" if r["side"] == 1 else "triangle-down" for r in shown],
                },
            )
        panel(
            "Executed prices",
            "Upward triangles are buys. Downward triangles are sells. Markers are systematically thinned to at most 1,400 per strategy.",
            fig,
            "USDT / ETH",
        )
        fig = go.Figure()
        for n, rows in self.samples.items():
            rows = rows[:120]
            for k in (
                ("bid", "ask", "quote_bid", "quote_ask")
                if n == next(iter(self.metrics))
                else ("quote_bid", "quote_ask")
            ):
                fig.add_scatter(
                    x=[date_time(r["ts"]) for r in rows],
                    y=[r[k] for r in rows],
                    name=(LABEL[n] + " " if "quote" in k else "Market ") + k,
                    mode="lines+markers",
                    connectgaps=False,
                    marker={"size": 3},
                    line={
                        "width": 1.2,
                        "dash": "dot" if k.endswith("ask") else "solid",
                        "color": COLOR[n] if "quote" in k else "#87949b",
                    },
                )
        panel(
            "Resting quotes and market touch",
            "First two hours at the sampling clock. Gaps indicate a side had no active quote at that observation.",
            fig,
            "USDT / ETH",
        )
        fig = go.Figure()
        for n, m in self.metrics.items():
            for field, label in (
                ("immediate", "Capture"),
                ("directional", "Directional"),
                ("combined", "Combined"),
                ("after_fee", "After fee"),
            ):
                fig.add_bar(
                    x=[f"{h}s" for h in HORIZONS],
                    y=[m["markouts"]["all"][str(h)][field] for h in HORIZONS],
                    name=LABEL[n] + " · " + label,
                    legendgroup=n,
                )
        panel(
            "Execution markouts",
            "Each bar sums the same horizon’s eligible fills. Missing future marks are omitted. A negative directional value indicates adverse selection.",
            fig,
            "USDT",
        )
        fig = go.Figure()
        for n, rows in self.fills.items():
            fig.add_bar(
                x=["Buy", "Sell"],
                y=[sum(r["side"] == s for r in rows) for s in (1, -1)],
                name=LABEL[n],
                marker_color=COLOR[n],
            )
        panel("Executed fills by side", "Each partial execution counts once.", fig, "Fill count")
        fig = go.Figure()
        for n, rows in self.daily.items():
            fig.add_bar(
                x=[date_time(r["ts"])[:10] for r in rows],
                y=[r["pnl"] for r in rows],
                name=LABEL[n],
                marker_color=COLOR[n],
            )
        panel(
            "Daily PnL",
            "Changes in marked equity from one UTC close to the next. Open inventory carries overnight.",
            fig,
            "USDT",
        )
        if self.sensitivity:
            scores = defaultdict(float)
            for r in self.sensitivity:
                case = r["parameter"] + (
                    " = " + str(r["value"]) if r["parameter"] != "reference" else ""
                )
                scores[(case, r["strategy"])] += r["net_pnl"]
            cases = list(dict.fromkeys(k for k, n in scores))
            names = ["fixed", "adaptive"]
            fig = go.Figure(
                go.Heatmap(
                    x=[LABEL[n] for n in names],
                    y=cases,
                    z=[[scores.get((k, n)) for n in names] for k in cases],
                    colorscale="RdBu",
                    zmid=0,
                    colorbar={"title": "USDT"},
                    hovertemplate="%{y}<br>%{x}: %{z:.4f} USDT<extra></extra>",
                )
            )
            fig.update_yaxes(automargin=True)
            panel(
                "Parameter sensitivity",
                "Each scenario replays the entire continuous evaluation period from flat. Each row changes one parameter. Blank cells have no effect on that policy.",
                fig,
                "Scenario",
            )
        return panels

    def generate(self):
        self.out.mkdir(parents=True, exist_ok=True)
        m = self.metrics
        title = "Market Making Under Inventory Risk"
        counts = {
            k: sum(f["events"] for f in self.quality["files"] if f["kind"] == k)
            for k in ("trades", "bookTicker")
        }
        cfg = self.config
        cards = "".join(
            f"<article><span>{LABEL[n]}</span><strong>{v['net_pnl']:+,.2f}</strong><small>USDT net PnL</small></article>"
            for n, v in m.items()
        )
        report = (
            f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title><style>"
            + STYLE
            + "</style><script>"
            + get_plotlyjs()
            + "</script></head><body><main>"
            + f"<header><p>Execution research &nbsp; / &nbsp; {cfg['symbol']} perpetual</p><h1>{title}</h1><p>An event-driven cryptocurrency backtesting engine</p></header>"
            + f"<p class='intro'>Held-out replay from {cfg['evaluation_start']} through {cfg['end']} UTC. Each policy starts flat with {cfg['capital']:,.0f} USDT. Terminal inventory is marked at the last observed midpoint.</p><div class='cards'>{cards}</div>"
            + "<section><h2>Strategy comparison</h2>"
            + table(self.headers, comparison(m, self.base))
            + "</section>"
            + "".join(self.charts())
            + "<section><h2>Methodology and limitations</h2>"
            + f"<p>The complete dataset contains {counts['trades']:,} individual trades and {counts['bookTicker']:,} L1 updates. Strategies observe books at publication time. The venue tests post-only orders at transaction time. Prints precede new quotes and acknowledgements at equal times. {self.execution_note} use a {cfg['participation']:.0%} print-volume cap and lot rounding. The displayed queue and the effect of inserting our orders are unobservable.</p>"
            + f"<p>Maker fees are a scenario assumption of {cfg['maker_fee_bps']} bp per side. {self.funding_note} Drawdown uses all marked equity changes. PnL per unit risk divides by time-weighted RMS base inventory. Horizon markouts overlap and cannot be added to portfolio PnL. The short interval limits inference across market regimes.</p></section></main></body></html>"
        )
        (self.out / "index.html").write_text(report, encoding="utf-8")
        self.preview()
        if len(m) == 3:
            from .paper import publish

            publish(self)
        return self.out / "index.html"

    def preview(self):
        w, h = 800, 220
        series = {
            n: [(r["ts"], r["equity"] - self.config["capital"]) for r in rows]
            for n, rows in self.samples.items()
        }
        all_points = [p for s in series.values() for p in s]
        t0, t1 = min(t for t, v in all_points), max(t for t, v in all_points)
        lo, hi = min(v for t, v in all_points), max(v for t, v in all_points)

        def y(v):
            return 36 + (hi - v) / max(hi - lo, 1e-12) * 140

        svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"><rect width="100%" height="100%" fill="white"/><g font-family="Arial" font-size="12" fill="#304454"><text x="12" y="20">Held-out marked PnL (USDT)</text>'
        ]
        for v in (hi, lo):
            svg.append(
                f'<text x="6" y="{y(v) + 4}">{v:.1f}</text><path d="M48 {y(v)}h730" stroke="#d8dfe2"/>'
            )
        for i, (n, s) in enumerate(series.items()):
            points = " ".join(
                f"{50 + (t - t0) / max(1, t1 - t0) * 720:.1f},{y(v):.1f}" for t, v in s
            )
            svg.append(
                f'<polyline points="{points}" fill="none" stroke="{COLOR[n]}" stroke-width="1.6"/><text x="{80 + 215 * i}" y="205" fill="{COLOR[n]}">{LABEL[n]}</text>'
            )
        (self.out / "pnl.svg").write_text("".join(svg) + "</g></svg>", encoding="utf-8")


STYLE = """*{box-sizing:border-box}body{margin:0;background:#f2f4f2;color:#1c3340;font:15px/1.55 Arial,sans-serif}
main{max-width:1140px;margin:auto;padding:32px 4vw 64px}header{border-top:5px solid #17665e;padding:25px 0 10px}
header p:first-child{font-size:13px;color:#17665e;font-weight:700}h1,h2{font-family:Georgia,serif;font-weight:500;line-height:1.12}
h1{font-size:clamp(38px,5.5vw,70px);margin:12px 0}h2{font-size:28px;margin:0 0 14px}.intro{max-width:780px;font-size:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}.cards article{background:#183e43;color:white;flex:1;min-width:155px;padding:16px 20px}
strong,small{display:block}strong{font:36px Georgia,serif}small{color:#c4ddd7}section{background:white;border:1px solid #d9e1de;margin:18px 0;padding:22px}
section p{color:#475c65;margin:0 0 14px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:7px;border-bottom:1px solid #e1e8e4;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:#15594f}
@media(max-width:600px){main{padding:12px}section{padding:12px}.cards article{min-width:100px}strong{font-size:25px}}"""
