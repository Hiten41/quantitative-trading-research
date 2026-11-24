"""Select on the original five days and replay a locked policy on the next five."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from mmbacktest.analytics import PerformanceAnalyzer
from mmbacktest.config import Config
from mmbacktest.data.feed import HistoricalDataFeed
from mmbacktest.data.pipeline import DAY, save_json, utc_ns
from mmbacktest.engine import EventEngine
from mmbacktest.experiment import phase, study_id

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/adaptation"
TRAIN = ("2023-09-02", "2023-09-06")
TEST = ("2023-09-07", "2023-09-11")
CANDIDATES = ("selective_maker", "pullback_maker", "anchored_maker")
HALF_LIVES = (1800, 7200)
WIDTHS = (4.0, 10.0)
CLOSE_FEE_BPS = 5.0


def config():
    return replace(
        Config.load(ROOT / "configs/default.yaml"), evaluation_start=TEST[0], end=TEST[1]
    )


def unwind(q, b):
    price = b.bid if q >= 0 else b.ask
    return abs(q) * (abs(b.mid - price) + price * CLOSE_FEE_BPS / 10000)


def select():
    c = config()
    feed = HistoricalDataFeed(ROOT / "data", c, start=TRAIN[0], end=TRAIN[1])
    engines = [
        EventEngine(replace(c, signal_half_life_s=t, entry_width_bps=w), name)
        for name in CANDIDATES
        for t in ((900, 3600) if name == "anchored_maker" else HALF_LIVES)
        for w in ((15.0, 30.0) if name == "anchored_maker" else WIDTHS)
    ]
    for d in feed.days:
        logging.getLogger(__name__).info("Development %s", d)
        day = feed.load(d)
        for engine in engines:
            engine.replay(day)
        del day
    trials = []
    for e in engines:
        e.finish()
        m = PerformanceAnalyzer.summarize(e)
        close_cost = unwind(e.ledger.inventory, e.prior)
        eligible = m["orders_with_fill"] >= 20 and len({f.side for f in e.fills}) == 2
        trials.append(
            {
                "strategy": e.name,
                "signal_half_life_s": e.c.signal_half_life_s,
                "entry_width_bps": e.c.entry_width_bps,
                "net_pnl": m["net_pnl"],
                "drawdown": m["max_drawdown"],
                "fees": m["fees"],
                "fills": m["fill_count"],
                "orders_with_fill": m["orders_with_fill"],
                "both_sides": len({f.side for f in e.fills}) == 2,
                "closing_cost": close_cost,
                "eligible": eligible,
                "score": m["net_pnl"] - close_cost - 0.5 * m["max_drawdown"],
                "daily_pnl": [day["pnl"] for day in e.daily],
            }
        )
    pool = [v for v in trials if v["eligible"]]
    if not pool:
        raise RuntimeError("No strategy satisfied the development activity floor")
    best = max(pool, key=lambda v: v["score"])
    selected = replace(
        c, signal_half_life_s=best["signal_half_life_s"], entry_width_bps=best["entry_width_bps"]
    )
    lock = {
        "development": TRAIN,
        "holdout": TEST,
        "rule": "highest development net PnL minus terminal spread/5 bp exit cost minus half maximum drawdown; at least 20 touched orders and both sides",
        "training_id": study_id(c, feed.manifest, CANDIDATES),
        "trials": trials,
        "strategy": best["strategy"],
        "config": asdict(selected),
    }
    path = OUT / "selection.json"
    if path.exists() and json.loads(path.read_text()) != json.loads(json.dumps(lock)):
        raise ValueError(
            "Existing selection differs; retain the published holdout and start a new experiment"
        )
    save_json(path, lock)
    print("Locked", best)


def prior_settlement_mid(feed, day, t):
    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    for dd in (day, previous):
        path = feed.folder / dd / "bookTicker.parquet"
        if path.exists():
            tab = pq.read_table(
                path,
                columns=["published", "bid", "ask"],
                filters=[("published", "<", t), ("published", ">=", t - 120_000_000_000)],
            )
            if len(tab):
                return (tab["bid"][-1].as_py() + tab["ask"][-1].as_py()) / 2
    raise ValueError("Missing prior funding proxy mark for open exposure")


def funding_proxy(feed, folder, name):
    """Funding sensitivity using positions strictly before each settlement."""
    with gzip.open(folder / (name + "-fills.csv.gz"), "rt") as stream:
        rows = list(csv.DictReader(stream))
    ts = np.array([int(r["ts"]) for r in rows], dtype=np.int64)
    positions = [float(r["inventory"]) for r in rows]
    flows = []
    for day in feed.days:
        begin = utc_ns(day)
        rates = [
            r
            for r in feed.manifest["funding_records"]
            if begin <= int(r["fundingTime"]) * 10**6 < begin + DAY
        ]
        if len(rates) != 3 or any(
            abs(int(r["fundingTime"]) * 10**6 - (begin + i * DAY // 3)) >= 10**9
            for i, r in enumerate(rates)
        ):
            raise ValueError("Incomplete eight-hour funding schedule for this study")
        for r in rates:
            t = int(r["fundingTime"]) * 10**6
            idx = int(ts.searchsorted(t)) - 1
            q = positions[idx] if idx >= 0 else 0.0
            price = float(r.get("markPrice") or 0)
            origin = "settlement mark"
            if not price and q:
                price = prior_settlement_mid(feed, day, t)
                origin = "published midpoint proxy"
            flows.append(
                {
                    "ts": t,
                    "position": q,
                    "rate": float(r["fundingRate"]),
                    "price": price or None,
                    "source": origin if price else "flat",
                    "cashflow": -q * price * float(r["fundingRate"]),
                }
            )
    return flows


def evaluate(confirmation=False):
    from verify_run import audit

    raw = (OUT / "selection.json").read_bytes()
    lock = json.loads(raw)
    c = Config(**lock["config"])
    name = lock["strategy"]
    training = json.loads((ROOT / "data" / c.symbol / "manifest.json").read_text())
    if study_id(config(), training, CANDIDATES) != lock["training_id"]:
        raise ValueError("Research code or development inputs differ from the locked selection")

    period = ("2023-09-13", "2023-09-17") if confirmation else TEST
    target = OUT / "confirmation" if confirmation else OUT
    root = ROOT / "data" / ("confirmation" if confirmation else "followup")
    c = replace(c, evaluation_start=period[0], end=period[1])
    feed = HistoricalDataFeed(root, c, start=period[0], end=period[1])
    base_names = ("fixed", "adaptive", "no_skew", name)
    result = {"selection_sha256": hashlib.sha256(raw).hexdigest(), "period": period, "results": {}}
    for tag, setting, names in (
        ("reference", c, base_names),
        ("higher_fee", replace(c, maker_fee_bps=2), [name]),
        ("lower_participation", replace(c, participation=0.02), [name]),
    ):
        key = study_id(setting, feed.manifest, names)
        folder = ROOT / "runs/adaptation" / key
        folder.parent.mkdir(parents=True, exist_ok=True)
        if not (folder / "metrics.json").exists():
            staging = Path(tempfile.mkdtemp(dir=folder.parent))
            try:
                phase(feed, feed.days, setting, names, staging / "phase")
                (staging / "phase").rename(folder)
            finally:
                shutil.rmtree(staging)
        metrics = json.loads((folder / "metrics.json").read_text())
        audit_result = audit(feed, folder, setting, feed.days)
        last = feed.load(feed.days[-1])
        closing = last.at(last.stop - 1)
        del last
        summary = {}
        for policy, m in metrics.items():
            flows = funding_proxy(feed, folder, policy)
            estimated = sum(row["cashflow"] for row in flows)
            q = m["terminal_inventory"]
            if abs(q) > (closing.bid_qty if q >= 0 else closing.ask_qty):
                raise ValueError("Terminal position exceeds quoted top-level depth")
            cost = unwind(q, closing)
            summary[policy] = {
                "metrics": m,
                "daily": json.loads((folder / (policy + "-daily.json")).read_text()),
                "estimated_funding": estimated,
                "funding_records": flows,
                "closing_cost": cost,
                "after_proxy_funding_and_close": m["net_pnl"] - m["funding"] + estimated - cost,
            }
        result["results"][tag] = {
            "run_id": key,
            "config": asdict(setting),
            "policies": summary,
            "audit": audit_result,
        }
    save_json(target / "quality.json", feed.manifest)
    save_json(target / "holdout.json", result)
    print(
        json.dumps(
            {
                case: {
                    k: round(v["after_proxy_funding_and_close"], 4)
                    for k, v in r["policies"].items()
                }
                for case, r in result["results"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["select", "evaluate", "confirm"])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stage = p.parse_args().stage
    if stage == "select":
        select()
    else:
        evaluate(confirmation=stage == "confirm")
