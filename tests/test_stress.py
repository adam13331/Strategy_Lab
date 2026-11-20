from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from sqx.engine import Context
from sqx.stress import (
    CrossMarketStage, MonteCarloStage, ParameterStage, PriceCostStage,
    ShuffleStage, StressConfig, block_bootstrap_returns, garch_returns,
    jitter_spec, perturb_parameters, perturb_price_noise, price_shape,
    run_pipeline, shuffled_max_drawdowns, summarize, synthetic_context,
)


def make_ctx(n_m1: int = 2500, tf_seconds: int = 900, phase: float = 0.0,
             symbol: str = "USDJPY") -> Context:
    """A trending market so the SMA strategy has a positive expectancy."""
    n_tf = n_m1 // 15 + 20
    tf_ts = np.arange(n_tf, dtype=np.int64) * tf_seconds
    trend = np.linspace(100.0, 135.0, n_tf)
    wave = 0.10 * np.sin(np.arange(n_tf) / 5.0 + phase)
    close = trend + wave
    high = close + 0.12
    low = close - 0.12
    open_ = np.concatenate(([close[0]], close[:-1]))
    tf_df = pd.DataFrame({"open": open_, "high": high, "low": low,
                          "close": close, "volume": np.ones(n_tf)})
    m1_ts = np.arange(n_m1, dtype=np.int64) * 60
    base = np.repeat(close, 15)[:n_m1]
    m1_open = base + np.sin(np.arange(n_m1) / 9.0 + phase) * 0.01
    m1_close = base + np.sin(np.arange(n_m1) / 9.0 + phase + 0.4) * 0.01
    m1_high = np.maximum(m1_open, m1_close) + 0.02
    m1_low = np.minimum(m1_open, m1_close) - 0.02
    day_int = m1_ts // 86400
    day_vals, day_id = np.unique(day_int, return_inverse=True)
    m1_tf_idx = np.clip(np.searchsorted(tf_ts, m1_ts, side="right") - 1, 0,
                        n_tf - 1)
    prev = np.clip(np.searchsorted(tf_ts + tf_seconds, m1_ts, side="right") - 1,
                   0, n_tf - 1)
    split_idx = n_m1 // 2
    return Context(
        symbol=symbol, timeframe="M15", tf_seconds=tf_seconds, tf_df=tf_df,
        tf_ts=tf_ts, tf_close_ts=tf_ts + tf_seconds,
        tf_to_m1=np.clip(np.searchsorted(m1_ts, tf_ts + tf_seconds), 0,
                         n_m1 - 1),
        m1_prev_tf=prev.astype(np.int64), m1_ts=m1_ts, m1_open=m1_open,
        m1_high=m1_high, m1_low=m1_low, m1_close=m1_close,
        m1_day_id=day_id.astype(np.int64),
        day_ts=day_vals.astype(np.int64) * 86400,
        entry_ok=np.ones(n_m1, dtype=np.uint8),
        force_close=np.zeros(n_m1, dtype=np.uint8), split_idx=split_idx,
        split_day=int(day_id[split_idx]), start_ts=int(m1_ts[0]),
        end_ts=int(m1_ts[-1]), m1_tf_idx=m1_tf_idx.astype(np.int64),
    )


def make_spec(ctx: Context) -> dict:
    return {
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "direction": "both",
        "entry": {
            "long": [{
                "indicator": "sma", "output": "value", "operator": "gt",
                "params": {"period": 10},
                "operand": {"kind": "price", "value": None},
            }],
            "short": [{
                "indicator": "sma", "output": "value", "operator": "lt",
                "params": {"period": 10},
                "operand": {"kind": "price", "value": None},
            }],
        },
        "exit": {
            "conditions": [],
            "stop_loss": {"mode": "stop_loss", "type": "atr",
                          "atr_period": 14, "atr_mult": 2.0},
            "take_profit": {"mode": "none", "type": None},
            "trailing": {"mode": "none"},
            "opposite_signal": "ignore", "max_bars_in_trade": 30,
            "cooldown_bars": 0,
        },
        "risk": {
            "sizing": "fixed_lot", "fixed_lot": 0.1, "risk_percent": 1.0,
            "leverage": 30, "balance": 10000.0, "spread_pips": 0.3,
            "commission_per_lot": 0.0, "slippage_pips": 0.0,
        },
    }


