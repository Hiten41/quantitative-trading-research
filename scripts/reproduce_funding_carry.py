"""Reproduce the historical study from the repository root."""

from __future__ import annotations

import csv
import gzip
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fundingcarry.replay import experiment
from fundingcarry.sources import MINUTE, iso, sources
from fundingcarry.study_audit import audit
from fundingcarry.study_data import combine, prepare
from fundingcarry.study_engine import simulate
from mmbacktest.data.archive import digest
from mmbacktest.data.pipeline import save_json


def csv_file(path, rows):
    if not rows:
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    import numpy
    import plotly
    import pyarrow

    if not __debug__:
        raise RuntimeError("run without -O to enable independent audit assertions")
    os.chdir(ROOT)
    started = time.monotonic()
    folder = ROOT / "reports/funding-carry"
    dest = folder / "final"
    dest.mkdir(exist_ok=True)
    root = Path("data/funding-carry")
    protocol = Path("configs/carry_study.json")
    cfg = json.loads(protocol.read_text())
    margin_protocol = Path("configs/carry_margin_stress.json")
    cfg["scenarios"].update(json.loads(margin_protocol.read_text()))
    legacy = json.loads(Path("configs/funding_carry.json").read_text())
    original = json.loads((folder / "sources.json").read_text())
    if not all(
        (root / r["path"]).is_file() and digest(root / r["path"]) == r["sha256"] for r in original
    ):
        original = sources(legacy, root, folder / "sources.json")
    regression, paths = experiment(legacy, root, original)
    if regression != json.loads((folder / "results.json").read_text()):
        raise ValueError("legacy regression changed")
    journal = audit(root, original, regression, paths)
    intervals, records, history, regimes = prepare(root, cfg, legacy)
    manifest = {r["url"]: r for r in original + records}
    metrics = []
    datasets = {}
    exports = {}
    for name, span in intervals.items():
        data, events, prior, rest = combine(root, records, span, history)
        manifest.update({r["url"]: r for r in rest})
        datasets[name] = {
            "start": iso(span[0]),
            "end": iso(span[1] + MINUTE),
            "settlements": len(events),
            "negative": sum(e["rate"] < 0 for e in events.values()),
            "observed": sum(e["source"] == "REST settlement" for e in events.values()),
            "minutes": len(data["mark"]) - 1,
        }
        for scenario, overrides in cfg["scenarios"].items():
            for policy in "ABC":
                m, streams = simulate(
                    cfg | overrides, data, events, prior, span[0], policy, scenario == "base"
                )
                metrics.append(dict(window=name, policy=policy, scenario=scenario, **m))
                for row in streams.pop("risk_events"):
                    exports.setdefault("risk_events", []).append(
                        dict(window=name, policy=policy, scenario=scenario, **row)
                    )
                if scenario == "base":
                    for key, rows in streams.items():
                        exports.setdefault(key, []).extend(
                            dict(window=name, policy=policy, **row) for row in rows
                        )
        print(name, datasets[name], flush=True)
    for d in exports["decisions"]:
        if (
            d["last_signal"] is not None
            and d["last_signal"] > d["time"] - MINUTE * cfg["signals"]["publication_delay_minutes"]
        ):
            raise ValueError("future signal")
    for d in exports["balances"]:
        equity = (
            d["spot_cash"]
            + d["spot_qty"] * d["spot_mark"]
            + d["futures_wallet"]
            + d["futures_unrealized"]
            + d["transit"]
        )
        if abs(equity - d["equity"]) > 1e-8:
            raise ArithmeticError("wallet audit failed")
    for d in exports["settlements"]:
        if abs(d["cashflow"] - d["short"] * d["rate"] * d["mark"]) > 1e-10:
            raise ArithmeticError("funding audit failed")
    sources_list = list(manifest.values())
    pinned = dest / "input_hashes.json"
    hashes = {r["url"]: r["sha256"] for r in sources_list}
    if pinned.exists() and json.loads(pinned.read_text()) != hashes:
        raise ValueError("historical inputs differ from study lock")
    for k, rows in exports.items():
        csv_file(dest / (k + ".csv.gz"), rows)
    csv_file(dest / "metrics.csv", metrics)
    csv_file(dest / "regimes.csv", regimes)
    save_json(pinned, hashes)
    save_json(dest / "sources.json", sources_list)
    result = {
        "protocol_sha256": digest(protocol),
        "margin_protocol_sha256": digest(margin_protocol),
        "windows": datasets,
        "regression": journal,
        "metrics": metrics,
        "regimes": regimes,
        "cached_input_bytes": sum((root / r["path"]).stat().st_size for r in sources_list),
        "versions": {
            "python": sys.version.split()[0],
            "numpy": numpy.__version__,
            "pyarrow": pyarrow.__version__,
            "plotly": plotly.__version__,
        },
    }
    from fundingcarry.study_report import publish

    publish(dest, result, exports, cfg)
    result["runtime_seconds"] = round(time.monotonic() - started, 2)
    save_json(dest / "study.json", result)
    print(f"Completed in {result['runtime_seconds']} seconds; {dest}")


if __name__ == "__main__":
    main()
