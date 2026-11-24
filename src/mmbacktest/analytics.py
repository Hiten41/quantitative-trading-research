"""Ledger reconciliation and execution diagnostics on observed fills only."""

from __future__ import annotations

import math

import numpy as np

from .data.pipeline import NS
from .engine import HORIZONS


def ratio(a, b):
    return a / b if b else None


class PerformanceAnalyzer:
    @staticmethod
    def summarize(e):
        c, ledger = e.c, e.ledger
        mid = e.prior.mid
        equity = ledger.equity(mid)
        pnl = equity - c.capital
        unreal = ledger.unrealized(mid)
        residual = pnl - (ledger.realized + unreal - ledger.fees + ledger.funding)
        if not math.isclose(residual, 0, abs_tol=max(1e-8, 1e-10 * c.capital)):
            raise ArithmeticError(f"ledger reconciliation failed: {residual}")
        duration = (e.stop - e.start) / NS
        rms = math.sqrt(e.sq_seconds / duration)
        fills = e.fills
        touched = {f.order for f in fills}
        volume = sum(f.qty for f in fills)
        count = e.counts
        diagnostics = {}
        for side in (0, 1, -1):
            selected = [f for f in fills if not side or f.side == side]
            by_horizon = {}
            for h in HORIZONS:
                valid = [f for f in selected if f.marks[h] is not None]
                mark = sum(f.marks[h] for f in valid)
                capture = sum(f.capture for f in valid)
                fee = sum(f.fee for f in valid)
                notional = sum(f.qty * f.price for f in valid)
                by_horizon[str(h)] = {
                    "valid": len(valid),
                    "missing": len(selected) - len(valid),
                    "directional": mark if valid else None,
                    "immediate": capture if valid else None,
                    "combined": capture + mark if valid else None,
                    "after_fee": capture + mark - fee if valid else None,
                    "directional_bps": ratio(mark * 10000, notional),
                    "after_fee_bps": ratio((capture + mark - fee) * 10000, notional),
                }
            diagnostics[{0: "all", 1: "buy", -1: "sell"}[side]] = by_horizon
        return {
            "strategy": e.name,
            "start_ns": e.start,
            "stop_ns": e.stop,
            "net_pnl": pnl,
            "gross_pnl": ledger.realized + unreal,
            "return_pct": 100 * pnl / c.capital,
            "net_equity": equity,
            "cash_equivalent": ledger.cash,
            "collateral_wallet": c.capital + ledger.realized - ledger.fees + ledger.funding,
            "terminal_inventory": ledger.inventory,
            "average_entry": ledger.entry,
            "realized_gross": ledger.realized,
            "unrealized": unreal,
            "fees": ledger.fees,
            "funding": ledger.funding,
            "reconciliation_error": residual,
            "gross_exposure": abs(ledger.inventory) * mid,
            "fill_count": len(fills),
            "orders_with_fill": len(touched),
            "order_fill_rate": ratio(len(touched), count["submitted"]),
            "quantity_fill_rate": ratio(volume, count["submitted_lots"] * c.step),
            "partial_orders": count["orders_partially_filled"],
            "executed_quantity": volume,
            "traded_notional": sum(f.qty * f.price for f in fills),
            "avg_abs_inventory": e.abs_seconds / duration,
            "max_abs_inventory": e.max_inventory,
            "abs_inventory_seconds": e.abs_seconds,
            "holding_seconds": e.holding_seconds,
            "utilization": e.abs_seconds / duration / c.inventory_limit,
            "rms_inventory": rms,
            "pnl_per_rms_inventory": ratio(pnl, rms),
            "max_drawdown": e.drawdown,
            "max_drawdown_pct": e.drawdown_pct * 100,
            "spread_capture": sum(f.capture for f in fills),
            "buy_fill_probability": ratio(
                len({f.order for f in fills if f.side == 1}), count["submitted_1"]
            ),
            "sell_fill_probability": ratio(
                len({f.order for f in fills if f.side == -1}), count["submitted_-1"]
            ),
            "counts": dict(count),
            "markouts": diagnostics,
        }


def paired_blocks(a, b, capital):
    """Resample aligned 6-hour paired increments; conditional descriptive interval."""

    def blocks(e):
        by_time = {row["ts"]: row["equity"] for row in e.samples}
        edges = [capital] + [by_time[t] for t in range(e.start + 21600 * NS, e.stop, 21600 * NS)]
        return np.diff(edges + [e.daily[-1]["equity"]])

    delta = blocks(b) - blocks(a)
    rng = np.random.default_rng(20230902)
    totals = rng.choice(delta, size=(5000, len(delta)), replace=True).sum(axis=1)
    return {
        "block_hours": 6,
        "blocks": len(delta),
        "observed_difference": float(delta.sum()),
        "percentile_95": np.percentile(totals, [2.5, 97.5]).tolist(),
    }
