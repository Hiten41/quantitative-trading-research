"""Synthetic clocks, liquidity crises and funding reversals for the independent study."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from fundingcarry.sources import MINUTE, save_funding_snapshot
from fundingcarry.study_data import acquire, price_event, select, stats
from fundingcarry.study_engine import price_bar, settlement, signal, simulate


@pytest.fixture
def cfg():
    return json.loads(
        (Path(__file__).resolve().parents[1] / "configs/carry_study.json").read_text()
    )


def market(n=250):
    bar = [100, 100, 100, 100, 1000, 100000, 500, 50000]
    return {k: np.tile(bar, (n + 1, 1)).astype(float) for k in ("spot1m", "perp1m", "mark")}


def ev(i, rate, mark=100):
    return {
        "time": (i - 1) * MINUTE + 2,
        "rate": rate,
        "mark": mark,
        "low": mark - 5,
        "high": mark + 5,
        "source": "prior completed mark close",
        "gap_ms": 3,
    }


def run(c, pol="A", data=None, events=None, prior=None):
    return simulate(c, market() if data is None else data, events or {}, prior or [], 0, pol, True)


@pytest.mark.parametrize(("rate", "expected"), [(0.001, 0.05), (-0.001, -0.05), (0, 0)])
def test_signed_settlement(rate, expected):
    e = ev(1, rate)
    assert settlement(e, 0.5, "point", 1) == pytest.approx(expected)
    assert settlement(e, 0, "point", 1) == 0


@pytest.mark.parametrize("rate", [-0.001, 0.001])
def test_conditional_envelope_order(rate):
    e = ev(1, rate)
    assert (
        settlement(e, 1, "lower", 1) <= settlement(e, 1, "point", 1) <= settlement(e, 1, "upper", 1)
    )
    assert settlement(e, 1, "point", 0.25) == pytest.approx(rate * 100 * (0.25 if rate > 0 else 1))


def test_clock_ties_open_positions_only(cfg):
    _, d = run(cfg, events={i: ev(i, 0.001) for i in (1, 2, 3, 235, 236, 237)})
    assert [v["short"] for v in d["settlements"]] == [0, 0, 0.5, 0.5, 0.5, 0]
    assert all(e["gap_ms"] == 3 for e in d["settlements"])


def test_signal_uses_only_completed_publications(cfg):
    rules = cfg["signals"] | {"lookback": 2}
    history = [(0, 0.1), (MINUTE, 0.2), (2 * MINUTE, -0.9)]
    assert signal(history, 2 * MINUTE, rules) == pytest.approx((0.15, MINUTE, 0))
    assert signal(history, MINUTE, rules) is None


def test_replacing_future_funding_preserves_earlier_decisions(cfg):
    prior = [(-i * MINUTE, 0.002) for i in range(1, 11)]
    events = {180: ev(180, 0.001)}
    _, a = run(cfg, "B", events=events, prior=prior)
    _, b = run(cfg, "B", events={180: ev(180, -0.002)}, prior=prior)
    assert [f for f in a["fills"] if f["time"] < 178 * MINUTE] == [
        f for f in b["fills"] if f["time"] < 178 * MINUTE
    ]
    assert a["decisions"][0]["last_signal"] < a["decisions"][0]["time"]
    assert a["fills"][:2] == b["fills"][:2]


def test_costs_once_and_terminal_cash(cfg):
    m, d = run(cfg)
    assert len(d["fills"]) == 4
    assert all(f["fee"] > 0 and f["spread"] > 0 and f["slippage"] > 0 for f in d["fills"])
    assert m["net"] == pytest.approx(-m["total_cost"])
    assert m["net"] == pytest.approx(m["spot_cash"] + m["futures_wallet"] - cfg["capital"])
    assert [
        d["balances"][-1][k]
        for k in ("spot_qty", "futures_qty", "spot_unrealized", "futures_unrealized")
    ] == [0, 0, 0, 0]
    assert m["fees_perp"] + m["fees_spot"] == pytest.approx(sum(f["fee"] for f in d["fills"]))
    for r in d["balances"]:
        assert r["equity"] == pytest.approx(
            r["spot_cash"]
            + r["spot_qty"] * r["spot_mark"]
            + r["futures_wallet"]
            + r["futures_unrealized"]
            + r["transit"]
        )


def test_distinct_venues_and_mark_liquidation(cfg):
    d = market()
    d["mark"][12, 1] = 140
    m, logs = run(cfg | {"leverage": 5}, data=d)
    assert m["spot_cash"] > 9000 and m["liquidations"] == 1
    assert (
        next(r for r in logs["risk_events"] if r["event"] == "liquidation")["time"] == 11 * MINUTE
    )
    assert any(r["forced"] and r["price"] >= 140 for r in logs["fills"])
    assert m["liquidation_fee"] > 0 and m["forced_impact"] > 0
    assert m["net"] < 0 and logs["balances"][-1]["spot_qty"] == 0


def test_transfer_cannot_save_short_before_arrival(cfg):
    data = market()
    data["mark"][8, 1] = 119
    data["mark"][12, 1] = 135
    m, logs = run(cfg | {"leverage": 5, "transfer_delay": 20}, data=data)
    fast, _ = run(cfg | {"leverage": 5, "transfer_delay": 1}, data=data)
    assert m["transfers"] == 1 and m["liquidations"] == 1 and fast["liquidations"] == 0
    events = {e["event"]: e for e in logs["risk_events"]}
    assert (
        events["transfer available"]["time"] - events["transfer requested"]["time"] == 20 * MINUTE
    )
    assert events["liquidation"]["transit"] == 100


def test_no_transfer_when_cash_unavailable(cfg):
    d = market()
    d["mark"][8, 1] = 119
    m, _ = run(cfg | {"leverage": 5, "transfer_amount": 20000}, data=d)
    assert m["transfers"] == 0


def test_volume_delay_and_legging(cfg):
    d = market()
    d["perp1m"][2:6, 4:] = 0
    m, logs = run(cfg, data=d)
    assert m["rejected_minutes"] == 4 and m["unhedged_minutes"] == 6
    assert logs["fills"][1]["time"] - logs["fills"][0]["time"] == 5 * MINUTE


def test_terminal_liquidity_failure_explicit(cfg):
    d = market()
    d["spot1m"][230:, 4:] = 0
    with pytest.raises(ValueError, match="terminal"):
        run(cfg, data=d)


@pytest.mark.parametrize("policy", ["B", "C"])
def test_negative_regime_abstains(policy, cfg):
    m, _ = run(cfg, policy, prior=[(-i * MINUTE, -0.005) for i in range(1, 21)])
    assert m["net"] == 0 and m["executed_notional"] == 0


def test_deterioration_exit(cfg):
    prior = [(-i * MINUTE, 0.005) for i in range(1, 11)]
    events = {i: ev(i, -0.002) for i in (10, 20, 30)}
    _, d = run(cfg, "C", events=events, prior=prior)
    assert any(r["action"] == "risk exit" for r in d["decisions"])


def test_reproducibility_and_configuration_immutable(cfg):
    frozen = copy.deepcopy(cfg)
    a = run(cfg)
    b = run(cfg)
    assert a == b and cfg == frozen


def test_discovery_ignores_pnl(cfg):
    rates = [(k * 8 * 60 * MINUTE, -0.001, 8) for k in range(5)]
    r = stats("2020-01", rates)
    assert r["negative_pct"] == 100 and r["longest_negative"] == 5 and not r["complete_8h"]
    a = dict(r, month="2022-09", complete_8h=True)
    b = dict(
        r, month="2022-06", complete_8h=True, negative_pct=50, positive_pct=50, longest_negative=4
    )
    assert select([a, b], cfg) == select([b | {"net": 1e99}, a | {"net": -1e99}], cfg)


def test_absent_archive_fails_without_generated_prices(tmp_path):
    with pytest.raises(ValueError, match="missing verified"):
        acquire(tmp_path, "mark", "2022-09", {})


def test_invalid_precision(cfg):
    with pytest.raises(ValueError, match="invalid order size"):
        run(cfg | {"qty": 0.5005})


def test_sell_uses_residual_aggressor_volume(cfg):
    data = market()["spot1m"][0]
    data[7] = 51000
    assert price_bar(data, 1, cfg)[1] > 102
    assert price_bar(data, -1, cfg)[1] < 98


def test_prior_point_independent_of_current_minute_and_later_marks():
    bars = market()["mark"]
    bars[2, :4] = [110, 150, 90, 120]
    row = {"fundingTime": MINUTE + 2, "fundingRate": "-.001", "markPrice": ""}
    result = price_event(row, bars, 2)
    assert (result["mark"], result["gap_ms"], result["low"], result["high"]) == (100, 3, 90, 150)
    assert result["source"] == "prior completed mark close"
    bars[2:, 1:4] = [180, 80, 130]
    assert result["mark"] == price_event(row, bars, 2)["mark"]
    observed = price_event(row | {"markPrice": "112"}, bars, 2)
    assert observed["source"] == "REST settlement" and observed["gap_ms"] == 0
    assert observed["low"] == observed["mark"] == observed["high"] == 112


def test_fixed_policy_skips_unaffordable_entry_even_if_later_price_falls(cfg):
    data = market()
    data["spot1m"][0, :4] = 1000
    m, logs = run(cfg | {"capital": 140}, data=data)
    assert m["executed_notional"] == 0 and not logs["fills"]


def test_policy_specific_config_keeps_other_policies_independent(cfg):
    prior = [(-i * MINUTE, 0.005) for i in range(1, 11)]
    custom = cfg | {"policies": {"C": {"signals": {"entry_bps": 10000}}}}
    assert run(custom, "B", prior=prior)[0]["executed_notional"] > 0
    assert run(custom, "C", prior=prior)[0]["executed_notional"] == 0


def test_funding_snapshot_preserves_locked_bytes(tmp_path):
    snapshot = tmp_path / "funding.json"
    save_funding_snapshot(snapshot, [{"rate": "0.0001"}])
    assert snapshot.read_bytes() == b'[\r\n  {\r\n    "rate": "0.0001"\r\n  }\r\n]'

    with pytest.raises(ValueError):
        save_funding_snapshot(snapshot, [{"rate": float("nan")}])
    assert json.loads(snapshot.read_bytes()) == [{"rate": "0.0001"}]
