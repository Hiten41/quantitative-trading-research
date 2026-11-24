"""Verified monthly market inputs and deterministic regime discovery."""

from __future__ import annotations

import calendar
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby, pairwise

import numpy as np
import pyarrow.csv as pc

from mmbacktest.data.archive import CDN, S3, digest, download_one, get
from mmbacktest.data.pipeline import save_json

from .sources import DAY, HOUR, MINUTE, SYMBOL, iso, month_start, months, ms, save_funding_snapshot

PREFIX = {
    "funding": "futures/um/monthly/fundingRate/ETHUSDT/",
    "mark": "futures/um/monthly/markPriceKlines/ETHUSDT/1m/",
    "spot1m": "spot/monthly/klines/ETHUSDT/1m/",
    "perp1m": "futures/um/monthly/klines/ETHUSDT/1m/",
}


def catalog(kind):
    prefix = "data/" + PREFIX[kind]
    xml = ET.fromstring(get(S3, params={"prefix": prefix, "list-type": "2"}).content)
    if xml.findtext("{*}IsTruncated") != "false":
        raise ValueError("incomplete monthly catalog")
    keys = {c.findtext("{*}Key"): int(c.findtext("{*}Size")) for c in xml.findall("{*}Contents")}
    return {
        k[-11:-4]: {"url": f"{CDN}/{k}", "size": v, "checksum": True}
        for k, v in keys.items()
        if re.search(r"-\d{4}-\d{2}\.zip$", k) and k + ".CHECKSUM" in keys
    }


def acquire(root, kind, month, listing):
    if month not in listing:
        raise ValueError(f"missing verified {kind} archive for {month}")
    item = listing[month]
    rel = kind + "/" + item["url"].split("/")[-1]
    p = root / rel
    side = p.with_suffix(".provenance.json")
    if p.exists() and side.exists():
        r = json.loads(side.read_text())
        if (
            r["url"] == item["url"]
            and p.stat().st_size == item["size"]
            and digest(p) == r["sha256"]
        ):
            return r
    r = {**download_one(item, root / kind), "path": rel, "kind": kind, "month": month}
    save_json(side, r)
    return r


def rates(root, rec):
    with zipfile.ZipFile(root / rec["path"]) as z, z.open(z.namelist()[0]) as f:
        rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
    result = [
        (int(r["calc_time"]), float(r["last_funding_rate"]), int(r["funding_interval_hours"]))
        for r in rows
    ]
    if not result or any(not np.isfinite(r) or abs(r) >= 1 or h <= 0 for _, r, h in result):
        raise ValueError("invalid funding row")
    if any(a[0] >= b[0] for a, b in pairwise(result)):
        raise ValueError("unordered funding calendar")
    return result


def stats(month, rows):
    a = np.array([r for t, r, h in rows])
    start = month_start(month)
    days = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    slots = [round(t / (8 * HOUR)) * 8 * HOUR for t, _, _ in rows]
    complete = slots == list(range(start, start + days * DAY, 8 * HOUR)) and all(
        h == 8 and abs(s - t) < MINUTE for s, (t, _, h) in zip(slots, rows, strict=True)
    )
    return {
        "month": month,
        "count": len(rows),
        "positive_pct": 100 * float(np.mean(a > 0)),
        "negative_pct": 100 * float(np.mean(a < 0)),
        "zero_pct": 100 * float(np.mean(a == 0)),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "minimum": float(a.min()),
        "maximum": float(a.max()),
        "cumulative": float(a.sum()),
        "longest_negative": max(
            (sum(1 for _ in g) for flag, g in groupby(a < 0) if flag), default=0
        ),
        "complete_8h": complete,
    }


def select(rows, config):
    prior = [
        r
        for r in rows
        if r["complete_8h"]
        and r["month"] < "2023-10"
        and r["month"] not in config["periods"].values()
    ]
    adverse = sorted(
        (r for r in prior if r["cumulative"] < 0 and r["longest_negative"] >= 3),
        key=lambda r: (-r["longest_negative"], r["cumulative"], r["month"]),
    )
    if not adverse:
        raise ValueError("no qualifying negative month")
    neg = adverse[0]["month"]
    mixed = min(
        (r for r in prior if r["month"] != neg and r["positive_pct"] and r["negative_pct"]),
        key=lambda r: (abs(r["negative_pct"] - 50), r["month"]),
    )
    return {"negative": neg, "mixed": mixed["month"], **config["periods"]}


def discover(root, config):
    with ThreadPoolExecutor(6) as pool:
        cache = root / "study-month-catalog.json"
        if cache.exists():
            cats = json.loads(cache.read_text())
        else:
            cats = dict(zip(PREFIX, pool.map(catalog, PREFIX), strict=True))
            save_json(cache, cats)
        all_months = sorted(m for m in cats["funding"] if m <= config["scan_end"])
        records = list(pool.map(lambda m: acquire(root, "funding", m, cats["funding"]), all_months))
    history = {r["month"]: rates(root, r) for r in records}
    regimes = [stats(m, rows) for m, rows in history.items()]
    return cats, records, history, regimes, select(regimes, config)


def windows(selected, legacy):
    result = {}
    for name, m in selected.items():
        a = month_start(m)
        days = calendar.monthrange(int(m[:4]), int(m[5:]))[1]
        result[name] = (a + HOUR, a + days * DAY - MINUTE)
    for w in legacy["windows"]:
        result[w["name"]] = (ms(w["enter_utc"]), ms(w["exit_utc"]))
    return result


