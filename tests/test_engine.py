from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mmbacktest.analytics import PerformanceAnalyzer
from mmbacktest.config import Config
from mmbacktest.data.feed import BookUpdateEvent, Day, FundingEvent
from mmbacktest.engine import EventEngine
from mmbacktest.portfolio import PortfolioLedger
from mmbacktest.strategy import InventoryRiskManager, InventorySkew, MarketState, RollingVariance

MS = 10**6


def session(books, trades, stop=7000):
    ts, bid, ask = zip(*books, strict=True)
    book = {
        "ts": np.array(ts, dtype="int64") * MS,
        "bid": np.array(bid),
        "ask": np.array(ask),
        "bid_qty": np.ones(len(ts)),
        "ask_qty": np.ones(len(ts)),
    }
    tape = {
        key: np.array([r[i] for r in trades])
        for i, key in enumerate(("ts", "price", "qty", "sell"))
    }
    tape["ts"] = tape["ts"].astype("int64") * MS
    return Day(0, stop * MS, book, tape, [])


@pytest.fixture
def adversary():
    raw = json.loads((Path(__file__).parent / "fixtures/adversarial.json").read_text())
    return session(raw["book_ms"], raw["trades_ms"], raw["stop_ms"])


@pytest.fixture
def cfg():
    return replace(
        Config(),
        tick=1,
        step=0.01,
        spread_bps=200,
        quantity=0.1,
        inventory_limit=0.1,
        min_notional=0,
        min_price=1,
        refresh_ms=10000,
        stale_ms=10000,
        lifetime_ms=12000,
    )


def simulate(day, cfg, name="fixed"):
    e = EventEngine(cfg, name)
    e.replay(day)
    e.finish()
    return e


