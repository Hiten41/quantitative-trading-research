from dataclasses import replace

import numpy as np
import pytest

from mmbacktest.config import Config
from mmbacktest.data.feed import BookUpdateEvent, Day
from mmbacktest.engine import EventEngine
from mmbacktest.strategy import InventoryRiskManager

S = 10**9


def tiny(start, stop, books, prints):
    return Day(
        start,
        stop,
        {
            "ts": np.array([t for t, p in books]),
            "bid": np.array([p - 1.0 for t, p in books]),
            "ask": np.array([p + 1.0 for t, p in books]),
            "bid_qty": np.ones(len(books)),
            "ask_qty": np.ones(len(books)),
        },
        {
            key: np.array([row[i] for row in prints])
            for i, key in enumerate(("ts", "price", "qty", "sell"))
        },
        [],
    )


def test_horizon_crosses_partition_without_resetting_inventory():
    c = replace(
        Config(),
        new_latency_ms=0,
        refresh_ms=10000,
        tick=1,
        spread_bps=200,
        min_notional=0,
        participation=1,
        stale_ms=10000,
    )
    e = EventEngine(c)
    e.replay(tiny(0, 2 * S, [(0, 100)], [(S + 1, 102, 1.0, False)]))
    assert e.fills[0].side == -1 and 1 not in e.fills[0].marks
    e.replay(tiny(2 * S, 3 * S, [(2 * S, 95)], []))
    e.finish()
    assert e.fills[0].marks[1] == pytest.approx(0.5)
    assert e.fills[0].marks[5] is None
    assert e.ledger.inventory == -0.1


def test_missing_partition_rejected():
    e = EventEngine(Config())
    e.replay(tiny(0, S, [(0, 100)], []))
    with pytest.raises(ValueError, match="discontinuous"):
        e.replay(tiny(2 * S, 3 * S, [(2 * S, 101)], []))


def test_rebate_requires_explicit_assumption():
    with pytest.raises(ValueError, match="rebate_assumption"):
        replace(Config(), maker_fee_bps=-0.1)
    assert (
        replace(
            Config(), maker_fee_bps=-0.1, rebate_assumption="Hypothetical rebate stress"
        ).maker_fee_bps
        < 0
    )


def test_earlier_resting_opposite_side_cannot_be_crossed():
    e = EventEngine(Config())
    o = e.orders.submit(-1, 100, 2)
    o.status = "active"
    incoming = e.orders.submit(1, 101, 2)
    e.activation(100, incoming, BookUpdateEvent(99, 100.5, 1, 101.5, 1))
    assert e.counts["post_only_or_stale_rejected"] == 1
    assert e.orders.sides == {-1: o}


def test_threshold_zero_and_unsupported_order_size():
    b = BookUpdateEvent(1, 100, 1, 101, 1)
    assert InventoryRiskManager.order(replace(Config(), min_qty=1), b, 99.0, 1, 0) is None
