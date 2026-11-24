"""Shared publication layout and browsable views of the study's exported evidence."""

from __future__ import annotations

import base64
import csv
import gzip
import json
import re
from html import escape
from pathlib import Path

import markdown


def asset(name):
    return Path(__file__).with_name(name).read_text(encoding="utf8")


def navigation(active, prefix=""):
    links = (
        ("dashboard", "Dashboard", "index.html"),
        ("paper", "Research paper", "research.html"),
        ("audit", "Benchmark audit", "../audit.html"),
        ("data", "Data explorer", "data.html"),
    )
    items = []
    for key, label, href in links:
        current = ' aria-current="page"' if key == active else ""
        items.append(f'<a href="{prefix}{href}"{current}>{label}</a>')
    return (
        '<div class="sitebar"><span class="site-brand"><i aria-hidden="true"></i>'
        'Quantitative Trading Research</span><nav aria-label="Study navigation">'
        + "".join(items)
        + "</nav></div>"
    )


def page(title, active, body, prefix="", script=""):
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width, initial-scale=1"><title>{escape(title)} · ETH research</title>'
        f'<style>{asset("study_site.css")}</style></head><body class="publication">'
        f"{navigation(active, prefix)}<main>{body}</main>{script}</body></html>"
    )


# Paths are relative to their individual publication; downloads retain the original files.
PAPER_LINKS = {
    "sources.json": "sources",
    "settlements.csv.gz": "settlements",
    "regimes.csv": "regimes",
    "../../../configs/carry_study.json": "rules",
    "metrics.csv": "metrics",
    "../../../configs/carry_margin_stress.json": "margin-rules",
    "study.json": "run",
    "input_hashes.json": "hashes",
    "fills.csv.gz": "fills",
    "balances.csv.gz": "balances",
    "decisions.csv.gz": "decisions",
    "curves.csv.gz": "curves",
    "risk_events.csv.gz": "risk",
}