def minute_table(root, r):
    with zipfile.ZipFile(root / r["path"]) as z:
        with z.open(z.namelist()[0]) as f:
            header = not f.readline().split(b",")[0].isdigit()
        with z.open(z.namelist()[0]) as f:
            table = pc.read_csv(
                f,
                read_options=pc.ReadOptions(
                    column_names=[str(i) for i in range(12)], skip_rows=int(header)
                ),
            )
    t = table["0"].to_numpy()
    unit = 1000 if t[0] > 10**14 else 1
    if np.any(table["6"].to_numpy() != t + MINUTE * unit - 1):
        raise ValueError(f"invalid candle endpoints {r['path']}")
    t = t // unit
    data = np.column_stack([table[str(i)].to_numpy() for i in (1, 2, 3, 4, 5, 7, 9, 10)])
    if (
        not np.isfinite(data).all()
        or np.any(data[:, :4] <= 0)
        or np.any(data[:, 2] > np.minimum(data[:, 0], data[:, 3]))
        or np.any(data[:, 1] < np.maximum(data[:, 0], data[:, 3]))
        or np.any(data[:, 4:] < 0)
        or np.any(np.diff(t) != MINUTE)
    ):
        raise ValueError(f"invalid or missing bars {r['path']}")
    return t, data


def rest_month(root, m, start, end):
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={SYMBOL}&startTime={start}&endTime={end}&limit=1000"
    p = root / "funding" / f"study-history-{m}.json"
    side = p.with_suffix(".provenance.json")
    if side.exists() and p.exists():
        record = json.loads(side.read_text())
        if record["url"] != url or digest(p) != record["sha256"]:
            raise ValueError("REST snapshot integrity failure: " + m)
    else:
        rows = get(url).json()
        if not isinstance(rows, list) or not 0 < len(rows) < 1000:
            raise ValueError("missing or truncated REST funding history")
        save_funding_snapshot(p, rows)
        record = {
            "url": url,
            "path": p.relative_to(root).as_posix(),
            "sha256": digest(p),
            "kind": "rest",
            "month": m,
        }
        save_json(side, record)
    return json.loads(p.read_text()), record


def price_event(row, mark, i):
    """Prefer the exchange mark. Separate causal pricing from range sensitivity."""
    t = int(row["fundingTime"])
    observed = float(row.get("markPrice") or 0)
    if i < 1 or not np.isfinite(observed) or observed < 0:
        raise ValueError("invalid settlement mark or missing prior bar")
    point = observed or mark[i - 1, 3]
    return {
        "time": t,
        "rate": float(row["fundingRate"]),
        "mark": point,
        "low": observed or min(point, mark[i, 2]),
        "high": observed or max(point, mark[i, 1]),
        "source": "REST settlement" if observed else "prior completed mark close",
        "gap_ms": 0 if observed else t % MINUTE + 1,
    }


def combine(root, records, window, archive):
    start, end = window
    kinds = {}
    for kind in ("spot1m", "perp1m", "mark"):
        parts = [
            minute_table(root, r)
            for r in records
            if r["kind"] == kind and r["month"] in months(start, end)
        ]
        t = np.concatenate([pair[0] for pair in parts])
        ix = (t >= start - MINUTE) & (t <= end)
        if not np.array_equal(t[ix], np.arange(start - MINUTE, end + 1, MINUTE)):
            raise ValueError(f"incomplete {kind} replay coverage: {iso(start)}")
        kinds[kind] = np.concatenate([pair[1] for pair in parts])[ix]
    prior = sorted((t, r) for rows in archive.values() for t, r, _ in rows if t < start - MINUTE)
    events = {}
    rest_records = []
    for m in months(start, end):
        a = month_start(m)
        b = a + calendar.monthrange(int(m[:4]), int(m[5:]))[1] * DAY
        if not stats(m, archive[m])["complete_8h"]:
            raise ValueError("incomplete eight-hour settlement calendar for " + m)
        rows, rec = rest_month(root, m, a, b - 1)
        rest_records.append(rec)
        archived = {round(t / MINUTE): r for t, r, _ in archive[m]}
        if len(rows) != len(archived) or len(
            {round(int(x["fundingTime"]) / MINUTE) for x in rows}
        ) != len(rows):
            raise ValueError("incomplete or duplicate API settlement")
        for row in rows:
            t = int(row["fundingTime"])
            rate = float(row["fundingRate"])
            if (
                not np.isfinite(rate)
                or row["symbol"] != SYMBOL
                or abs(rate - archived[round(t / MINUTE)]) > 1e-12
            ):
                raise ValueError("archive/REST mismatch")
            if not start <= t < end + MINUTE:
                continue
            i = (t - start) // MINUTE + 1
            if i in events:
                raise ValueError("duplicate settlement")
            events[i] = price_event(row, kinds["mark"], i)
    return kinds, events, prior, rest_records


def prepare(root, config, legacy):
    cats, records, history, regimes, selected = discover(root, config)
    intervals = windows(selected, legacy)
    needed = sorted(
        {
            (m, k)
            for a, b in intervals.values()
            for m in months(a, b)
            for k in ("mark", "spot1m", "perp1m")
        }
    )
    with ThreadPoolExecutor(6) as pool:
        market = list(pool.map(lambda job: acquire(root, job[1], job[0], cats[job[1]]), needed))
    return intervals, records + market, history, regimes
