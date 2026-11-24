"""Provenance-checked market depth, funding, and valuation data."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow.csv as pacsv
import requests

from mmbacktest.data.archive import CDN, digest, download_one, get, retry
from mmbacktest.data.pipeline import save_json

HOUR = 3_600_000
MINUTE = 60_000
DAY = 24 * HOUR
SYMBOL = "ETHUSDT"


def save_funding_snapshot(path: Path, rows: list) -> None:
    """Keep the original REST snapshot encoding stable across operating systems."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        staging = Path(tmp.name)
    try:
        with staging.open("w", encoding="utf8", newline="\r\n") as stream:
            json.dump(rows, stream, indent=2, allow_nan=False)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def ms(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp).timestamp() * 1000)


def iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).isoformat(timespec="milliseconds")


def month_start(month: str) -> int:
    return ms(month + "-01T00:00:00Z")


def months(start: int, end: int):
    current = datetime.fromtimestamp(start / 1000, UTC).date().replace(day=1)
    while current.strftime("%Y-%m") <= iso(end)[:7]:
        yield current.strftime("%Y-%m")
        current = current.replace(
            year=current.year + (current.month == 12), month=current.month % 12 + 1
        )


def acquire_binance(job, root):
    month, kind = job
    base = {
        "funding": ("futures/um/monthly/fundingRate", "fundingRate"),
        "mark": ("futures/um/monthly/markPriceKlines", "1m"),
        "spot": ("spot/monthly/klines", "1h"),
    }
    prefix, freq = base[kind]
    filename = f"{SYMBOL}-{freq}-{month}.zip"
    subdir = "" if kind == "funding" else freq + "/"
    url = f"{CDN}/data/{prefix}/{SYMBOL}/{subdir}{filename}"

    def size():
        with requests.head(url, timeout=(10, 40), allow_redirects=True) as r:
            r.raise_for_status()
            return int(r.headers["Content-Length"])

    item = download_one({"url": url, "size": retry(size), "checksum": True}, root / kind)
    return {**item, "path": f"{kind}/{filename}", "kind": kind, "month": month}


def acquire_depth(job, root):
    day, exchange = job
    url = f"https://datasets.tardis.dev/v1/{exchange}/book_snapshot_5/{day.replace('-', '/')}/{SYMBOL}.csv.gz"
    rel = f"depth/{exchange}-{day}.csv.gz"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = path.with_suffix(".json")
    if path.is_file() and sidecar.is_file():
        record = json.loads(sidecar.read_text())
        if record["url"] == url and digest(path) == record["sha256"]:
            return record

    def transfer():
        with tempfile.NamedTemporaryFile(delete=False, dir=path.parent) as tmp:
            staging = Path(tmp.name)
        try:
            # raw.stream keeps the original gzip bytes even if the CDN sets Content-Encoding.
            with requests.get(
                url, stream=True, timeout=(15, 90), headers={"Accept-Encoding": "identity"}
            ) as r:
                r.raise_for_status()
                if r.status_code != 200:
                    raise OSError(f"partial response for {url}")
                size = 0
                md5 = hashlib.md5(usedforsecurity=False)
                with staging.open("wb") as out:
                    for chunk in r.raw.stream(1024 * 1024, decode_content=False):
                        out.write(chunk)
                        md5.update(chunk)
                        size += len(chunk)
                etag = r.headers.get("ETag", "").strip('"')
                if "Content-Length" in r.headers and size != int(r.headers["Content-Length"]):
                    raise OSError("truncated Tardis archive")
                if len(etag) == 32 and md5.hexdigest() != etag:
                    raise OSError("Tardis object ETag mismatch")
            with gzip.open(staging, "rb") as f:
                if not f.readline().startswith(b"exchange,symbol,timestamp,local_timestamp,"):
                    raise ValueError(f"unexpected depth schema: {url}")
                while f.read(1 << 20):
                    pass  # verify end-of-file gzip CRC
            os.replace(staging, path)
            record = {
                "url": url,
                "path": rel,
                "kind": "depth",
                "day": day,
                "exchange": exchange,
                "etag": etag,
                "size": size,
                "sha256": digest(path),
                "verified_utc": datetime.now(UTC).isoformat(),
            }
            save_json(sidecar, record)
            return record
        finally:
            staging.unlink(missing_ok=True)

    return retry(transfer)


