"""Passive policies with explicit limits on each side's desired ending inventory."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import Config
from .data.pipeline import NS

if TYPE_CHECKING:
    from .strategy import MarketState


@dataclass(frozen=True, slots=True)
class QuoteTargets:
    bid: float
    ask: float
    buy_limit: float  # maximum resulting long position
    sell_limit: float  # minimum resulting signed position


class TrendState:
    """Fast and slow clock-time EMA; warm up for one slow half-life."""

    def __init__(self) -> None:
        self.start = 0
        self.last: int | None = None
        self.fast = 0.0
        self.slow = 0.0

    def update(self, t: int, mid: float, half_life: float, reset_gap: float) -> float | None:
        if self.last is None or t - self.last > reset_gap * NS:
            self.fast = self.slow = mid
            self.start = t
        else:
            dt = (t - self.last) / NS
            if dt <= 0:
                raise ValueError("nonincreasing signal clock")
            decay = math.log(2) * dt / half_life
            self.fast += -math.expm1(-5 * decay) * (mid - self.fast)
            self.slow += -math.expm1(-decay) * (mid - self.slow)
        self.last = t
        if (t - self.start) < half_life * NS:
            return None
        return 10000 * (self.fast / self.slow - 1)


class SelectiveMaker:
    """Quote discounted entries; allow passive risk reduction when trend turns."""

    def __init__(self, directional: bool = False) -> None:
        self.trend = TrendState()
        self.directional = directional
        self.direction = 0

    def quotes(self, s: MarketState, c: Config) -> QuoteTargets:
        mid = s.book.mid
        drift = self.trend.update(s.ts, mid, c.signal_half_life_s, 1.5 * c.refresh_ms / 1000)
        # Half the reciprocal width pays a side's maker fee. Entry orders still
        # require a strict print through their actual resting price.
        width = max(c.entry_width_bps, 2 * max(0.0, c.maker_fee_bps))
        offset = mid * width / 10000
        bid, ask = mid - offset, mid + offset
        if drift is None:
            self.direction = 0
            return QuoteTargets(s.book.bid, s.book.ask, 0.0, 0.0)
        if drift >= c.trend_band_bps:
            self.direction = 1
        elif drift <= -c.trend_band_bps:
            self.direction = -1
        elif self.direction * drift <= 0:
            self.direction = 0
        q = s.inventory
        if self.directional:
            target = self.direction * c.inventory_limit
            if target > q and q < 0:
                bid = s.book.bid
            if target < q and q > 0:
                ask = s.book.ask
            return QuoteTargets(bid, ask, target, target)
        buy_limit = 0.0 if self.direction < 0 else c.inventory_limit
        sell_limit = 0.0 if self.direction > 0 else -c.inventory_limit
        # A reducing quote stops at flat, even when only a fraction of the
        # previous order filled. Matching-engine latency still applies.
        if q > 0:
            sell_limit = 0.0
            if self.direction < 0 or mid < s.entry - 3 * offset:
                ask = s.book.ask
                buy_limit = 0.0
            else:
                ask = max(ask, s.entry + 2 * offset)
        elif q < 0:
            buy_limit = 0.0
            if self.direction > 0 or mid > s.entry + 3 * offset:
                bid = s.book.bid
                sell_limit = 0.0
            else:
                bid = min(bid, s.entry - 2 * offset)
        return QuoteTargets(bid, ask, buy_limit, sell_limit)


class AnchoredMaker:
    """Buy below a trailing reference, offer above it, and exit toward that reference."""

    def __init__(self) -> None:
        self.trend = TrendState()
        self.paused = 0

    def quotes(self, s: MarketState, c: Config) -> QuoteTargets:
        mid = s.book.mid
        drift = self.trend.update(s.ts, mid, c.signal_half_life_s, 1.5 * c.refresh_ms / 1000)
        if drift is None:
            self.paused = 0
            return QuoteTargets(s.book.bid, s.book.ask, 0.0, 0.0)
        anchor = self.trend.slow
        width = anchor * max(c.entry_width_bps, 2 * max(0.0, c.maker_fee_bps)) / 10000
        displacement = mid - anchor
        if self.paused * displacement >= 0:
            self.paused = 0
        if displacement < -3 * width:
            self.paused = 1
        elif displacement > 3 * width:
            self.paused = -1
        bid = min(s.book.bid, anchor - width)
        ask = max(s.book.ask, anchor + width)
        upper = 0.0 if self.paused == 1 else c.inventory_limit
        lower = 0.0 if self.paused == -1 else -c.inventory_limit
        if s.inventory > 0:
            ask = s.book.ask if self.paused == 1 else max(s.book.ask, anchor)
            lower = 0.0
        if s.inventory < 0:
            bid = s.book.bid if self.paused == -1 else min(s.book.bid, anchor)
            upper = 0.0
        return QuoteTargets(bid, ask, upper, lower)
