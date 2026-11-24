"""Reproducible study orchestration. Every strategy shares each loaded tape."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import os
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from .analytics import PerformanceAnalyzer, paired_blocks
from .data.feed import HistoricalDataFeed
from .data.pipeline import save_json
from .engine import EventEngine

NAMES = ("fixed", "adaptive", "no_skew")
GRID = {
    "gamma": (0.2, 2.0),
    "spread_bps": (1.0, 6.0),
    "inventory_limit": (0.2, 1.0),
    "new_latency_ms": (10, 250),
    "cancel_latency_ms": (20, 400),
    "maker_fee_bps": (0.0, 2.0),
    "participation": (0.02, 0.25),
    "vol_window_s": (60, 600),
    "refresh_ms": (1000, 10000),
}


def write_rows(path, rows, fields):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def study_id(config, manifest, names):
    identity = {
        "config": asdict(config),
        "files": [f["parquet_sha256"] for f in manifest["files"]],
        "funding": manifest["funding_records"],
        "funding_included": manifest["funding_included"],
        "contract": manifest["contract"],
        "strategies": names,
    }
    # Include source code so a changed simulator cannot reuse an old result.
    h = hashlib.sha256(json.dumps(identity, sort_keys=True).encode())
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        if path.name in {"report.py", "paper.py", "cli.py", "__main__.py"}:
            continue
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return config.symbol.lower() + "-" + h.hexdigest()[:12]


def phase(feed, days, c, names, folder):
    folder.mkdir()
    engines = {}
    with ExitStack() as stack:
        for name in names:
            writer = csv.writer(
                stack.enter_context(
                    gzip.open(
                        folder / (name + "-orders.csv.gz"), "wt", encoding="utf-8", newline=""
                    )
                )
            )
            writer.writerow(["ts", "id", "side", "price", "lots", "remaining", "event"])

            def audit(t, o, action, w=writer):
                w.writerow((t, o.id, o.side, o.price, o.lots, o.remaining, action))

            engines[name] = EventEngine(c, name, audit)
        for date in days:
            logging.getLogger(__name__).info("%s: replaying %s", folder.name, date)
            day = feed.load(date)
            for e in engines.values():
                e.replay(day)
            del day
        results = {}
        for name, e in engines.items():
            e.finish()
            results[name] = PerformanceAnalyzer.summarize(e)
            write_rows(
                folder / (name + "-fills.csv.gz"),
                e.fill_rows(),
                [
                    "ts",
                    "order",
                    "trade_id",
                    "side",
                    "price",
                    "qty",
                    "mid",
                    "fee",
                    "inventory",
                    "cash",
                    "capture",
                    "markout_1s",
                    "markout_5s",
                    "markout_30s",
                    "markout_60s",
                ],
            )
            write_rows(folder / (name + "-samples.csv.gz"), e.samples, e.samples[0].keys())
            save_json(folder / (name + "-daily.json"), e.daily)
        if "adaptive" in engines and "fixed" in engines:
            save_json(
                folder / "paired-blocks.json",
                paired_blocks(engines["fixed"], engines["adaptive"], c.capital),
            )
    save_json(folder / "metrics.json", results)


def sensitivity(feed, c, days):
    """Reprice each one-factor scenario on the entire continuous evaluation period."""
    scenarios = [("reference", 0, c)] + [
        (p, v, replace(c, **{p: v})) for p, vs in GRID.items() for v in vs
    ]
    rows = []
    for param, value, setting in scenarios:
        names = ("adaptive",) if param in ("vol_window_s", "gamma") else ("fixed", "adaptive")
        engines = [EventEngine(setting, name) for name in names]
        logging.getLogger(__name__).info("sensitivity %s=%s", param, value)
        for date in days:
            day = feed.load(date)
            for e in engines:
                e.replay(day)
            del day
        for e in engines:
            e.finish()
            m = PerformanceAnalyzer.summarize(e)
            rows.append(
                {
                    "start": days[0],
                    "end": days[-1],
                    "parameter": param,
                    "value": value,
                    "strategy": e.name,
                    "net_pnl": m["net_pnl"],
                    "fees": m["fees"],
                    "fills": m["fill_count"],
                    "rms_inventory": m["rms_inventory"],
                    "max_drawdown": m["max_drawdown"],
                    "daily": e.daily,
                }
            )
    return rows


def run(config, root, runs, names=NAMES):
    feed = HistoricalDataFeed(root, config)
    identifier = study_id(config, feed.manifest, names)
    runs.mkdir(parents=True, exist_ok=True)
    target = runs / identifier
    if (target / "complete.json").exists():
        return target
    temp = Path(tempfile.mkdtemp(dir=runs, prefix=".working-"))
    try:
        save_json(temp / "config.json", asdict(config))
        save_json(temp / "quality.json", feed.manifest)
        calibration = [d for d in feed.days if d < config.evaluation_start]
        evaluation = [d for d in feed.days if d >= config.evaluation_start]
        phase(feed, calibration, config, names, temp / "calibration")
        phase(feed, evaluation, config, names, temp / "evaluation")
        if len(names) > 1:
            save_json(temp / "sensitivity.json", sensitivity(feed, config, evaluation))
        save_json(temp / "complete.json", {"run_id": identifier, "strategies": names})
        try:
            os.rename(temp, target)
        except OSError:
            if not (target / "complete.json").exists():
                raise
        return target
    finally:
        if temp.exists():
            shutil.rmtree(temp)