def loose_config(**overrides) -> StressConfig:
    cfg = StressConfig(
        source_run_id="test",
        seed=7,
        parameter=ParameterStage(
            rounds=15, perturb_min_pct=0.1, perturb_max_pct=0.2,
            max_sharpe_drop_pct=100, max_profit_factor_drop_pct=100,
            min_profitable_pct=0),
        price_cost=PriceCostStage(
            rounds=1, noise_min_pct=0, noise_max_pct=0.001,
            max_sharpe_drop_pct=100, min_profitable_pct=0),
        shuffle=ShuffleStage(rounds=1000, p95_max_dd_multiple=10.0,
                             p99_max_dd_multiple=10.0),
        monte_carlo=MonteCarloStage(rounds=500, min_profitable_pct=0),
        cross_market=CrossMarketStage(alternate_symbols=["ALT"],
                                      min_avg_profit_factor=0.0),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_jitter_spec_clamps_and_only_touches_params():
    ctx = make_ctx(600)
    spec = make_spec(ctx)
    rng = np.random.default_rng(3)
    out = jitter_spec(spec, 20.0, rng)
    assert out is not spec
    assert spec["entry"]["long"][0]["params"]["period"] == 10
    assert 2 <= out["entry"]["long"][0]["params"]["period"] <= 200
    assert 2 <= out["exit"]["stop_loss"]["atr_period"] <= 100
    assert out["exit"]["stop_loss"]["atr_mult"] > 0


def test_summarize_percentiles():
    metrics = [{"trades": t, "sharpe": t / 10, "net_return": t} for t in range(1, 101)]
    s = summarize(metrics)
    assert s["trades"]["min"] == 1
    assert s["trades"]["max"] == 100
    assert s["trades"]["median"] == 50.5


def test_stage_round_ranges_are_enforced():
    with pytest.raises(ValidationError):
        ParameterStage(rounds=14)
    with pytest.raises(ValidationError):
        ParameterStage(rounds=51)
    with pytest.raises(ValidationError):
        PriceCostStage(rounds=0)
    with pytest.raises(ValidationError):
        PriceCostStage(rounds=51)
    with pytest.raises(ValidationError):
        ShuffleStage(rounds=999)
    with pytest.raises(ValidationError):
        ShuffleStage(rounds=5001)
    with pytest.raises(ValidationError):
        MonteCarloStage(rounds=499)
    with pytest.raises(ValidationError):
        MonteCarloStage(rounds=1001)
    with pytest.raises(ValidationError):
        ParameterStage(perturb_min_pct=20, perturb_max_pct=10)


def test_enabled_stages_follow_flags():
    cfg = StressConfig(source_run_id="x")
    assert len(cfg.enabled_stages()) == 5
    cfg.monte_carlo.enabled = False
    assert "monte_carlo" not in cfg.enabled_stages()


def test_price_noise_keeps_valid_bars_and_changes_prices():
    ctx = make_ctx(600)
    ctx2 = perturb_price_noise(ctx, np.random.default_rng(3), 0.0008)
    assert len(ctx2.m1_close) == len(ctx.m1_close)
    assert np.isfinite(ctx2.m1_close).all()
    assert not np.allclose(ctx2.m1_close, ctx.m1_close)
    assert (ctx2.m1_high >= np.maximum(ctx2.m1_open, ctx2.m1_close)).all()
    assert (ctx2.m1_low <= np.minimum(ctx2.m1_open, ctx2.m1_close)).all()
    assert (ctx2.tf_df["high"] >= ctx2.tf_df[["open", "close"]].max(axis=1)).all()
    assert (ctx2.tf_df["low"] <= ctx2.tf_df[["open", "close"]].min(axis=1)).all()


def test_parameter_perturbation_does_not_mutate_source():
    ctx = make_ctx(600)
    spec = make_spec(ctx)
    out = perturb_parameters(spec, 15.0, np.random.default_rng(4))
    assert spec["entry"]["long"][0]["params"]["period"] == 10
    assert spec["exit"]["max_bars_in_trade"] == 30
    assert out is not spec
    assert out["entry"]["long"][0]["params"]["period"] != 10 or \
        out["exit"]["max_bars_in_trade"] != 30


def test_synthetic_context_preserves_structure():
    ctx = make_ctx(900)
    shape = price_shape(ctx)
    rng = np.random.default_rng(9)
    returns = block_bootstrap_returns(shape, rng, 60)
    assert len(returns) == len(ctx.m1_close) - 1
    syn = synthetic_context(ctx, shape, returns)
    assert syn.symbol == ctx.symbol
    assert np.array_equal(syn.m1_ts, ctx.m1_ts)
    assert len(syn.tf_df) == len(ctx.tf_df)
    assert np.isfinite(syn.m1_close).all()
    assert (syn.m1_close > 0).all()
    assert not np.allclose(syn.m1_close, ctx.m1_close)
    assert (syn.tf_df["high"] >= syn.tf_df[["open", "close"]].max(axis=1)).all()
    assert (syn.tf_df["low"] <= syn.tf_df[["open", "close"]].min(axis=1)).all()


def test_garch_paths_are_finite_and_same_length():
    ctx = make_ctx(700)
    shape = price_shape(ctx)
    r = garch_returns(shape, np.random.default_rng(2))
    assert len(r) == len(ctx.m1_close) - 1
    assert np.isfinite(r).all()


def test_shuffled_drawdowns_shape_and_zero_when_all_wins():
    pnls = np.ones(100) * 10.0
    dd = shuffled_max_drawdowns(pnls, 10000.0, 1000,
                                np.random.default_rng(1))
    assert dd.shape == (1000,)
    assert np.allclose(dd, 0.0)
    mixed = np.concatenate([np.ones(50) * 20.0, -np.ones(50) * 20.0])
    dd2 = shuffled_max_drawdowns(mixed, 10000.0, 1000,
                                 np.random.default_rng(1))
    assert dd2.max() > 0


def test_pipeline_runs_all_stages_and_survives_loose_thresholds():
    ctx = make_ctx(2000, phase=0.0)
    alt = make_ctx(2000, phase=1.7, symbol="EURUSD")
    spec = make_spec(ctx)
    cfg = loose_config()
    with ThreadPoolExecutor(max_workers=2) as pool:
        res = run_pipeline(spec, ctx, cfg, alternates=[("EURUSD", alt)],
                           pool=pool)
    assert res["baseline"]["trades"] > 0
    assert res["survival"] is True, res
    assert [res["stages"][k]["status"] for k in res["stages"]] == [
        "passed", "passed", "passed", "passed", "passed"]
    assert res["stages"]["parameter_sensitivity"]["tests_run"] == 15
    assert res["stages"]["price_cost_sensitivity"]["tests_run"] == 2
    assert res["stages"]["trade_shuffling"]["tests_run"] == 1000
    assert res["stages"]["monte_carlo"]["tests_run"] == 500
    assert res["stages"]["cross_market_validation"]["tests_run"] == 1
    cost_extra = res["stages"]["price_cost_sensitivity"]["extra"]["cost_stress"]
    assert cost_extra["baseline_fallback"] is True
    assert cost_extra["baseline_slippage_pips"] > 0
    assert cost_extra["baseline_commission_per_lot"] > 0


def test_pipeline_stops_after_first_failed_stage():
    ctx = make_ctx(800)
    spec = make_spec(ctx)
    cfg = loose_config()
    failed = {
        "stage": "parameter_sensitivity", "label": "Parameter sensitivity",
        "status": "failed", "passed": False, "tests_requested": 15,
        "tests_run": 15, "checks": [], "checks_passed": 0, "checks_total": 0,
        "metrics": {}, "extra": {},
    }
    with patch("sqx.stress.run_parameter_stage", return_value=failed):
        res = run_pipeline(spec, ctx, cfg, alternates=[])
    assert res["survival"] is False
    assert res["stages"]["parameter_sensitivity"]["status"] == "failed"
    for key in ("price_cost_sensitivity", "trade_shuffling", "monte_carlo",
                "cross_market_validation"):
        assert res["stages"][key]["status"] == "not_run"


def test_pipeline_marks_later_stages_when_disabled():
    ctx = make_ctx(600)
    spec = make_spec(ctx)
    cfg = loose_config()
    cfg.monte_carlo.enabled = False
    cfg.cross_market.enabled = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        res = run_pipeline(spec, ctx, cfg, alternates=[], pool=pool)
    assert res["stages"]["monte_carlo"]["status"] == "skipped"
    assert res["stages"]["cross_market_validation"]["status"] == "skipped"
    assert res["survival"] is True