def test_strict_direction_volume_activation_and_tie_order(adversary, cfg):
    e = simulate(adversary, cfg)
    assert [f.ts // MS for f in e.fills] == [54, 55]
    assert [f.qty for f in e.fills] == pytest.approx([0.04, 0.06])
    assert [f.price for f in e.fills] == [99, 99]
    assert [f.mid for f in e.fills] == [100, 90]
    assert e.counts["fully_filled"] == 1
    assert e.counts["orders_partially_filled"] == 1
    assert e.max_inventory == pytest.approx(0.1)


def test_markouts_stale_and_end_of_sample(adversary, cfg):
    e = simulate(adversary, cfg)
    f = e.fills[0]
    assert f.capture == pytest.approx(0.04)
    assert f.marks[1] == pytest.approx((102 - 100) * 0.04)
    assert f.marks[5] == pytest.approx((89 - 100) * 0.04)
    assert f.marks[30] is None and f.marks[60] is None
    stale = simulate(adversary, replace(cfg, stale_ms=100)).fills[1]
    assert stale.marks[1] is not None
    d = session([(0, 99, 101)], [(51, 98, 1, True)])
    assert simulate(d, replace(cfg, stale_ms=100)).fills[0].marks[1] is None


def test_no_fill_and_zero_risk(adversary, cfg):
    m = PerformanceAnalyzer.summarize(simulate(adversary, replace(cfg, fill_model="none")))
    assert m["net_pnl"] == m["fill_count"] == 0
    assert m["pnl_per_rms_inventory"] is None
    assert m["markouts"]["all"]["1"]["directional"] is None


def test_volume_cannot_be_routed_twice(cfg):
    d = session([(0, 99, 101)], [(51, 98, 0.4, True)])
    e = simulate(d, cfg)
    assert len(e.fills) == 1 and sum(f.qty for f in e.fills) == pytest.approx(0.04)


def test_cancellation_race_and_no_instant_replacement(cfg):
    d = session(
        [(0, 99, 101), (90, 104, 106)],
        [(110, 98, 0.2, True), (130, 98, 0.2, True), (131, 98, 1, True)],
    )
    c = replace(cfg, refresh_ms=100, cancel_latency_ms=30)
    e = simulate(d, c)
    assert [f.ts // MS for f in e.fills] == [110, 130]
    assert len({f.order for f in e.fills}) == 1
    assert e.counts["canceled"] >= 1


def test_expiry_wins_timestamp_tie(cfg):
    d = session([(0, 99, 101)], [(149, 98, 0.2, True), (150, 98, 10, True)])
    e = simulate(d, replace(cfg, lifetime_ms=100))
    assert [f.ts // MS for f in e.fills] == [149]


def test_post_only_recheck_and_publication_clock(cfg):
    day = session([(0, 99, 101), (100, 89, 91)], [(60, 88, 5, True)])
    day.exchange = dict(day.book, ts=np.array([0, 40 * MS]))
    e = simulate(day, cfg)
    assert not e.fills
    assert e.counts["post_only_or_stale_rejected"] == 1
    assert day.at(50 * MS).mid == 100
    assert day.at(50 * MS, venue=True).mid == 90


def test_queue_depletion_and_unknown_depth(cfg):
    day = session([(0, 99, 101)], [(51, 99, 0.8, True), (52, 99, 0.4, True)])
    e = simulate(day, replace(cfg, fill_model="queue"))
    assert len(e.fills) == 1 and e.fills[0].qty == pytest.approx(0.02)
    assert not simulate(day, cfg).fills
    outer = session([(0, 99, 101)], [(52, 98, 100, True)])
    assert not simulate(outer, replace(cfg, fill_model="queue", spread_bps=400)).fills


def test_qty_ticks_limits_and_minimums(cfg):
    book = BookUpdateEvent(0, 99, 1, 101, 1)
    check = InventoryRiskManager.order
    assert check(cfg, book, 99.8, 1, 7) == (99, 3)
    assert check(cfg, book, 100.2, -1, -7) == (101, 3)
    assert check(cfg, book, 150, 1, 10) is None
    assert check(cfg, book, 50, -1, -10) is None
    assert check(cfg, book, 150, 1, 0)[0] < book.ask
    assert check(cfg, book, 50, -1, 0)[0] > book.bid
    assert check(replace(cfg, min_notional=50), book, 99, 1, 0) is None


def test_average_cost_flip_and_fee_identity():
    l = PortfolioLedger(1000, 0.1)
    assert l.fill(1, 10, 100, 2) == pytest.approx(0.02)
    l.fill(1, 10, 120, 2)
    assert l.entry == 110
    l.fill(-1, 30, 130, 2)
    assert l.realized == 40 and l.entry == 130 and l.inventory == -1
    assert l.unrealized(120) == 10
    assert l.equity(120) == pytest.approx(1050 - l.fees)
    l.fill(1, 10, 120, 2)
    assert l.lots == 0 and l.entry == 0 and l.realized == 50


def test_funding_uses_pretrade_position_and_venue_mark(cfg):
    d = session([(0, 99, 101)], [(51, 98, 1, True), (100, 102, 1, False)])
    d.funding = [FundingEvent(100 * MS, 0.01, 110)]
    e = simulate(d, cfg)
    assert e.ledger.funding == pytest.approx(-0.11)
    assert PerformanceAnalyzer.summarize(e)["reconciliation_error"] == pytest.approx(0, abs=1e-9)


def test_risk_time_integrals_and_book_event_drawdown(cfg):
    d = session([(0, 99, 101), (500, 79, 81), (600, 119, 121)], [(100, 98, 1, True)], 1000)
    e = simulate(d, replace(cfg, maker_fee_bps=0))
    m = PerformanceAnalyzer.summarize(e)
    assert m["abs_inventory_seconds"] == pytest.approx(0.09)
    assert m["rms_inventory"] == pytest.approx((0.9 * 0.1**2) ** 0.5)
    assert m["holding_seconds"] == pytest.approx(0.9)
    assert m["max_drawdown"] == pytest.approx(2)


def test_no_future_observation_and_determinism(adversary, cfg):
    old = simulate(adversary, cfg)
    assert PerformanceAnalyzer.summarize(old) == PerformanceAnalyzer.summarize(
        simulate(adversary, cfg)
    )
    day = session(
        [(0, 99, 101), (54, 89, 91), (1053, 2, 4)], [(54, 98, 0.4, True), (55, 98, 2, True)]
    )
    other = simulate(day, cfg)
    assert [(f.ts, f.mid, f.price, f.qty) for f in old.fills] == [
        (f.ts, f.mid, f.price, f.qty) for f in other.fills
    ]
    assert old.fills[0].marks[1] != other.fills[0].marks[1]


def test_volatility_window_and_inventory_direction(cfg):
    v = RollingVariance(2)
    assert v.update(0, 100) == 0
    assert v.update(10**9, 110) == pytest.approx(110**2 * np.log(1.1) ** 2)
    v.update(2 * 10**9, 110)
    assert v.update(3 * 10**9, 110) == pytest.approx(0, abs=1e-10)
    b = BookUpdateEvent(0, 99, 1, 101, 1)
    s = InventorySkew()
    low = s.quotes(MarketState(0, b, 0.1, 0.01), cfg)
    high = s.quotes(MarketState(0, b, -0.1, 0.01), cfg)
    assert low[0] < high[0] and low[1] < high[1]
    assert InventorySkew(False).quotes(MarketState(0, b, 0.1, 0.01), cfg) == pytest.approx(
        InventorySkew(False).quotes(MarketState(0, b, -0.1, 0.01), cfg)
    )


def test_shared_assumptions_same_quotes_same_fills(adversary, cfg):
    c = replace(cfg, gamma=0, vol_width=0, maker_fee_bps=0)
    ledgers = [simulate(adversary, c, name).ledger for name in ("fixed", "adaptive", "no_skew")]
    assert ledgers[0] == ledgers[1] == ledgers[2]


def test_symmetric_sell_execution(cfg):
    day = session([(0, 99, 101)], [(51, 102, 1, True), (52, 101, 1, False), (53, 102, 1, False)])
    e = simulate(day, cfg)
    assert [(f.ts // MS, f.side, f.price) for f in e.fills] == [(53, -1, 101)]


def test_fill_provenance_identifies_original_print_at_tied_time(cfg):
    day = session([(0, 99, 101)], [(54, 98, 0.4, False), (54, 98, 0.4, True), (54, 98, 0.4, True)])
    day.tape["id"] = np.array([5001, 5003, 5004])
    e = simulate(day, cfg)
    assert [f.trade_id for f in e.fills] == [5003, 5004]
    assert sum(f.qty for f in e.fills) == pytest.approx(0.08)