def reader(path, source, result, audit=False):
    prefix = "final/" if audit else ""
    if audit:
        for marker, heading in (
            ("| Value |", "Cash reconciliation"),
            ("All money amounts", "Fees and closing balances"),
            ("Archive rates are", "Funding verification"),
            ("The estimated-income row", "Sensitivity to estimated marks"),
        ):
            source = source.replace(marker, "## " + heading + "\n\n" + marker, 1)
        links = {"results.json": "benchmarks", "sources.json": "benchmark-sources"}
    else:
        links = PAPER_LINKS
    for href, view in links.items():
        source = source.replace("](" + href + ")", "](" + prefix + "data.html#" + view + ")")
    source = source.replace("(../audit.md)", "(../audit.html)").replace(
        "(final/research.md)", "(final/research.html)"
    )
    title, rest = source.strip().split("\n", 1)
    title = title.removeprefix("# ")
    engine = markdown.Markdown(extensions=["tables", "toc"])
    text = engine.convert(rest)
    chapters = engine.toc_tokens
    toc = "".join(f'<a href="#{c["id"]}">{escape(c["name"])}</a>' for c in chapters)
    picks = "".join(f'<option value="{c["id"]}">{escape(c["name"])}</option>' for c in chapters)
    text = re.sub(r"(<h2 id=)", r"</section><section>\1", text)
    text = ("<section>" + text + "</section>").replace("<section></section>", "")
    text = text.replace(
        "<table>",
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Research table"><table>',
    ).replace("</table>", "</table></div>")
    table_labels = {
        "net": "Net PnL",
        "spot_raw": "Gross spot PnL",
        "perp_raw": "Gross futures PnL",
        "slip": "Slippage",
        "end_spot": "Closing spot wallet",
        "end_perp": "Closing futures wallet",
        "exact": "Observed marks",
        "estimated": "Estimated marks",
        "estimated_income": "Income from estimated marks",
        "no_funding": "PnL without funding",
        "positive": "Positive settlements",
        "negative": "Negative settlements",
    }

    def field_label(match):
        tag, key = match.groups()
        label = table_labels.get(key, key.replace("_", " ").capitalize())
        return f"<{tag}>{escape(label)}</{tag}>"

    text = re.sub(r"<(th)>([a-z][a-z_]+)</th>", field_label, text)
    if audit:
        text = re.sub(r"<(td)>([a-z][a-z_]+)</td>", field_label, text)
    regs = list(result["regression"].values())
    fixed = sum(
        r["net"]
        for r in result["metrics"]
        if r["scenario"] == "base" and r["policy"] == "A" and r["window"].startswith("holdout")
    )
    combined = sum(r["net"] for r in regs)
    stats = [("Original quarters", f"{combined:+.2f} USDT", "gain")]
    if audit:
        stats.extend(
            [
                ("Funding settlements", str(sum(r["positive"] + r["negative"] for r in regs)), ""),
                ("Estimated marks", str(sum(r["estimated"] for r in regs)), ""),
            ]
        )
    else:
        stats.extend(
            [
                (
                    "Fixed hedge · 2025 holdouts",
                    f"{fixed:+.2f} USDT",
                    "loss" if fixed < 0 else "gain",
                ),
                ("Historical windows", str(len(result["windows"])), ""),
            ]
        )
    cards = "".join(
        f'<div><small>{escape(k)}</small><strong class="{c}">{escape(v)}</strong></div>'
        for k, v, c in stats
    )
    if audit:
        scale = max(row["funding"] for row in regs)
        comparisons = []
        for window, row in result["regression"].items():
            lines = "".join(
                f'<div class="bar-row"><span>{label}</span><span class="track"><i style="width:{100 * row[key] / scale:.3f}%"></i></span><b>{row[key]:.2f}</b></div>'
                for key, label in (("funding", "Net funding"), ("net", "Net PnL"))
            )
            comparisons.append(
                f'<div class="quarter"><strong>{escape(window)}</strong>{lines}</div>'
            )
        figure = (
            '<figure class="bridge"><figcaption>Funding and final PnL <small>USDT</small></figcaption>'
            + "".join(comparisons)
            + "</figure>"
        )
        text = text.replace('<div class="table-scroll"', figure + '<div class="table-scroll"', 1)
    body = f"""<header class="mast"><p class="context">ETHUSDT <span>/</span> {"Benchmark audit" if audit else "Research paper"}</p>
<h1>{escape(title)}</h1><div class="stats">{cards}</div></header>
<div class="reader-grid"><aside class="contents"><h2>Contents</h2><nav aria-label="On this page">{toc}</nav></aside>
<div class="reader"><label class="chapter-picker">Jump to section <select id="chapter">{picks}</select></label><article>{text}</article></div></div>"""
    script = """<script>
const links=[...document.querySelectorAll('.contents a')],picker=document.querySelector('#chapter');
picker.onchange=()=>{location.hash=picker.value};
let waiting=false;
function active(){waiting=false;let current=links[0];for(const a of links){if(document.getElementById(a.hash.slice(1)).getBoundingClientRect().top<135)current=a;}
if(current){for(const a of links){if(a===current)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current');}
picker.value=current.hash.slice(1);}}
addEventListener('scroll',()=>{if(!waiting){waiting=true;requestAnimationFrame(active);}}, {passive:true});active();
</script>"""
    path.write_text(
        page(title, "audit" if audit else "paper", body, prefix, script), encoding="utf8"
    )


def read_json(path):
    return json.loads(path.read_bytes())


def settings(data, prefix=""):
    for name, value in data.items():
        key = f"{prefix} / {name}" if prefix else name
        if isinstance(value, dict) and value:
            yield from settings(value, key)
        else:
            yield {
                "setting": key,
                "value": json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else value,
            }


