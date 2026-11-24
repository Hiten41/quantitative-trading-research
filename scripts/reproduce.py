"""Rebuild the published ETHUSDT experiment using its frozen source manifest."""

from __future__ import annotations

import logging
from pathlib import Path

from verify_run import verify_run

from mmbacktest.config import Config
from mmbacktest.data.pipeline import prepare, save_json
from mmbacktest.experiment import run
from mmbacktest.report import ReportGenerator


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    config = Config.load(root / "configs/default.yaml")
    data = root / "data"
    reports = root / "reports"
    prepare(config.symbol, config.start, config.end, data, reference=reports / "quality.json")
    result = run(config, data, root / "runs")
    output = ReportGenerator(result, reports).generate()
    save_json(reports / "audit.json", verify_run(result, data))
    print(f"Run ID: {result.name}")
    print(output)


if __name__ == "__main__":
    main()
