"""Synthetic unit books and payments. Published research uses the archived inputs."""

from __future__ import annotations

import csv
import gzip
import json
import zipfile
from dataclasses import replace

import numpy as np
import pytest

from fundingcarry.replay import execute, funding_ledger, simulate, spot_purchase
from fundingcarry.sources import HOUR, MINUTE, Bars, Book, books_at, iso


def book(t, bid, ask, qty=100.0):
    return Book(t * 1000, t * 1000, ((bid, qty), (bid - 1, qty)), ((ask, qty), (ask + 1, qty)))


def test_buy_fee_in_base_and_residual():
    gross, fee, dust = spot_purchase(0.5, 0.001, 0.0001)
    assert gross == pytest.approx(0.5006)
    assert gross - fee - dust == pytest.approx(0.5)
    assert 0 < dust < 0.0001
    assert spot_purchase(0.5, 0, 0.0001) == (0.5, 0, 0)


def test_cross_depth_slippage_and_capacity():
    b = book(1000, 99, 101, 1.0)
    c = {"depth_participation": 0.1, "max_book_age_ms": 100}
    buy = execute(b, "spot", "buy", 0.2, 1000, c, 2)
    assert buy.levels_used == 2 and buy.raw_vwap == pytest.approx(101.5)
    assert buy.price == pytest.approx(101.5 * 1.0002)
    sell = execute(b, "futures", "sell", 0.1, 1000, c, 2)
    assert sell.price == pytest.approx(99 * 0.9998)
    with pytest.raises(ValueError, match="insufficient"):
        execute(b, "spot", "buy", 0.201, 1000, c, 0)
    for t in (999, 1101):
        with pytest.raises(ValueError, match="stale"):
            execute(b, "spot", "buy", 0.01, t, c, 0)


def test_asof_snapshot_never_uses_next_book(tmp_path):
    path = tmp_path / "test.csv.gz"
    rows = []
    for t in (100000, 101000):
        r = {"exchange": "binance", "symbol": "ETHUSDT", "timestamp": t, "local_timestamp": t}
        for i in range(5):
            for side, px in (("bids", 99 - i), ("asks", 101 + i)):
                r[f"{side}[{i}].price"] = px + (100 if t > 100000 else 0)
                r[f"{side}[{i}].amount"] = 3.0
        rows.append(r)
    with gzip.open(path, "wt", newline="") as output:
        w = csv.DictWriter(output, fieldnames=rows[0])
        w.writeheader()
        w.writerows(rows)
    assert books_at(path, "binance", [100], 20)[100].asks[0][0] == 101
    with pytest.raises(ValueError, match="fresh"):
        books_at(path, "binance", [50], 20)


def rates_fixture(tmp_path, delete=False, mismatch=False):
    enter = 0
    leave = 32 * HOUR + 5
    rates = [0.001, -0.001, 0.001, -0.001]
    history = [
        {
            "symbol": "ETHUSDT",
            "fundingTime": (i + 1) * 8 * HOUR,
            "fundingRate": rate,
            "markPrice": "" if i < 2 else "102",
        }
        for i, rate in enumerate(rates)
    ]
    with zipfile.ZipFile(tmp_path / "rates.zip", "w") as z:
        z.writestr(
            "rates.csv",
            "calc_time,funding_interval_hours,last_funding_rate\n"
            + "\n".join(f"{r['fundingTime']},8,{r['fundingRate']}" for r in history),
        )
    if delete:
        history.pop()
    if mismatch:
        history[0]["fundingRate"] = 0.1
    (tmp_path / "history.json").write_text(json.dumps(history))
    records = [
        {"kind": "funding", "path": "rates.zip"},
        {"window": "trial", "kind": "history", "path": "history.json"},
    ]
    t = np.arange(0, leave + MINUTE, MINUTE)
    bars = Bars(t, *[np.full(len(t), v) for v in (100, 105, 95, 100)])
    return funding_ledger(tmp_path, records, {"name": "trial"}, bars, 0.5, enter, leave)


def test_signed_funding_uses_actual_mark_or_adverse_minute(tmp_path):
    flows = rates_fixture(tmp_path)
    assert [r["mark"] for r in flows] == [95, 105, 102, 102]
    assert [r["cashflow"] for r in flows] == pytest.approx([0.0475, -0.0525, 0.051, -0.051])


@pytest.mark.parametrize("delete,mismatch", [(True, False), (False, True)])
def test_incomplete_or_changed_rates_fail(tmp_path, delete, mismatch):
    with pytest.raises(ValueError):
        rates_fixture(tmp_path, delete=delete, mismatch=mismatch)


def test_wallet_reconciliation_and_collateral_stress():
    cfg = {
        "capital_usdt": 1000,
        "futures_collateral_usdt": 500,
        "hedge_eth": 0.5,
        "spot_lot_eth": 0.0001,
        "futures_lot_eth": 0.001,
        "spot_taker_bps": 10,
        "futures_taker_bps": 5,
        "spot_latency_ms": 250,
        "futures_latency_ms": 1250,
        "depth_participation": 0.1,
        "max_book_age_ms": 500,
    }
    end = 2 * HOUR + 1000
    w = {"enter_utc": iso(1000), "exit_utc": iso(end)}
    books = {
        (market, base + lag): book(base + lag, price - 1, price + 1)
        for base, price in ((1000, 100), (end, 120))
        for market, lag in (("binance", 250), ("binance-futures", 1250))
    }

    def bars(step):
        t = np.arange(0, end, step)
        return Bars(t, *[np.full(len(t), v) for v in (101, 103, 99, 102)])

    events = [
        {"time_ms": HOUR, "cashflow": 0.6, "source": "reported settlement mark"},
        {"time_ms": 2 * HOUR, "cashflow": -0.2, "source": "reported settlement mark"},
    ]
    m, path = simulate(cfg, w, books, events, bars(HOUR), bars(MINUTE), credit_multiple=0.5)
    assert m["funding"] == pytest.approx(0.1)
    assert m["basis_pnl_before_slippage"] == pytest.approx(-2)
    assert abs(m["reconciliation_error"]) < 1e-9
    assert path[-1]["equity"] == pytest.approx(m["ending_spot_cash"] + m["ending_futures_cash"])
    assert m["net_pnl"] < 0
    with pytest.raises(ValueError, match="buffer"):
        simulate(
            cfg,
            w,
            books,
            events,
            bars(HOUR),
            replace(bars(MINUTE), highs=np.full(len(bars(MINUTE).times), 1600.0)),
        )