def sources(config, root: Path, reference: Path | None = None):
    root.mkdir(parents=True, exist_ok=True)
    windows = config["windows"]
    start = min(ms(w["enter_utc"]) for w in windows)
    end = max(ms(w["exit_utc"]) for w in windows)
    days = sorted({w[k][:10] for w in windows for k in ("enter_utc", "exit_utc")})
    jobs = [(m, k) for m in months(start, end) for k in ("mark", "funding", "spot")]
    with ThreadPoolExecutor(4) as pool:
        records = list(pool.map(lambda job: acquire_binance(job, root), jobs))
        records.extend(
            pool.map(
                lambda job: acquire_depth(job, root),
                [(d, x) for d in days for x in ("binance", "binance-futures")],
            )
        )
    # Binance's funding history sometimes also supplies the actual settlement mark.
    for w in windows:
        a, b = ms(w["enter_utc"]), ms(w["exit_utc"])
        if b - a > 300 * DAY:
            raise ValueError("REST funding request exceeds supported bounded window")
        rel = f"funding/history-{a}-{b}.json"
        p = root / rel
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={SYMBOL}&startTime={a}&endTime={b}&limit=1000"
        if not p.exists():
            payload = get(url).json()
            if not isinstance(payload, list) or not 0 < len(payload) < 1000:
                raise ValueError("invalid or paginated funding history")
            save_funding_snapshot(p, payload)
        records.append(
            {"url": url, "path": rel, "sha256": digest(p), "kind": "history", "window": w["name"]}
        )
    if reference and reference.exists():
        published = json.loads(reference.read_text(encoding="utf8"))
        if {r["url"]: r["sha256"] for r in records} != {r["url"]: r["sha256"] for r in published}:
            raise ValueError("data differ from the published study")
    return records


@dataclass(frozen=True)
class Book:
    local_us: int
    exchange_us: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


def books_at(path: Path, exchange: str, times: list[int], age_ms: int) -> dict[int, Book]:
    """Return the most recent complete recorded book at or before each arrival time."""
    targets = sorted(set(times))
    found = {}
    prev = None
    with gzip.open(path, "rb") as stream:
        for batch in pacsv.open_csv(stream):
            clock = batch["local_timestamp"].to_numpy()
            if np.any(np.diff(clock) < 0) or (prev is not None and int(clock[0]) < prev):
                raise ValueError("reversed capture clock")
            prev = int(clock[-1])
            for t in targets:
                j = int(np.searchsorted(clock, t * 1000, side="right")) - 1
                if j < 0:
                    continue

                def value(k, batch=batch, j=j):
                    return batch[k][j].as_py()

                if value("exchange") != exchange or value("symbol") != SYMBOL:
                    raise ValueError("wrong execution book")

                def levels(side):
                    vals = tuple(
                        (float(value(f"{side}[{i}].price")), float(value(f"{side}[{i}].amount")))
                        for i in range(5)
                    )
                    if any(not np.isfinite([p, q]).all() or min(p, q) <= 0 for p, q in vals):
                        raise ValueError("invalid displayed price or quantity")
                    prices = np.array([p for p, q in vals])
                    if np.any(np.diff(prices) * (1 if side == "asks" else -1) <= 0):
                        raise ValueError("unordered depth levels")
                    return vals

                b = Book(int(clock[j]), int(value("timestamp")), levels("bids"), levels("asks"))
                if b.bids[0][0] >= b.asks[0][0]:
                    raise ValueError("crossed execution book")
                found[t] = b
            if clock[-1] > targets[-1] * 1000:
                break
    for t in targets:
        if t not in found or t * 1000 - found[t].local_us > age_ms * 1000:
            raise ValueError(f"no fresh {exchange} book at {iso(t)}")
    return found


@dataclass(frozen=True)
class Bars:
    times: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray

    def at(self, t: int) -> int:
        i = int(self.times.searchsorted(t))
        if i == len(self.times) or int(self.times[i]) != t:
            raise ValueError(f"missing valuation bar at {iso(t)}")
        return i


def read_bars(root, records, kind):
    parts = []
    freq = MINUTE if kind == "mark" else HOUR
    for r in sorted((r for r in records if r["kind"] == kind), key=lambda r: r["month"]):
        with zipfile.ZipFile(root / r["path"]) as z:
            with z.open(z.namelist()[0]) as f:
                first = f.readline().split(b",")[0]
            with z.open(z.namelist()[0]) as f:
                table = pacsv.read_csv(
                    f,
                    read_options=pacsv.ReadOptions(
                        column_names=[str(i) for i in range(12)], skip_rows=int(not first.isdigit())
                    ),
                    convert_options=pacsv.ConvertOptions(
                        include_columns=["0", "1", "2", "3", "4", "6"]
                    ),
                )
        t, o, h, l, c, close_time = (table[n].to_numpy() for n in table.column_names)
        if int(t[0]) != month_start(r["month"]) or np.any(close_time != t + freq - 1):
            raise ValueError("incorrect candle coverage or end timestamp")
        if (
            not np.isfinite([o, h, l, c]).all()
            or np.any(l <= 0)
            or np.any(l > np.minimum(o, c))
            or np.any(h < np.maximum(o, c))
        ):
            raise ValueError("invalid valuation bars")
        parts.append((t, o, h, l, c))
    bars = Bars(*(np.concatenate(v) for v in zip(*parts, strict=True)))
    if np.any(np.diff(bars.times) != freq):
        raise ValueError(f"missing {kind} valuation bars")
    return bars
