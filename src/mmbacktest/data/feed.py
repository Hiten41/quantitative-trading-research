"""Day-bounded columnar feed. Book lookup is an indexed replay of L1 updates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .archive import dates, digest
from .pipeline import DAY, NS, utc_ns


@dataclass(frozen=True, slots=True)
class BookUpdateEvent:
    ts: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class TradeEvent:
    ts: int
    price: float
    qty: float
    sell: bool


@dataclass(frozen=True, slots=True)
class FundingEvent:
    ts: int
    rate: float
    mark: float


@dataclass(slots=True)
class Day:
    start: int
    stop: int  # half open
    book: dict[str, np.ndarray]
    tape: dict[str, np.ndarray]
    funding: list[FundingEvent]

    exchange: dict[str, np.ndarray] | None = None

    def at(self, ts, prior=None, *, inclusive=True, venue=False):
        b = self.exchange if venue and self.exchange is not None else self.book
        idx = int(b["ts"].searchsorted(ts, side="right" if inclusive else "left")) - 1
        if idx < 0:
            return prior
        return BookUpdateEvent(
            int(b["ts"][idx]),
            float(b["bid"][idx]),
            float(b["bid_qty"][idx]),
            float(b["ask"][idx]),
            float(b["ask_qty"][idx]),
        )


class HistoricalDataFeed:
    def __init__(self, root: Path, config, *, start: str | None = None, end: str | None = None):
        self.folder = root / config.symbol
        self.manifest = json.loads((self.folder / "manifest.json").read_text())
        m = self.manifest
        if m["symbol"] != config.symbol or m["market"] != "Binance USD-M linear perpetual":
            raise ValueError("market identity mismatch")
        contract = m["contract"]
        if (contract["symbol"], contract["contractType"], contract["marginAsset"]) != (
            config.symbol,
            "PERPETUAL",
            "USDT",
        ):
            raise ValueError("contract mismatch")
        filters = {f["filterType"]: f for f in contract["filters"]}
        for f, key, expected in [
            ("PRICE_FILTER", "tickSize", config.tick),
            ("LOT_SIZE", "stepSize", config.step),
        ]:
            if float(filters[f][key]) != expected:
                raise ValueError(f"configured {key} differs from manifest")
        for f, key, v, lower in [
            ("PRICE_FILTER", "minPrice", config.min_price, True),
            ("PRICE_FILTER", "maxPrice", config.max_price, False),
            ("LOT_SIZE", "minQty", config.min_qty, True),
            ("LOT_SIZE", "maxQty", config.max_qty, False),
            ("MIN_NOTIONAL", "notional", config.min_notional, True),
        ]:
            bound = float(filters[f][key])
            if (lower and v < bound) or (not lower and v > bound):
                raise ValueError(f"configured {key} outside exchange snapshot")
        self.days = list(dates(start or config.start, end or config.end))
        indexed = {(f["day"], f["kind"]): f for f in m["files"]}
        for day in self.days:
            for kind in ("bookTicker", "trades"):
                p = self.folder / day / (kind + ".parquet")
                if (day, kind) not in indexed or digest(p) != indexed[day, kind]["parquet_sha256"]:
                    raise ValueError(f"unverified normalized data: {p}")

    def load(self, day, seconds=None):
        start = utc_ns(day)
        stop = start + (DAY if seconds is None else seconds * NS)

        def columns(kind, cols):
            t = pq.read_table(
                self.folder / day / (kind + ".parquet"),
                columns=cols,
                filters=None if seconds is None else [("ts", "<", stop)],
            )
            return {n: t[n].to_numpy() for n in cols}

        fields = ["bid", "bid_qty", "ask", "ask_qty"]
        exchange = columns("bookTicker", ["ts", "published", *fields])
        # Transaction partitions can publish their final updates after UTC midnight.
        prefix = None
        if day != self.days[0]:
            previous = self.days[self.days.index(day) - 1]
            prefix = pq.read_table(
                self.folder / previous / "bookTicker.parquet",
                columns=["published", *fields],
                filters=[("published", ">=", start), ("published", "<", stop)],
            )
        limit = int(exchange["published"].searchsorted(stop))
        books = {}
        for k in ["published", *fields]:
            a = exchange[k][:limit]
            if prefix is not None and len(prefix):
                a = np.concatenate((prefix[k].to_numpy(), a))
            books["ts" if k == "published" else k] = a
        if np.any(np.diff(books["ts"]) < 0):
            raise ValueError("reversed interday publication clock")
        trades = columns("trades", ["id", "ts", "price", "qty", "sell"])
        funding = [
            FundingEvent(
                int(r["fundingTime"]) * 10**6, float(r["fundingRate"]), float(r["markPrice"])
            )
            for r in self.manifest["funding_records"]
            if self.manifest["funding_included"] and start <= int(r["fundingTime"]) * 10**6 < stop
        ]
        return Day(start, stop, books, trades, funding, exchange)
