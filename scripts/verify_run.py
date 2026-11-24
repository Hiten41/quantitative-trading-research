"""Independent ex-post audit of published fills against immutable historical prints."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from mmbacktest.config import Config
from mmbacktest.data.feed import HistoricalDataFeed
from mmbacktest.data.pipeline import NS
from mmbacktest.engine import HORIZONS


def read_rows(path):
    with gzip.open(path, "rt", encoding="utf8", newline="") as stream:
        return list(csv.DictReader(stream))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def check_mark(f, h, day, prior, c):
    target = int(f["ts"]) + h * NS
    book = day.at(target, prior)
    value = f[f"markout_{h}s"]
    if book is None or target - book.ts > c.stale_ms * 10**6:
        require(value == "", "stale or unavailable horizon marked")
        return 0
    expected = int(f["side"]) * (book.mid - float(f["mid"])) * float(f["qty"])
    require(value != "" and abs(float(value) - expected) < 1e-8, "incorrect markout")
    return 1


def audit(feed, folder, c, days):
    result = {}
    metrics = json.loads((folder / "metrics.json").read_text())
    for name, m in metrics.items():
        fills = read_rows(folder / (name + "-fills.csv.gz"))
        orders = read_rows(folder / (name + "-orders.csv.gz"))
        active = {int(r["id"]): int(r["ts"]) for r in orders if r["event"] == "activated"}
        transitions = Counter(
            (int(r["id"]), int(r["ts"]))
            for r in orders
            if r["event"] in ("partial_fill", "fully_filled")
        )
        require(
            transitions == Counter((int(f["order"]), int(f["ts"])) for f in fills),
            "order/fill transition mismatch",
        )
        # Decimal accumulation is independent of the engine's floating-point financing balance.
        position = Decimal(0)
        cash = Decimal(str(c.capital))
        fees = Decimal(0)
        funding_total = Decimal(0)
        pending_marks = []
        index = 0
        marks = 0
        prior = None
        last_mid = None
        for d in days:
            day = feed.load(d)
            marks += sum(check_mark(f, h, day, prior, c) for f, h in pending_marks)
            pending_marks.clear()
            settlements = iter(day.funding)
            settlement = next(settlements, None)
            tape = day.tape
            last_t = last_index = -1
            while index < len(fills):
                f = fills[index]
                t = int(f["ts"])
                if t >= day.stop:
                    break
                require(day.start <= t and t >= last_t, "fill clock regression")
                key = int(f["order"])
                require(
                    active[key] < t < active[key] + c.lifetime_ms * 10**6,
                    "fill outside live lifetime",
                )
                while settlement and settlement.ts <= t:
                    amount = (
                        -position * Decimal(str(settlement.mark)) * Decimal(str(settlement.rate))
                    )
                    funding_total += amount
                    cash += amount
                    settlement = next(settlements, None)
                side = int(f["side"])
                qty = Decimal(f["qty"])
                price = Decimal(f["price"])
                q = float(qty)
                p = float(price)
                matching = int(tape["id"].searchsorted(int(f["trade_id"])))
                require(
                    matching < len(tape["id"])
                    and int(tape["id"][matching]) == int(f["trade_id"])
                    and matching > last_index
                    and tape["ts"][matching] == t,
                    "unknown or reused print ID",
                )
                through = side * (p - tape["price"][matching])
                require(
                    bool(tape["sell"][matching]) == (side == 1)
                    and (through > 0 or (c.fill_model == "queue" and through == 0))
                    and q <= tape["qty"][matching] * c.participation + 1e-12,
                    "print not eligible",
                )
                last_t, last_index = t, matching
                book = day.at(t, prior, inclusive=False)
                require(
                    book
                    and t - book.ts <= c.stale_ms * 10**6
                    and abs(book.mid - float(f["mid"])) < 1e-8,
                    "fill mid was unavailable",
                )
                fee = price * qty * Decimal(str(c.maker_fee_bps)) / Decimal(10000)
                require(abs(float(fee) - float(f["fee"])) < 1e-9, "fee mismatch")
                position += side * qty
                cash -= side * price * qty + fee
                fees += fee
                require(abs(float(cash) - float(f["cash"])) < 1e-7, "fill cash mismatch")
                require(
                    abs(float(position) - float(f["inventory"])) < 1e-8
                    and abs(float(position)) <= c.inventory_limit + 1e-9,
                    "inventory mismatch",
                )
                for h in HORIZONS:
                    if t + h * NS >= day.stop:
                        pending_marks.append((f, h))
                    else:
                        marks += check_mark(f, h, day, prior, c)
                index += 1
            while settlement:
                amount = -position * Decimal(str(settlement.mark)) * Decimal(str(settlement.rate))
                funding_total += amount
                cash += amount
                settlement = next(settlements, None)
            prior = day.at(day.stop - 1, prior)
            last_mid = prior.mid
        require(index == len(fills), "fills outside replay period")
        require(all(f[f"markout_{h}s"] == "" for f, h in pending_marks), "markout beyond phase")
        require(abs(float(funding_total) - m["funding"]) < 1e-7, "funding mismatch")
        value = float(cash + position * Decimal(str(last_mid)))
        require(
            abs(value - m["net_equity"]) < 1e-7 and abs(float(fees) - m["fees"]) < 1e-7,
            "independent reconciliation failed",
        )
        result[name] = {
            "distinct_prints_matched": index,
            "valid_markouts_checked": marks,
            "missing_markouts_checked": len(HORIZONS) * index - marks,
            "terminal_equity_error_usdt": value - m["net_equity"],
        }
    return result


def verify_run(folder: Path, data: Path):
    c = Config(**json.loads((folder / "config.json").read_text()))
    feed = HistoricalDataFeed(data, c)
    original = json.loads((folder / "quality.json").read_text())
    expected = [(r["file"], r["parquet_sha256"]) for r in original["files"]]
    actual = [(r["file"], r["parquet_sha256"]) for r in feed.manifest["files"]]
    require(actual == expected, "data manifest differs from the run")
    result = {"run_id": folder.name}
    for phase in ("calibration", "evaluation"):
        days = [d for d in feed.days if (d < c.evaluation_start) == (phase == "calibration")]
        result[phase] = audit(feed, folder / phase, c, days)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, help="Write JSON audit evidence to disk")
    args = parser.parse_args()
    folder = args.runs / args.run_id
    result = verify_run(folder, args.data)
    if args.output:
        from mmbacktest.data.pipeline import save_json

        save_json(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
