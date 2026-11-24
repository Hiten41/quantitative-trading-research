"""Queue proxy, order lifecycle, and deterministic scheduling records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from math import inf

from .config import Config
from .strategy import floor_units


class EventType(IntEnum):
    FUNDING = -3
    EXPIRY = -2
    CANCELLATION = 1
    ACTIVATION = 2
    QUOTE_DECISION = 3
    SAMPLE = 4


@dataclass(order=True, slots=True)
class ScheduledEvent:
    ts: int
    kind: EventType
    sequence: int
    payload: object = field(compare=False, default=None)


@dataclass(slots=True)
class Order:
    id: int
    side: int
    price: float
    lots: int
    remaining: int
    status: str = "pending"
    active: int = -1
    expires: int = -1
    queue: float = inf
    cancelling: bool = False


@dataclass(slots=True)
class FillEvent:
    ts: int
    order: int
    side: int
    price: float
    qty: float
    mid: float
    fee: float
    inventory: float
    cash: float
    trade_id: int
    marks: dict[int, float | None] = field(default_factory=dict)

    @property
    def capture(self) -> float:
        return self.side * (self.mid - self.price) * self.qty


@dataclass(frozen=True, slots=True)
class RiskEvent:
    ts: int
    reason: str


class ExecutionSimulator:
    @staticmethod
    def allocate(order: Order, price: float, volume: float, ts: int, c: Config) -> int:
        """One print is routed to at most one live order on the eligible side."""
        if (
            c.fill_model == "none"
            or order.status != "active"
            or ts <= order.active
            or ts >= order.expires
            or volume <= 0
        ):
            return 0
        through = order.side * (order.price - price)
        if through < 0 or (through == 0 and c.fill_model != "queue"):
            return 0
        if through == 0:
            consumed = min(order.queue, volume)
            order.queue -= consumed
            volume -= consumed
        return min(order.remaining, floor_units(volume * c.participation, c.step))


class OrderManager:
    """One outstanding order per side, including pending-cancel exposure."""

    def __init__(self) -> None:
        self.sides: dict[int, Order] = {}
        self.sequence = 0

    def submit(self, side: int, price: float, lots: int) -> Order:
        if side in self.sides:
            raise RuntimeError("replacement before acknowledgement")
        self.sequence += 1
        order = Order(self.sequence, side, price, lots, lots)
        self.sides[side] = order
        return order
