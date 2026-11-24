"""Causal quote policies. All prices are USDT per ETH and time is in seconds."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .data.feed import BookUpdateEvent
from .data.pipeline import NS
from .selective import AnchoredMaker, QuoteTargets, SelectiveMaker


class RollingVariance:
    """Realized log variance per second on the trailing quote-decision grid."""

    def __init__(self, window: float) -> None:
        self.window = int(window * NS)
        self.returns: deque[tuple[int, float]] = deque()
        self.previous: tuple[int, float] | None = None
        self.total = 0.0

    def update(self, t: int, mid: float) -> float:
        if self.previous:
            prev, price = self.previous
            if t <= prev:
                raise ValueError("nonincreasing volatility clock")
            value = math.log(mid / price) ** 2
            self.returns.append((prev, value))
            self.total += value
        self.previous = t, mid
        while self.returns and self.returns[0][0] < t - self.window:
            self.total -= self.returns.popleft()[1]
        if not self.returns:
            return 0.0
        return mid**2 * max(0.0, self.total) * NS / (t - self.returns[0][0])


@dataclass(frozen=True, slots=True)
class MarketState:
    ts: int
    book: BookUpdateEvent
    inventory: float
    variance: float
    entry: float = 0.0


class Strategy(Protocol):
    def quotes(self, s: MarketState, c: Config) -> tuple[float, float] | QuoteTargets: ...


class FixedSpread:
    def quotes(self, s: MarketState, c: Config) -> tuple[float, float]:
        mid = s.book.mid
        half = mid * c.spread_bps / 20000
        return mid - half, mid + half


class InventorySkew:
    def __init__(self, skew: bool = True) -> None:
        self.skew = skew

    def quotes(self, s: MarketState, c: Config) -> tuple[float, float]:
        mid = s.book.mid
        variance = s.variance * c.horizon_s
        reservation = mid - (c.gamma * s.inventory * variance if self.skew else 0)
        half = (
            mid * (c.spread_bps / 2 + max(0, c.maker_fee_bps)) / 10000
            + c.vol_width * math.sqrt(variance)
            + c.gamma * c.quantity * variance / 2
        )
        return reservation - half, reservation + half


def strategy(name: str) -> Strategy:
    if name == "fixed":
        return FixedSpread()
    if name in ("adaptive", "no_skew"):
        return InventorySkew(name == "adaptive")
    if name == "anchored_maker":
        return AnchoredMaker()
    if name in ("selective_maker", "pullback_maker"):
        return SelectiveMaker(directional=name == "pullback_maker")
    raise ValueError(f"unknown strategy {name}")


def floor_units(x: float, unit: float) -> int:
    # Correct only representation error at an exact lattice point.
    ratio = x / unit
    return math.floor(ratio + 2 * math.ulp(ratio))


class InventoryRiskManager:
    @staticmethod
    def order(
        c: Config,
        book: BookUpdateEvent,
        raw: float,
        side: int,
        inventory_lots: int,
        side_limit: float | None = None,
    ) -> tuple[float, int] | None:
        if raw <= 0:
            return None
        price_ticks = (
            floor_units(min(raw, book.ask - c.tick), c.tick)
            if side == 1
            else -floor_units(-max(raw, book.bid + c.tick), c.tick)
        )
        price = round(price_ticks * c.tick, 12)
        capacity = floor_units(c.inventory_limit, c.step) - side * inventory_lots
        if side_limit is not None:
            capacity = min(capacity, floor_units(side * side_limit, c.step) - side * inventory_lots)
        lots = min(floor_units(min(c.quantity, c.max_qty), c.step), capacity)
        if (
            lots <= 0
            or lots * c.step < c.min_qty
            or price * lots * c.step < c.min_notional
            or not c.min_price <= price <= c.max_price
            or (price >= book.ask if side == 1 else price <= book.bid)
        ):
            return None
        return price, lots
