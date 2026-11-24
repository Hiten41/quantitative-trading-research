"""Bounded-memory normalization and quality checks on exchange transaction time."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import csv

from .archive import acquire, digest, discover, download_one, get

NS = 10**9
DAY = 86400 * NS
MAX_GAP = 120 * NS
SCHEMA_VERSION = 2
FIELDS = {
    "trades": ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker"],
    "bookTicker": [
        "update_id",
        "best_bid_price",
        "best_bid_qty",
        "best_ask_price",
        "best_ask_qty",
        "transaction_time",
        "event_time",
    ],
}


def utc_ns(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()) * NS


def normalize_time(values):
    """Accept one epoch unit per batch, in the supported 2000–2100 interval."""
    a = np.asarray(values)
    if not np.issubdtype(a.dtype, np.integer):
        raise ValueError("nonintegral timestamp")
    for factor in (10**6, 10**3, 1):
        if np.all((a >= 946684800 * NS // factor) & (a < 4102444800 * NS // factor)):
            return a.astype(np.int64) * factor
    raise ValueError("invalid or mixed timestamp units")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(value, tmp, indent=2, allow_nan=False)
    os.replace(tmp.name, path)


def normalize(item, root: Path, symbol):
    day, kind = item["day"], item["kind"]
    target = root / symbol / day / (kind + ".parquet")
    meta = target.with_suffix(".json")
    if meta.exists() and target.exists():
        record = json.loads(meta.read_text())
        if (
            record.get("schema_version") == SCHEMA_VERSION
            and record["sha256"] == item["sha256"]
            and record["parquet_sha256"] == digest(target)
        ):
            return record
    target.parent.mkdir(parents=True, exist_ok=True)
    start = utc_ns(day)
    names = FIELDS[kind]
    trade = kind == "trades"
    time_field = "time" if trade else "transaction_time"
    minute_counts = np.zeros(1440, dtype=np.int64)
    first = last = last_id = first_id = last_publication = None
    max_publication_lag_ms = 0.0
    id_gaps = []
    count = ties = gap = 0
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        staging = Path(tmp.name)
    writer = None
    try:
        with zipfile.ZipFile(root / "raw" / item["file"]) as archive:
            entries = [n for n in archive.namelist() if n.endswith(".csv")]
            if len(entries) != 1:
                raise ValueError("expected exactly one CSV archive member")
            with archive.open(entries[0]) as source:
                first_line = source.readline().decode("utf-8-sig").strip().split(",")
            header = not first_line[0].isdigit()
            if header and first_line != names:
                raise ValueError(f"unexpected {kind} header {first_line}")
            types = {name: pa.float64() for name in names}
            types.update({names[0]: pa.int64(), time_field: pa.int64()})
            types.update({"is_buyer_maker": pa.bool_()} if trade else {"event_time": pa.int64()})
            with archive.open(entries[0]) as source:
                batches = csv.open_csv(
                    source,
                    read_options=csv.ReadOptions(
                        column_names=names, skip_rows=int(header), block_size=4 << 20
                    ),
                    convert_options=csv.ConvertOptions(column_types=types),
                )
                for batch in batches:
                    if any(c.null_count for c in batch.columns):
                        raise ValueError("missing archive fields")
                    arr = {n: batch[n].to_numpy(zero_copy_only=False) for n in names}
                    ts = normalize_time(arr[time_field])
                    ids = arr[names[0]]
                    if np.any(ts < start) or np.any(ts >= start + DAY):
                        raise ValueError(f"{day}: event outside requested UTC day")
                    diffs = np.diff(ts, prepend=ts[0] if last is None else last)
                    id_diffs = np.diff(ids, prepend=ids[0] - 1 if last_id is None else last_id)
                    if np.any(diffs < 0) or np.any(id_diffs <= 0):
                        raise ValueError(f"{day}/{kind}: reversed clock or discontinuous IDs")
                    if trade:
                        id_gaps.extend(
                            (int(ids[i] - id_diffs[i] + 1), int(ids[i] - 1))
                            for i in np.flatnonzero(id_diffs > 1)
                        )
                    if first is None:
                        first, first_id = int(ts[0]), int(ids[0])
                        ties -= 1
                    last, last_id = int(ts[-1]), int(ids[-1])
                    count += len(ts)
                    gap = max(gap, int(diffs.max()))
                    ties += int(np.count_nonzero(diffs == 0))
                    minute_counts += np.bincount((ts - start) // (60 * NS), minlength=1440)
                    values = (
                        {"price": arr["price"], "qty": arr["qty"]}
                        if trade
                        else dict(
                            zip(
                                ("bid", "bid_qty", "ask", "ask_qty"),
                                (arr[n] for n in names[1:5]),
                                strict=True,
                            )
                        )
                    )
                    if any(np.any(~np.isfinite(v) | (v <= 0)) for v in values.values()):
                        raise ValueError(f"{day}/{kind}: invalid price or size")
                    if not trade and np.any(values["ask"] <= values["bid"]):
                        raise ValueError(f"{day}: locked or crossed market")
                    if not trade:
                        pub = normalize_time(arr["event_time"])
                        if np.any(pub < ts) or np.any(
                            np.diff(
                                pub,
                                prepend=pub[0] if last_publication is None else last_publication,
                            )
                            < 0
                        ):
                            raise ValueError("invalid book publication clock")
                        values["published"] = pub
                        last_publication = int(pub[-1])
                        max_publication_lag_ms = max(
                            max_publication_lag_ms, float((pub - ts).max()) / 1e6
                        )
                    if trade:
                        values["sell"] = arr["is_buyer_maker"]
                    table = pa.table(dict(ts=ts, id=ids, **values))
                    if writer is None:
                        writer = pq.ParquetWriter(
                            staging, table.schema, compression="zstd", use_dictionary=False
                        )
                    writer.write_table(table)
        if first is None or max(gap, first - start, start + DAY - last) > MAX_GAP:
            raise ValueError(f"{day}/{kind}: empty or >120s discontinuity")
        writer.close()
        writer = None
        proof = verify_gaps(root, symbol, day, staging, id_gaps) if id_gaps else None
        os.replace(staging, target)
    finally:
        if writer:
            writer.close()
        staging.unlink(missing_ok=True)
    record = dict(
        **item,
        trade_id_gaps=id_gaps,
        corroboration=proof,
        schema_version=SCHEMA_VERSION,
        parquet_sha256=digest(target),
        first_ns=first,
        last_ns=last,
        first_id=first_id,
        last_id=last_id,
        events=count,
        repeated_timestamps=ties,
        max_gap_seconds=gap / NS,
        missing_timestamps=0,
        invalid_or_crossed=0,
        max_publication_lag_ms=max_publication_lag_ms,
        mean_events_per_second=count / 86400,
        minute_count_percentiles=dict(
            zip(
                ("p0", "p50", "p95", "p100"),
                np.percentile(minute_counts, [0, 50, 95, 100]).tolist(),
                strict=True,
            )
        ),
    )
    save_json(meta, record)
    logging.getLogger(__name__).info("normalized %s %s: %d events", day, kind, count)
    return record


def prepare(symbol, start, end, root=Path("data"), workers=3, reference=None):
    sources = acquire(symbol, start, end, root, workers)
    with ThreadPoolExecutor(min(2, max(1, workers))) as pool:
        files = list(pool.map(lambda item: normalize(item, root, symbol), sources))
    coverage = []
    previous = {}
    for day in sorted({f["day"] for f in files}):
        pair = [f for f in files if f["day"] == day]
        left = max(f["first_ns"] for f in pair)
        right = min(f["last_ns"] for f in pair)
        if len(pair) != 2 or left >= right:
            raise ValueError("no overlapping archive pair")
        for f in pair:
            prev = previous.get(f["kind"])
            if prev and (
                f["first_ns"] - prev["last_ns"] > MAX_GAP
                or (f["kind"] == "trades" and f["first_id"] != prev["last_id"] + 1)
            ):
                raise ValueError(f"discontinuity at boundary {day}")
            previous[f["kind"]] = f
        # Count clock ambiguity and stale as-of joins without allocating Python events.
        local = root / symbol / day
        books = pq.read_table(local / "bookTicker.parquet", columns=["published"])[
            "published"
        ].to_numpy()
        prints = pq.read_table(local / "trades.parquet", columns=["ts"])["ts"].to_numpy()
        left_indices = books.searchsorted(prints, side="left")
        right_indices = books.searchsorted(prints, side="right")
        missing = int((left_indices == 0).sum())
        valid_indices = left_indices[missing:] - 1
        stale = int(np.count_nonzero(prints[missing:] - books[valid_indices] > 2 * NS))
        coverage.append(
            {
                "day": day,
                "tied_trade_publication_events": int((right_indices > left_indices).sum()),
                "no_earlier_same_day_book": missing,
                "prior_book_older_than_2s": stale,
                "overlap_ns": [left, right],
                "unmatched_edges_seconds": [
                    (left - utc_ns(day)) / NS,
                    (utc_ns(day) + DAY - right) / NS,
                ],
            }
        )
    endpoint = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    endpoint_funding = "https://fapi.binance.com/fapi/v1/fundingRate"
    if reference:
        snapshot = json.loads(Path(reference).read_text(encoding="utf-8"))
        if (snapshot["symbol"], snapshot["start"], snapshot["end"]) != (symbol, start, end):
            raise ValueError("reference manifest belongs to a different dataset")
        expected = {f["file"]: f["sha256"] for f in snapshot["files"]}
        if expected != {f["file"]: f["sha256"] for f in files}:
            raise ValueError("source ZIPs differ from the frozen study")
        contract = snapshot["contract"]
        settlements = snapshot["funding_records"]
        metadata_asof = snapshot.get("metadata_asof", snapshot["generated_utc"])
    else:
        contract = next(s for s in get(endpoint).json()["symbols"] if s["symbol"] == symbol)
        settlements = []
        for period in coverage:
            begin = utc_ns(period["day"]) // 10**6
            settlements.extend(
                get(
                    endpoint_funding,
                    params={
                        "symbol": symbol,
                        "startTime": begin,
                        "endTime": begin + 86_400_000 - 1,
                        "limit": 1000,
                    },
                ).json()
            )
        metadata_asof = datetime.now(UTC).isoformat()
    if (
        contract["symbol"],
        contract["contractType"],
        contract["quoteAsset"],
        contract["marginAsset"],
    ) != (symbol, "PERPETUAL", "USDT", "USDT"):
        raise ValueError("expected linear USDT perpetual")
    funding_times = [int(r["fundingTime"]) for r in settlements]
    if any(
        r["symbol"] != symbol or not np.isfinite(float(r["fundingRate"])) for r in settlements
    ) or funding_times != sorted(set(funding_times)):
        raise ValueError("invalid or unsorted funding response")
    # The supported funding calendar for the replay is the 8-hour ETH schedule.
    # An incomplete or differently scheduled sequence requires an explicit new calendar.
    expected = np.arange(utc_ns(start) // 10**6, (utc_ns(end) + DAY) // 10**6, 8 * 3600 * 1000)
    valid = (
        len(expected) == len(funding_times)
        and np.all(np.abs(expected - funding_times) < 1000)
        and all(
            np.isfinite(float(r.get("markPrice") or 0)) and float(r.get("markPrice") or 0) > 0
            for r in settlements
        )
    )
    manifest = {
        "symbol": symbol,
        "market": "Binance USD-M linear perpetual",
        "start": start,
        "end": end,
        "files": files,
        "coverage": coverage,
        "exchange_info_url": endpoint,
        "contract": contract,
        "funding_url": endpoint_funding,
        "funding_records": settlements,
        "funding_included": valid,
        "funding_reason": "published settlement marks"
        if valid
        else "incomplete settlement marks or unsupported funding calendar; funding excluded throughout",
        "metadata_asof": metadata_asof,
        "generated_utc": datetime.now(UTC).isoformat(),
    }
    save_json(root / symbol / "manifest.json", manifest)
    return manifest


def verify_gaps(root, symbol, day, parquet, gaps):
    """Reject unexplained missing prints. Aggregate IDs must be contiguous around each gap."""
    item = download_one(discover(symbol, day, day, ("aggTrades",))[0], root / "raw")
    with zipfile.ZipFile(root / "raw" / item["file"]) as z, z.open(z.namelist()[0]) as stream:
        table = csv.read_csv(stream)
    agg, first, last, quantity = (table.column(i).to_numpy() for i in (0, 3, 4, 2))
    if np.any(np.diff(agg) != 1) or np.any(first[1:] <= last[:-1]):
        raise ValueError("ambiguous aggregate sequence")
    tape = pq.read_table(parquet, columns=["id", "qty"])
    ids, qty = (tape[n].to_numpy() for n in ("id", "qty"))
    for left, right in gaps:
        i = int(last.searchsorted(left))
        if i >= len(last) or i == 0:
            raise ValueError("unbracketed ID gap")
        # Inside an aggregate, require its entire reported quantity to be
        # present in the raw prints. Between aggregates, boundaries must match.
        if first[i] <= left and last[i] >= right:
            lo, hi = ids.searchsorted([first[i], last[i] + 1])
            if not np.isclose(qty[lo:hi].sum(), quantity[i], rtol=0, atol=1e-8):
                raise ValueError("missing raw executed volume")
        elif last[i - 1] != left - 1 or first[i] != right + 1:
            raise ValueError("unexplained trade ID gap")
    return item