def evidence(dest):
    datasets = {}

    def add(key, title, file, rows, visible, group):
        rows = list(rows)
        fields = list(dict.fromkeys(k for r in rows for k in r))
        matrix = [[row.get(k) for k in fields] for row in rows]
        payload = json.dumps(matrix, separators=(",", ":"), ensure_ascii=False).encode()
        datasets[key] = {
            "title": title,
            "file": file,
            "fields": fields,
            "visible": visible.split(),
            "group": group,
            "count": len(rows),
            "payload": base64.b64encode(gzip.compress(payload, mtime=0)).decode(),
        }

    for key, title, file, cols, group in (
        (
            "metrics",
            "Policy performance",
            "metrics.csv",
            "window policy scenario net funding_net total_cost drawdown",
            "Results",
        ),
        (
            "regimes",
            "Funding regimes",
            "regimes.csv",
            "month count positive_pct negative_pct longest_negative cumulative",
            "Results",
        ),
        (
            "settlements",
            "Funding settlements",
            "settlements.csv.gz",
            "window policy time rate mark cashflow source",
            "Ledgers",
        ),
        (
            "fills",
            "Trade fills",
            "fills.csv.gz",
            "window policy time venue side qty price fee",
            "Ledgers",
        ),
        (
            "balances",
            "Wallet balances",
            "balances.csv.gz",
            "window policy time spot_cash futures_wallet equity",
            "Ledgers",
        ),
        (
            "decisions",
            "Trading decisions",
            "decisions.csv.gz",
            "window policy time action last_signal",
            "Ledgers",
        ),
        (
            "curves",
            "Equity and drawdown",
            "curves.csv.gz",
            "window policy time equity drawdown funding",
            "Ledgers",
        ),
        (
            "risk",
            "Stress events",
            "risk_events.csv.gz",
            "window policy scenario time event",
            "Ledgers",
        ),
    ):
        path = dest / file
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf8", newline="") as stream:
            add(key, title, file, csv.DictReader(stream), cols, group)
    for key, title, file in (
        ("sources", "Study sources", "sources.json"),
        ("benchmark-sources", "Benchmark sources", "../sources.json"),
    ):
        add(key, title, file, read_json(dest / file), "kind path sha256", "Provenance")
    hashes = read_json(dest / "input_hashes.json")
    add(
        "hashes",
        "Input hashes",
        "input_hashes.json",
        ({"source": url, "sha256": sha} for url, sha in hashes.items()),
        "source sha256",
        "Provenance",
    )
    for key, title, file in (
        ("rules", "Trading rules", "../../../configs/carry_study.json"),
        ("margin-rules", "Margin scenarios", "../../../configs/carry_margin_stress.json"),
    ):
        add(key, title, file, settings(read_json(dest / file)), "setting value", "Methodology")
    report = read_json(dest / "study.json")
    meta = {k: v for k, v in report.items() if k not in ("metrics", "regimes", "regression")}
    add("run", "Run details", "study.json", settings(meta), "setting value", "Methodology")
    original = read_json(dest.parent / "results.json")["windows"]
    cases = []
    fills = []
    flows = []
    for window, record in original.items():
        for case, values in record["cases"].items():
            cases.append(
                {
                    "window": window,
                    "scenario": case,
                    **{k: v for k, v in values.items() if k != "fills"},
                }
            )
            fills.extend({"window": window, "scenario": case, **r} for r in values["fills"])
        flows.extend({"window": window, **r} for r in record["funding_events"])
    for key, title, rows, cols in (
        (
            "benchmarks",
            "Benchmark reconciliation",
            cases,
            "window scenario net_pnl funding spot_and_futures_fees",
        ),
        ("benchmark-fills", "Benchmark fills", fills, "window scenario venue side quantity price"),
        (
            "benchmark-funding",
            "Benchmark settlements",
            flows,
            "window utc rate mark cashflow source",
        ),
    ):
        add(key, title, "../results.json", rows, cols, "Original benchmarks")
    body = asset("study_evidence.html")
    payload = json.dumps(datasets, separators=(",", ":")).replace("<", "\\u003c")
    script = f'<script type="application/json" id="catalog">{payload}</script><script>{asset("study_evidence.js")}</script>'
    (dest / "data.html").write_text(
        page("Study data explorer", "data", body, script=script), encoding="utf8"
    )
