from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sqx import engine
from sqx.strategy import default_config


def _synthetic_market(days: int = 90) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(5)
    start = pd.Timestamp("2020-01-01 00:00:00")
    n = days * 1440
    ts = pd.date_range(start, periods=n, freq="min")
    # weekday-only behaviour so the session filter has something to do
    keep = ts.day_of_week < 5
    ts = ts[keep]
    n = len(ts)
    wave = np.sin(np.arange(n) / 900.0) * 0.6
    noise = rng.normal(0, 0.02, n)
    close = 150.0 + np.cumsum(noise) * 0.15 + wave
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.random(n) * 0.03
    low = np.minimum(open_, close) - rng.random(n) * 0.03
    m1 = pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low,
                       "close": close, "volume": rng.integers(20, 200, n)})
    h4 = (
        m1.set_index("ts")
        .resample("4h", label="left", closed="left", origin="start_day")
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
             close=("close", "last"), volume=("volume", "sum"))
        .dropna()
        .reset_index()
    )
    return m1, h4


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("market")
    m1, h4 = _synthetic_market()
    m1.to_parquet(tmp / "m1.parquet", index=False)
    h4.to_parquet(tmp / "h4.parquet", index=False)

    old_m1, old_tf = engine.m1_path, engine.tf_path
    engine.m1_path = lambda symbol: tmp / "m1.parquet"
    engine.tf_path = lambda symbol, tf: tmp / "h4.parquet"
    cfg = default_config(["TEST"])
    try:
        yield engine.load_context("TEST", "H4", 40, cfg.session.model_dump())
    finally:
        engine.m1_path, engine.tf_path = old_m1, old_tf


def _spec() -> dict:
    cfg = default_config(["TEST"])
    above = {"indicator": "sma", "params": {"period": 20}, "output": "value",
             "operator": "gt", "operand": {"kind": "price"}}
    below = {"indicator": "sma", "params": {"period": 20}, "output": "value",
             "operator": "lt", "operand": {"kind": "price"}}
    return {
        "schema": 1, "symbol": "TEST", "timeframe": "H4", "direction": "both",
        "entry": {"long": [above], "short": [below]},
        "exit": {
            "conditions": [],
            "opposite_signal": "reverse",
            "stop_loss": {"mode": "stop_loss", "type": "fixed", "pips": 20},
            "take_profit": {"mode": "take_profit", "type": "fixed", "pips": 20},
            "trailing": {"mode": "none"},
            "max_bars_in_trade": 12,
            "cooldown_bars": 0,
            "close_at_session_end": True,
        },
        "session": cfg.session.model_dump(),
        "risk": cfg.risk.model_dump(),
        "meta": {},
    }


