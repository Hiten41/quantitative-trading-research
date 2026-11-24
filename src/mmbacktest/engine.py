"""Exchange-time event loop. Scheduled events interleave with individual prints."""

from __future__ import annotations

import heapq
import math
from collections import Counter
from dataclasses import asdict
from itertools import count

import numpy as np

from .data.feed import Day
from .data.pipeline import NS
from .execution import EventType as E
from .execution import ExecutionSimulator, FillEvent, OrderManager, RiskEvent, ScheduledEvent
from .portfolio import PortfolioLedger
from .selective import QuoteTargets
from .strategy import InventoryRiskManager, MarketState, RollingVariance, strategy

HORIZONS = (1, 5, 30, 60)
MS = 10**6


class EventEngine:
    def __init__(self, c, name="fixed", audit=None):
        self.c = c
        self.name = name
        self.strategy = strategy(name)
        self.vol = RollingVariance(c.vol_window_s)
        self.orders = OrderManager()
        self.ledger = PortfolioLedger(c.capital, c.step)
        self.audit = audit
        self.timers = []
        self.sequence = count()
        self.counts = Counter()
        self.fills = []
        self.pending_marks = []
        self.samples = []
        self.daily = []
        self.cashflows = []
        self.prior = None
        self.venue_prior = None
        self.start = None
        self.stop = None
        self.peak = c.capital
        self.drawdown = 0.0
        self.drawdown_pct = 0.0
        self.max_inventory = 0.0
        self.abs_seconds = self.sq_seconds = self.holding_seconds = 0.0
        self.last_exposure = None
        self.variance = 0.0

    def schedule(self, t, kind, payload=None):
        heapq.heappush(self.timers, ScheduledEvent(t, kind, next(self.sequence), payload))

    def record(self, t, o, action):
        self.counts[action] += 1
        if self.audit:
            self.audit(t, o, action)

    def exposure(self, t):
        if self.last_exposure is not None:
            dt = (t - self.last_exposure) / NS
            q = self.ledger.inventory
            self.abs_seconds += dt * abs(q)
            self.sq_seconds += dt * q * q
            self.holding_seconds += dt * (q != 0)
        self.last_exposure = t

    def close_order(self, t, o, reason):
        self.record(t, o, reason)
        del self.orders.sides[o.side]
        o.status = reason

    def decision(self, t, book):
        c = self.c
        fresh = book and t - book.ts <= c.stale_ms * MS
        if fresh:
            self.variance = self.vol.update(t, book.mid)
            state = MarketState(t, book, self.ledger.inventory, self.variance, self.ledger.entry)
            plan = self.strategy.quotes(state, c)
            if isinstance(plan, QuoteTargets):
                prices = (plan.bid, plan.ask)
                limits = (plan.buy_limit, plan.sell_limit)
            else:
                prices = plan
                limits = (None, None)
        else:
            self.vol = RollingVariance(c.vol_window_s)
            self.counts[RiskEvent(t, "stale_decisions").reason] += 1
        for i, side in enumerate((1, -1)):
            target = (
                InventoryRiskManager.order(c, book, prices[i], side, self.ledger.lots, limits[i])
                if fresh
                else None
            )
            o = self.orders.sides.get(side)
            if o:
                if not o.cancelling and (
                    not target
                    or abs(target[0] - o.price) / c.tick + 1e-8 >= max(1, c.replace_ticks)
                    or o.remaining > target[1]
                ):
                    o.cancelling = True
                    self.record(t, o, "cancel_requested")
                    self.schedule(t + c.cancel_latency_ms * MS, E.CANCELLATION, o)
            elif target:
                o = self.orders.submit(side, *target)
                self.record(t, o, "submitted")
                self.counts[f"submitted_{side}"] += 1
                self.counts["submitted_lots"] += o.lots
                self.schedule(t + c.new_latency_ms * MS, E.ACTIVATION, o)
        self.schedule(t + c.refresh_ms * MS, E.QUOTE_DECISION)

    def activation(self, t, o, book):
        opposite = self.orders.sides.get(-o.side)
        self_cross = (
            opposite and opposite.status == "active" and o.side * (o.price - opposite.price) >= 0
        )
        if (
            self_cross
            or not book
            or t - book.ts > self.c.stale_ms * MS
            or (o.price >= book.ask if o.side == 1 else o.price <= book.bid)
        ):
            self.close_order(t, o, "post_only_or_stale_rejected")
            return
        o.status = "active"
        o.active = t
        o.expires = t + self.c.lifetime_ms * MS
        touch, size = (book.bid, book.bid_qty) if o.side == 1 else (book.ask, book.ask_qty)
        # Depth away from visible touch is unknown: no at-price fills there.
        o.queue = size if o.price == touch else math.inf
        self.record(t, o, "activated")
        self.schedule(o.expires, E.EXPIRY, o)

    def sample(self, t, book):
        if not book:
            return

        def quote(side):
            o = self.orders.sides.get(side)
            return o.price if o and o.status == "active" else None

        self.samples.append(
            {
                "ts": t,
                "mid": book.mid,
                "bid": book.bid,
                "ask": book.ask,
                "quote_bid": quote(1),
                "quote_ask": quote(-1),
                "inventory": self.ledger.inventory,
                "equity": self.ledger.equity(book.mid),
                "variance": self.variance,
            }
        )

    def timer(self, event, day):
        t, k, o = event.ts, event.kind, event.payload
        if k == E.FUNDING:
            amount = -self.ledger.inventory * o.mark * o.rate
            self.ledger.funding += amount
            self.cashflows.append((t, amount))
            self.changes.append((t, self.ledger.inventory, self.ledger.cash))
        elif k == E.QUOTE_DECISION:
            self.decision(t, day.at(t, self.prior))
        elif k == E.SAMPLE:
            self.sample(t, day.at(t, self.prior))
            self.schedule(t + 60 * NS, E.SAMPLE)
        elif self.orders.sides.get(o.side) is o:
            if k == E.ACTIVATION:
                visible = day.at(t, self.prior)
                venue = day.at(t, self.venue_prior, venue=True)
                self.activation(
                    t, o, venue if visible and t - visible.ts <= self.c.stale_ms * MS else None
                )
            else:
                self.close_order(t, o, "expired" if k == E.EXPIRY else "canceled")

    def execute(self, t, o, price, volume, day, print_index):
        # Ignore ineligible prints before querying the causal book.
        if (
            o.side * (o.price - price) < 0
            or (price == o.price and self.c.fill_model != "queue")
            or o.status != "active"
        ):
            return
        book = day.at(t, self.prior, inclusive=False)
        if not book or t - book.ts > self.c.stale_ms * MS:
            return
        lots = ExecutionSimulator.allocate(o, price, volume, t, self.c)
        if not lots:
            return
        self.exposure(t)
        fee = self.ledger.fill(o.side, lots, o.price, self.c.maker_fee_bps)
        if abs(self.ledger.inventory) > self.c.inventory_limit + 1e-10:
            raise ArithmeticError("inventory breach")
        self.max_inventory = max(self.max_inventory, abs(self.ledger.inventory))
        fill = FillEvent(
            t,
            o.id,
            o.side,
            o.price,
            lots * self.c.step,
            book.mid,
            fee,
            self.ledger.inventory,
            self.ledger.cash,
            trade_id=int(day.tape["id"][print_index]) if "id" in day.tape else print_index,
        )
        self.fills.append(fill)
        self.pending_marks.append(fill)
        self.changes.append((t, fill.inventory, fill.cash))
        if lots < o.remaining and o.remaining == o.lots:
            self.counts["orders_partially_filled"] += 1
        o.remaining -= lots
        if o.remaining:
            self.record(t, o, "partial_fill")
        else:
            self.close_order(t, o, "fully_filled")

    def markouts(self, day):
        pending = []
        for f in self.pending_marks:
            for h in HORIZONS:
                target = f.ts + h * NS
                if h in f.marks or target >= day.stop:
                    continue
                b = day.at(target, self.prior)
                f.marks[h] = (
                    f.side * (b.mid - f.mid) * f.qty
                    if b and target - b.ts <= self.c.stale_ms * MS
                    else None
                )
            if len(f.marks) < len(HORIZONS):
                pending.append(f)
        self.pending_marks = pending

    def drawdowns(self, day, initial):
        """Revalue on every L1 update and each fill, including intermediate tied prints."""
        ts = day.book["ts"]
        bid, ask = day.book["bid"], day.book["ask"]

        def update(equity):
            if len(equity):
                peaks = np.maximum.accumulate(np.maximum(equity, self.peak))
                self.drawdown = max(self.drawdown, float((peaks - equity).max()))
                self.drawdown_pct = max(self.drawdown_pct, float(((peaks - equity) / peaks).max()))
                self.peak = float(peaks[-1])

        left = 0
        q, cash = initial
        for t, new_q, new_cash in self.changes + [(day.stop, q, cash)]:
            right = int(ts.searchsorted(t))
            if right > left and q:
                update(cash + q * 0.5 * (bid[left:right] + ask[left:right]))
            elif right > left:
                update([cash])
            if t != day.stop:
                mark = day.at(t, self.prior, inclusive=False)
                if mark:
                    update([new_cash + new_q * mark.mid])
            left = right
            q, cash = new_q, new_cash

    def replay(self, day: Day):
        if self.start is None:
            self.start = day.start
            self.last_exposure = day.start
            self.schedule(day.start, E.QUOTE_DECISION)
            self.schedule(day.start, E.SAMPLE)
        elif day.start != self.stop:
            raise ValueError("discontinuous replay")
        self.changes = []
        initial = self.ledger.inventory, self.ledger.cash
        baseline = self.daily[-1]["equity"] if self.daily else self.c.capital
        for event in day.funding:
            self.schedule(event.ts, E.FUNDING, event)
        tape = day.tape
        prints = zip(tape["ts"], tape["price"], tape["qty"], tape["sell"], strict=True)
        for print_index, (t, price, qty, sell) in enumerate(prints):
            t = int(t)
            while self.timers and (
                self.timers[0].ts < t or (self.timers[0].ts == t and self.timers[0].kind < 0)
            ):
                self.timer(heapq.heappop(self.timers), day)
            order = self.orders.sides.get(1 if sell else -1)
            if order:
                self.execute(t, order, float(price), float(qty), day, print_index)
        while self.timers and self.timers[0].ts < day.stop:
            self.timer(heapq.heappop(self.timers), day)
        self.markouts(day)
        self.drawdowns(day, initial)
        self.exposure(day.stop)
        self.prior = day.at(day.stop - 1, self.prior)
        self.venue_prior = day.at(day.stop - 1, self.venue_prior, venue=True)
        if self.prior is None:
            raise ValueError("no book in replay period")
        self.stop = day.stop
        self.sample(day.stop - 1, self.prior)
        equity = self.ledger.equity(self.prior.mid)
        self.daily.append(
            {
                "ts": day.start,
                "equity": equity,
                "pnl": equity - baseline,
                "closing_mid": self.prior.mid,
            }
        )

    def finish(self):
        for o in list(self.orders.sides.values()):
            self.close_order(self.stop, o, "sample_ended")
        for f in self.pending_marks:
            for h in HORIZONS:
                f.marks.setdefault(h, None)
        self.pending_marks.clear()

    def fill_rows(self):
        for f in self.fills:
            row = asdict(f)
            del row["marks"]
            yield dict(**row, capture=f.capture, **{f"markout_{h}s": f.marks[h] for h in HORIZONS})
