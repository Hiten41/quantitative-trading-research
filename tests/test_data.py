from __future__ import annotations

import zipfile

import numpy as np
import pyarrow.parquet as pq
import pytest

from mmbacktest.data.archive import dates, digest
from mmbacktest.data.pipeline import DAY, FIELDS, NS, normalize, normalize_time, utc_ns

DAY_NAME = "2023-09-02"


@pytest.mark.parametrize("factor", [1, 1000, 1_000_000])
def test_utc_units(factor):
    start = 1693612800 * NS
    assert utc_ns(DAY_NAME) == start
    assert normalize_time(np.array([start // factor]))[0] == start


@pytest.mark.parametrize("bad", [[1], [float("nan")], [1693612800000, 1693612800000000]])
def test_bad_timestamps(bad):
    with pytest.raises(ValueError):
        normalize_time(np.array(bad))


def archive(tmp_path, kind, header, mutation=None, unit=10**6):
    start = utc_ns(DAY_NAME)
    rows = []
    for i, ts in enumerate(range(start, start + DAY, 60 * NS)):
        t = ts // unit
        row = (
            [i + 1, 100, 0.1, 10, t, str(i % 2 == 0).lower()]
            if kind == "trades"
            else [i + 1, 99, 1, 101, 2, t, t + 1]
        )
        rows.append(row)
    if mutation:
        mutation(rows)
    p = tmp_path / "raw" / (kind + ".zip")
    p.parent.mkdir(exist_ok=True)
    lines = [",".join(map(str, r)) for r in rows]
    if header:
        lines.insert(0, ",".join(FIELDS[kind]))
    with zipfile.ZipFile(p, "w") as out:
        out.writestr(kind + ".csv", "\n".join(lines))
    return {"file": p.name, "sha256": digest(p), "kind": kind, "day": DAY_NAME}


@pytest.mark.parametrize("header", [True, False])
@pytest.mark.parametrize("kind", ["trades", "bookTicker"])
def test_header_utc_and_direction(tmp_path, header, kind):
    item = archive(tmp_path, kind, header, unit=1000)
    record = normalize(item, tmp_path, "ETHUSDT")
    assert record["events"] == 1440
    path = tmp_path / "ETHUSDT" / DAY_NAME / (kind + ".parquet")
    t = pq.read_table(path)
    assert t["ts"][0].as_py() == utc_ns(DAY_NAME)
    if kind == "trades":
        assert t["sell"][:2].to_pylist() == [True, False]
    assert record["parquet_sha256"] == digest(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r[1].__setitem__(3, 98),
        lambda r: r[2].__setitem__(0, r[1][0]),
        lambda r: r[2].__setitem__(2, 0),
        lambda r: r[1].__setitem__(1, "nan"),
        lambda r: r.__delitem__(slice(2, 10)),
        lambda r: r.pop(0) and r.pop(0) and r.pop(0),
        lambda r: r[2].__setitem__(6, r[2][5] - 1),
    ],
)
def test_invalid_book_is_rejected(tmp_path, mutate):
    item = archive(tmp_path, "bookTicker", True, mutate)
    with pytest.raises(ValueError):
        normalize(item, tmp_path, "ETHUSDT")
    assert not (tmp_path / "ETHUSDT" / DAY_NAME / "bookTicker.json").exists()


def test_missing_date_interval():
    with pytest.raises(ValueError):
        list(dates("2023-09-05", "2023-09-01"))