def test_context_shapes(context):
    assert len(context.m1_ts) > 10_000
    assert len(context.tf_df) > 100
    assert len(context.day_ts) == len(np.unique(context.m1_day_id))
    assert 0 < context.split_idx < len(context.m1_ts)
    assert context.split_day > 0
    assert context.entry_ok[context.split_idx] == 0
    # Sunday is not tradable under the default Mon-Fri session
    sunday = (context.m1_ts // 86400 + 3) % 7 == 6
    if sunday.any():
        assert context.entry_ok[sunday].sum() == 0


def test_backtest_produces_consistent_results(context):
    res = engine.run_backtest(_spec(), context, engine.IndicatorCache())
    assert set(res) >= {"metrics_is", "metrics_oos", "daily_eq", "day_ts",
                        "trades", "final_equity", "split_day"}
    assert len(res["daily_eq"]) == len(context.day_ts)
    assert len(res["day_ts"]) == len(res["daily_eq"])
    assert 0 < res["split_day"] < len(res["daily_eq"])

    is_m, oos_m = res["metrics_is"], res["metrics_oos"]
    assert is_m["trades"] + oos_m["trades"] == len(res["trades"])
    assert is_m["trades"] > 0, "expected the synthetic trend rule to trade"
    assert is_m["max_dd"] >= 0
    assert -100.0 <= oos_m["net_return"] <= 10_000.0

    for t in res["trades"][:50]:
        assert t["side"] in ("long", "short")
        assert t["entry_ts"] <= t["exit_ts"]
        assert t["reason"] in range(1, 10)


def test_passes_gates():
    good = {"trades": 50, "sharpe": 1.0, "return_dd": 1.2, "stagnation_days": 30,
            "max_dd": 10.0, "profit_factor": 1.5, "win_rate": 55.0}
    gates = {"min_trades": 20, "min_sharpe": 0.5, "max_stagnation_days": 365,
             "min_return_dd": 0.5, "max_drawdown_pct": 50.0,
             "min_profit_factor": 1.0, "min_win_rate": 40.0}
    assert engine.passes(good, gates) == (True, "")
    for field, value, reason in [
        ("trades", 5, "min_trades"), ("sharpe", 0.1, "min_sharpe"),
        ("return_dd", 0.1, "return_dd"), ("stagnation_days", 400, "stagnation"),
        ("max_dd", 60.0, "max_dd"), ("profit_factor", 0.9, "profit_factor"),
        ("win_rate", 10.0, "win_rate"),
    ]:
        bad = dict(good, **{field: value})
        ok, why = engine.passes(bad, gates)
        assert not ok and why == reason


def test_segment_metrics_direction():
    days = np.arange(10) * 86400
    up = np.array([99.0, 100, 101, 102, 103, 104, 105, 106, 107, 110])
    dn = np.array([101.0, 100, 99, 98, 97, 96, 95, 94, 93, 90])
    m_up = engine.segment_metrics(up, 100.0, days, np.array([10.0]), 1)
    m_dn = engine.segment_metrics(dn, 100.0, days, np.array([-10.0]), 1)
    assert m_up["sharpe"] > 0 > m_dn["sharpe"]
    assert m_up["net_return"] > 0 > m_dn["net_return"]


def _with_orders(spec: dict, orders: list[dict], expiry: int = 10) -> dict:
    spec = dict(spec)
    spec["execution"] = {"orders": orders, "expiry_bars": expiry, "oco": True}
    return spec


def test_limit_and_stop_orders_fill(context):
    spec = _with_orders(_spec(), [{
        "type": "limit", "distance_type": "fixed", "pips": 0.2}])
    limit = engine.run_backtest(spec, context, engine.IndicatorCache())
    assert limit["metrics_all"]["trades"] > 0

    spec = _with_orders(_spec(), [{
        "type": "stop", "distance_type": "fixed", "pips": 0.2}])
    stop = engine.run_backtest(spec, context, engine.IndicatorCache())
    assert stop["metrics_all"]["trades"] > 0


def test_multiple_pending_orders_are_oco(context):
    spec = _with_orders(_spec(), [
        {"type": "limit", "distance_type": "fixed", "pips": 0.2},
        {"type": "stop", "distance_type": "fixed", "pips": 0.2},
    ])
    res = engine.run_backtest(spec, context, engine.IndicatorCache())
    assert res["metrics_all"]["trades"] > 0
    for t in res["trades"][:50]:
        assert t["entry_ts"] <= t["exit_ts"]


def test_pending_order_expires_without_filling(context):
    spec = _with_orders(_spec(), [{
        "type": "limit", "distance_type": "fixed", "pips": 10_000}], expiry=1)
    res = engine.run_backtest(spec, context, engine.IndicatorCache())
    assert res["metrics_all"]["trades"] == 0


def test_atr_limit_order_fills(context):
    spec = _with_orders(_spec(), [{
        "type": "limit", "distance_type": "atr",
        "atr_period": 14, "atr_mult": 0.05}])
    res = engine.run_backtest(spec, context, engine.IndicatorCache())
    assert res["metrics_all"]["trades"] > 0
