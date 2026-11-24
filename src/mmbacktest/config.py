from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class Config:
    symbol: str = "ETHUSDT"
    start: str = "2023-09-02"
    evaluation_start: str = "2023-09-04"
    end: str = "2023-09-06"
    capital: float = 10000
    quantity: float = 0.1
    inventory_limit: float = 0.5
    spread_bps: float = 3
    maker_fee_bps: float = 1
    rebate_assumption: str = ""
    gamma: float = 0.8
    horizon_s: float = 60
    vol_window_s: float = 180
    vol_width: float = 0.35
    refresh_ms: int = 5000
    new_latency_ms: int = 50
    cancel_latency_ms: int = 80
    lifetime_ms: int = 15000
    stale_ms: int = 2000
    replace_ticks: int = 2
    participation: float = 0.1
    fill_model: str = "through"
    tick: float = 0.01
    step: float = 0.001
    min_qty: float = 0.001
    max_qty: float = 10000
    min_price: float = 39.86
    signal_half_life_s: float = 1800.0
    entry_width_bps: float = 4.0
    trend_band_bps: float = 10.0
    max_price: float = 306177
    min_notional: float = 20

    def __post_init__(self):
        for f in fields(self):
            if f.name in {
                "symbol",
                "start",
                "evaluation_start",
                "end",
                "fill_model",
                "rebate_assumption",
            }:
                continue
            v = getattr(self, f.name)
            if (
                isinstance(v, bool)
                or not isinstance(v, (float, int))
                or not math.isfinite(v)
                or (v < 0 and f.name != "maker_fee_bps")
            ):
                raise ValueError(f"invalid {f.name}")
            if (f.name.endswith("_ms") or f.name == "replace_ticks") and not isinstance(v, int):
                raise ValueError(f"{f.name} requires an integer")
        if (
            min(
                self.tick,
                self.step,
                self.capital,
                self.quantity,
                self.inventory_limit,
                self.refresh_ms,
                self.lifetime_ms,
                self.stale_ms,
                self.vol_window_s,
                self.signal_half_life_s,
                self.entry_width_bps,
                self.trend_band_bps,
                self.horizon_s,
                self.max_qty,
                self.max_price,
            )
            <= 0
        ):
            raise ValueError("nonpositive scale, capital or timing")
        if self.maker_fee_bps < 0 and not self.rebate_assumption.strip():
            raise ValueError("negative maker fee requires a documented rebate_assumption")
        if self.max_price < self.min_price or self.max_qty < self.min_qty:
            raise ValueError("inverted exchange filter bounds")
        if self.participation > 1 or self.fill_model not in ("through", "queue", "none"):
            raise ValueError("invalid execution model")
        if (
            not date.fromisoformat(self.start)
            < date.fromisoformat(self.evaluation_start)
            <= date.fromisoformat(self.end)
        ):
            raise ValueError("require chronological calibration and evaluation dates")

    @classmethod
    def load(cls, path: Path):
        return cls(**yaml.safe_load(path.read_text()))
