"""Discover S3 objects before downloading them. All dates are inclusive UTC."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import requests

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
MARKET = "data/futures/um/daily"
LOG = logging.getLogger(__name__)


def dates(start: str, end: str):
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    if day > last:
        raise ValueError("end precedes start")
    while day <= last:
        yield day.isoformat()
        day += timedelta(days=1)


def retry(func):
    for attempt in range(4):
        try:
            return func()
        except (requests.RequestException, OSError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def get(url, **kwargs):
    def request():
        response = requests.get(url, timeout=(12, 90), **kwargs)
        response.raise_for_status()
        return response

    return retry(request)


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def discover(symbol: str, start: str, end: str, kinds=("trades", "bookTicker")):
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        raise ValueError("expected an uppercase USD-M USDT pair")
    days = list(dates(start, end))

    def listing(job):
        month, kind = job
        prefix = f"{MARKET}/{kind}/{symbol}/{symbol}-{kind}-{month}-"
        root = ET.fromstring(get(S3, params={"prefix": prefix, "list-type": "2"}).content)
        items = {
            c.findtext("{*}Key"): int(c.findtext("{*}Size")) for c in root.findall("{*}Contents")
        }
        result = []
        for day in days:
            if day.startswith(month):
                key = f"{prefix}{day[-2:]}.zip"
                if items.get(key, 0) <= 0:
                    raise ValueError(f"missing or empty listed archive: {key}")
                result.append(
                    {
                        "day": day,
                        "kind": kind,
                        "url": f"{CDN}/{key}",
                        "size": items[key],
                        "checksum": key + ".CHECKSUM" in items,
                    }
                )
        return result

    jobs = [(m, k) for m in sorted({d[:7] for d in days}) for k in kinds]
    with ThreadPoolExecutor(4) as pool:
        return sorted(
            (v for batch in pool.map(listing, jobs) for v in batch),
            key=lambda item: (item["day"], item["kind"]),
        )


def download_one(item, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    url = item["url"]
    path = folder / url.split("/")[-1]
    expected = get(url + ".CHECKSUM").text.split()[0].lower() if item["checksum"] else None
    if expected and not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError(f"invalid checksum response for {url}")

    def valid(file):
        return file.stat().st_size == item["size"] and (
            expected is None or digest(file) == expected
        )

    def transfer():
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with requests.get(url, stream=True, timeout=(12, 90)) as response:
                response.raise_for_status()
                with tmp_path.open("wb") as out:
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
            if not valid(tmp_path):
                raise OSError(f"size or checksum mismatch: {url}")
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    if not path.exists() or not valid(path):
        retry(transfer)
    LOG.info("verified %s (%s bytes)", path.name, item["size"])
    return dict(
        **item,
        file=path.name,
        sha256=expected or digest(path),
        verified_utc=datetime.now(UTC).isoformat(),
    )


def acquire(symbol, start, end, root, workers=3):
    items = discover(symbol, start, end)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        return list(pool.map(lambda item: download_one(item, root / "raw"), items))
