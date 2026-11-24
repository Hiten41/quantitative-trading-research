"""Run the fixed study without exchange credentials."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .replay import experiment
from .report import publish
from .sources import sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/funding_carry.json"))
    parser.add_argument("--data", type=Path, default=Path("data/funding-carry"))
    parser.add_argument("--output", type=Path, default=Path("reports/funding-carry"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = json.loads(args.config.read_text(encoding="utf8"))
    if config["symbol"] != "ETHUSDT":
        parser.error("This study supports Binance ETHUSDT spot and linear perpetual futures.")
    records = sources(config, args.data, args.output / "sources.json")
    result, paths = experiment(config, args.data, records)
    publish(args.output, result, paths, records)
    for label, w in result["windows"].items():
        print(f"{label}: {w['cases']['base']['net_pnl']:+.4f} USDT")
    print(args.output / "findings.md")


if __name__ == "__main__":
    main()
