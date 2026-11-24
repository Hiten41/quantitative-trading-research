"""Command-line entry points. Visualization dependencies load only for reporting."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import Config
from .data.archive import discover
from .data.pipeline import prepare
from .experiment import NAMES, run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mmbacktest")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("data-check", "download"):
        p = commands.add_parser(name)
        p.add_argument("--symbol", default="ETHUSDT")
        p.add_argument("--start", default=Config().start)
        p.add_argument("--end", default=Config().end)
        p.add_argument("--data", type=Path, default=Path("data"))
        if name == "download":
            p.add_argument("--workers", type=int, default=3)
            p.add_argument(
                "--reference",
                type=Path,
                help="Verify raw hashes and use this frozen contract and funding snapshot",
            )
    for name in ("backtest", "compare"):
        p = commands.add_parser(name)
        p.add_argument("--config", type=Path, default=Path("configs/default.yaml"))
        p.add_argument("--data", type=Path, default=Path("data"))
        p.add_argument("--runs", type=Path, default=Path("runs"))
        if name == "backtest":
            p.add_argument(
                "--strategy",
                choices=(*NAMES, "selective_maker", "pullback_maker", "anchored_maker"),
                default="fixed",
            )
    p = commands.add_parser("report")
    p.add_argument("--run-id", required=True)
    p.add_argument("--runs", type=Path, default=Path("runs"))
    p.add_argument("--output", type=Path, default=Path("reports"))
    a = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.command == "data-check":
        files = discover(a.symbol, a.start, a.end)
        print(json.dumps(files, indent=2))
        manifest = a.data / a.symbol / "manifest.json"
        if manifest.exists():
            m = json.loads(manifest.read_text())
            print("Locally validated UTC coverage")
            print(json.dumps([r for r in m["coverage"] if a.start <= r["day"] <= a.end], indent=2))
        else:
            print("Archive objects listed. Run download to validate row-level overlap.")
    elif a.command == "download":
        m = prepare(a.symbol, a.start, a.end, a.data, a.workers, a.reference)
        print(f"Validated {len(m['files'])} files. {a.data / a.symbol / 'manifest.json'}")
    elif a.command in ("backtest", "compare"):
        path = run(
            Config.load(a.config),
            a.data,
            a.runs,
            (a.strategy,) if a.command == "backtest" else NAMES,
        )
        print(f"Run ID: {path.name}")
        print(f"Evaluation: {path / 'evaluation' / 'metrics.json'}")
    else:
        from .report import ReportGenerator

        if Path(a.run_id).name != a.run_id:
            parser.error("expected a run ID, not a path")
        print(ReportGenerator(a.runs / a.run_id, a.output).generate())
