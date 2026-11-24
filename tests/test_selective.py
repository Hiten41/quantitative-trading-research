from dataclasses import replace

import pytest

from mmbacktest.config import Config
from mmbacktest.data.feed import BookUpdateEvent
from mmbacktest.data.pipeline import NS
from mmbacktest.selective import SelectiveMaker, TrendState
from mmbacktest.strategy import InventoryRiskManager, MarketState


def test_signal_warmup_and_gap_restart():
    signal = TrendState()
    assert signal.update(0, 100.0, 10.0, 6.0) is None
    assert signal.update(5 * NS, 101.0, 10.0, 6.0) is None
    assert signal.update(10 * NS, 103.0, 10.0, 6.0) > 0
    assert signal.update(18 * NS, 105.0, 10.0, 6.0) is None


def test_directional_exit_cannot_reverse_on_partial_inventory():
    c = replace(Config(), min_notional=0.0)
    book = BookUpdateEvent(0, 99.0, 1.0, 101.0, 1.0)
    quotes = SelectiveMaker(True).quotes(MarketState(0, book, 0.037, 0.0, 99.0), c)
    assert InventoryRiskManager.order(c, book, quotes.bid, 1, 37, quotes.buy_limit) is None
    price, lots = InventoryRiskManager.order(c, book, quotes.ask, -1, 37, quotes.sell_limit)
    assert price >= book.ask and lots == 37


@pytest.mark.parametrize("direction", [-1, 1])
def test_direction_and_order_headroom(direction):
    c = replace(Config(), signal_half_life_s=10, trend_band_bps=1, min_notional=0.0)
    policy = SelectiveMaker(True)
    for i in range(3):
        t = i * 5 * NS
        mid = 100.0 + direction * i
        book = BookUpdateEvent(t, mid - 0.01, 1.0, mid + 0.01, 1.0)
        quotes = policy.quotes(MarketState(t, book, 0.0, 0.0), c)
    assert quotes.buy_limit == quotes.sell_limit == direction * c.inventory_limit
    assert InventoryRiskManager.order(c, book, mid, -direction, 0, quotes.buy_limit) is None
    assert InventoryRiskManager.order(c, book, mid, direction, 0, quotes.buy_limit) is not None


def test_selective_exit_uses_known_cost_and_preserves_hard_limit():
    c = replace(Config(), signal_half_life_s=5, entry_width_bps=10, min_notional=0.0)
    p = SelectiveMaker()
    b = BookUpdateEvent(0, 99.99, 1.0, 100.01, 1.0)
    p.quotes(MarketState(0, b, 0.0, 0.0), c)
    quotes = p.quotes(MarketState(5 * NS, b, 0.1, 0.0, 100.0), c)
    assert quotes.ask == pytest.approx(100.2)
    assert quotes.sell_limit == 0
    assert InventoryRiskManager.order(c, b, quotes.bid, 1, 500, 10) is None


def test_anchor_waits_for_mean_reversion_after_large_drop():
    from mmbacktest.selective import AnchoredMaker
    from mmbacktest.strategy import strategy

    c = replace(Config(), signal_half_life_s=10, entry_width_bps=15.0)
    policy = strategy("anchored_maker")
    assert isinstance(policy, AnchoredMaker)

    def decision(seconds, mid, inventory=0.0):
        t = seconds * NS
        b = BookUpdateEvent(t, mid - 0.01, 1.0, mid + 0.01, 1.0)
        return policy.quotes(MarketState(t, b, inventory, 0.0, 100.0), c)

    assert decision(0, 100.0).buy_limit == 0.0
    decision(5, 100.0)
    initial = decision(10, 100.0)
    assert initial.bid == pytest.approx(99.85)
    assert initial.ask == pytest.approx(100.15)
    drop = decision(15, 98.0, 0.1)
    assert drop.buy_limit == drop.sell_limit == 0.0
    assert drop.ask == pytest.approx(98.01)
    assert decision(20, 99.0).buy_limit == 0.0
    assert decision(25, 100.0).buy_limit > 0.0
