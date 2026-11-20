"""Sequential five-stage strategy robustness pipeline.

This is the single robustness-testing pipeline: it runs a fixed, ordered
pipeline over an *accepted* strategy.  A strategy has to clear every enabled
stage before the next one is attempted:

1. parameter sensitivity (perturb the optimised parameters by +/-10-20%),
2. price noise and cost stress,
3. trade-sequence shuffling (drawdown distribution),
4. block-bootstrap / GARCH Monte Carlo price paths,
5. cross-market validation on alternate instruments.

The stage thresholds implement the classic robustness rules of thumb:

* median Sharpe / profit-factor drop bounded by a configurable percentage,
* a minimum share of perturbed variants that stay profitable,
* 95th/99th percentile shuffled max-drawdown bounded as a multiple of the
  historical max-drawdown,
* a minimum share of synthetic paths that stay profitable,
* an average cross-market profit factor above one.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

import numpy as np
import pandas as pd
from numba import njit
from pydantic import BaseModel, Field, field_validator, model_validator

from .engine import Context, IndicatorCache, run_backtest
from .indicators import INDICATORS

STAGES = [
    "parameter_sensitivity",
    "price_cost_sensitivity",
    "trade_shuffling",
    "monte_carlo",
    "cross_market_validation",
]

STAGE_LABELS = {
    "parameter_sensitivity": "Parameter sensitivity",
    "price_cost_sensitivity": "Price & cost sensitivity",
    "trade_shuffling": "Trade shuffling",
    "monte_carlo": "Monte Carlo price permutation",
    "cross_market_validation": "Cross-market validation",
}


# --------------------------------------------------------------------- config
class ParameterStage(BaseModel):
    enabled: bool = True
    rounds: int = Field(30, ge=15, le=50)
    perturb_min_pct: float = Field(10.0, ge=0, le=100)
    perturb_max_pct: float = Field(20.0, ge=0, le=100)
    max_sharpe_drop_pct: float = Field(25.0, ge=0, le=100)
    max_profit_factor_drop_pct: float = Field(30.0, ge=0, le=100)
    min_profitable_pct: float = Field(80.0, ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "ParameterStage":
        if self.perturb_max_pct < self.perturb_min_pct:
            raise ValueError("perturb_max_pct must be >= perturb_min_pct")
        return self


class PriceCostStage(BaseModel):
    enabled: bool = True
    rounds: int = Field(20, ge=1, le=50)
    noise_min_pct: float = Field(0.05, ge=0, le=5)
    noise_max_pct: float = Field(0.10, ge=0, le=5)
    max_sharpe_drop_pct: float = Field(25.0, ge=0, le=100)
    cost_multiplier_min: float = Field(2.0, ge=1.0, le=10.0)
    cost_multiplier_max: float = Field(3.0, ge=1.0, le=10.0)
    min_profitable_pct: float = Field(100.0, ge=0, le=100)
    # Optional explicit baselines. When unset, the strategy's own costs are
    # multiplied; a strategy with zero costs can otherwise make this sub-test
    # trivial.
    baseline_slippage_pips: float | None = Field(None, ge=0)
    baseline_commission_per_lot: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "PriceCostStage":
        if self.noise_max_pct < self.noise_min_pct:
            raise ValueError("noise_max_pct must be >= noise_min_pct")
        if self.cost_multiplier_max < self.cost_multiplier_min:
            raise ValueError("cost_multiplier_max must be >= cost_multiplier_min")
        return self


class ShuffleStage(BaseModel):
    enabled: bool = True
    rounds: int = Field(2000, ge=1000, le=5000)
    p95_max_dd_multiple: float = Field(1.5, ge=1.0, le=10.0)
    p99_max_dd_multiple: float = Field(2.0, ge=1.0, le=10.0)


class MonteCarloStage(BaseModel):
    enabled: bool = True
    rounds: int = Field(500, ge=500, le=1000)
    method: Literal["block_bootstrap", "garch"] = "block_bootstrap"
    block_size: int = Field(240, ge=10, le=5000)
    min_profitable_pct: float = Field(75.0, ge=0, le=100)


class CrossMarketStage(BaseModel):
    enabled: bool = True
    alternate_symbols: list[str] | None = None
    min_avg_profit_factor: float = Field(1.0, ge=0.0)

    @field_validator("alternate_symbols")
    @classmethod
    def _ids(cls, v: list[str] | None) -> list[str] | None:
        return sorted(set(v)) if v else None


class StressConfig(BaseModel):
    source_run_id: str
    bank_name: str | None = None
    seed: int = 2026
    strategy_ids: list[str] | None = None
    parameter: ParameterStage = Field(default_factory=ParameterStage)
    price_cost: PriceCostStage = Field(default_factory=PriceCostStage)
    shuffle: ShuffleStage = Field(default_factory=ShuffleStage)
    monte_carlo: MonteCarloStage = Field(default_factory=MonteCarloStage)
    cross_market: CrossMarketStage = Field(default_factory=CrossMarketStage)

    @field_validator("strategy_ids")
    @classmethod
    def _ids(cls, v: list[str] | None) -> list[str] | None:
        return sorted(set(v)) if v else None

    def stage_config(self, key: str) -> Any:
        return {
            "parameter_sensitivity": self.parameter,
            "price_cost_sensitivity": self.price_cost,
            "trade_shuffling": self.shuffle,
            "monte_carlo": self.monte_carlo,
            "cross_market_validation": self.cross_market,
        }[key]

    def enabled_stages(self) -> list[str]:
        return [k for k in STAGES if self.stage_config(k).enabled]


def default_stress_config(source_run_id: str = "") -> StressConfig:
    return StressConfig(source_run_id=source_run_id)


# ------------------------------------------------------------------- utilities
def scenario_seed(base: int, stage: str, index: int) -> int:
    return (int(base) * 1_000_003 + STAGES.index(stage) * 100_003
            + int(index))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p05": 0.0, "median": 0.0, "p95": 0.0,
                "min": 0.0, "max": 0.0, "mean": 0.0}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "p05": round(float(np.percentile(arr, 5)), 4),
        "median": round(float(np.percentile(arr, 50)), 4),
        "p95": round(float(np.percentile(arr, 95)), 4),
        "min": round(float(arr.min()), 4),
        "max": round(float(arr.max()), 4),
        "mean": round(float(arr.mean()), 4),
    }


def _drop_pct(baseline: float, stressed: float) -> float:
    """Percentage fall from baseline, never negative."""
    if baseline > 0:
        return max(0.0, (baseline - stressed) / baseline * 100.0)
    return 0.0 if stressed >= baseline else 100.0


def _profitable_share(metrics: list[dict[str, Any]]) -> float:
    if not metrics:
        return 0.0
    return 100.0 * sum(1 for m in metrics if m.get("net_return", 0.0) > 0) / len(metrics)


def _check(name: str, value: float, threshold: float,
           op: Literal["<=", ">=", ">"], unit: str = "") -> dict[str, Any]:
    if op == "<=":
        ok = value <= threshold
    elif op == ">=":
        ok = value >= threshold
    else:
        ok = value > threshold
    return {"name": name, "value": round(float(value), 4),
            "threshold": round(float(threshold), 4), "op": op,
            "unit": unit, "passed": bool(ok)}


def _run_batch(fn: Callable[[int], dict[str, Any]], indices: list[int],
               pool, batch: int, stage_key: str,
               progress: Callable[[str, int], None] | None,
               stop_event) -> list[dict[str, Any]]:
    """Evaluate independent scenarios, keeping peak memory bounded."""
    out: list[dict[str, Any]] = []
    for start in range(0, len(indices), batch):
        if stop_event is not None and stop_event.is_set():
            break
        chunk = indices[start:start + batch]
        if pool is None:
            results = [fn(k) for k in chunk]
        else:
            futures = [pool.submit(fn, k) for k in chunk]
            results = [f.result() for f in futures]
        out.extend(results)
        if progress:
            progress(stage_key, len(results))
    return out


def _stage_record(key: str, passed: bool, checks: list[dict[str, Any]],
                  tests_requested: int, metrics: list[dict[str, Any]],
                  extra: dict[str, Any] | None = None,
                  status: str | None = None,
                  tests_run_override: int | None = None) -> dict[str, Any]:
    checks_passed = sum(1 for c in checks if c["passed"])
    return {
        "stage": key,
        "label": STAGE_LABELS.get(key, key),
        "status": status or ("passed" if passed else "failed"),
        "passed": bool(passed),
        "tests_requested": int(tests_requested),
        "tests_run": int(tests_run_override if tests_run_override is not None
                         else len(metrics)),
        "checks": checks,
        "checks_passed": checks_passed,
        "checks_total": len(checks),
        "pass_rate": round(checks_passed / len(checks), 4) if checks else 0.0,
        "metrics": summarize(metrics) if metrics else {},
        "extra": extra or {},
    }


METRIC_KEYS = ["trades", "net_return", "max_dd", "return_dd", "sharpe",
               "stagnation_days", "profit_factor", "win_rate", "avg_trade"]


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def summarize(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Percentile summary of a list of scenario metric dicts."""
    out: dict[str, Any] = {}
    for key in METRIC_KEYS:
        vals = [float(m.get(key, 0.0)) for m in metrics]
        if not vals:
            out[key] = {"p05": 0.0, "median": 0.0, "p95": 0.0,
                        "min": 0.0, "max": 0.0, "mean": 0.0}
            continue
        out[key] = {
            "p05": round(_pct(vals, 5), 4),
            "median": round(_pct(vals, 50), 4),
            "p95": round(_pct(vals, 95), 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "mean": round(float(np.mean(vals)), 4),
        }
    return out


def _clamp_param(ind_id: str, name: str, value: float) -> float | int:
    ind = INDICATORS.get(ind_id)
    if ind:
        for p in ind.params:
            if p.name == name:
                value = min(max(value, p.min), p.max)
                return int(round(value)) if p.integer else float(value)
    return int(round(value)) if float(value).is_integer() else float(value)


def _jitter_condition(c: dict[str, Any], pct: float,
                      rng: np.random.Generator) -> None:
    params = c.get("params") or {}
    ind_id = c.get("indicator", "")
    ind = INDICATORS.get(ind_id)
    fixed = {p.name for p in ind.params if p.choices} if ind else set()
    for name, value in list(params.items()):
        if name in fixed or not isinstance(value, (int, float)):
            continue
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        candidate = float(value) * factor
        if candidate <= 0:
            candidate = max(0.01, abs(float(value)) * 0.01)
        params[name] = _clamp_param(ind_id, name, candidate)


def jitter_spec(spec: dict[str, Any], pct: float,
                rng: np.random.Generator) -> dict[str, Any]:
    """Randomize indicator and ATR parameters by +/- pct percent."""
    out = copy.deepcopy(spec)
    for side in ("long", "short"):
        for c in out.get("entry", {}).get(side, []):
            _jitter_condition(c, pct, rng)
    for c in out.get("exit", {}).get("conditions", []):
        _jitter_condition(c, pct, rng)
    ex = out.get("exit", {})
    for key in ("stop_loss", "take_profit", "trailing"):
        block = ex.get(key)
        if not isinstance(block, dict) or block.get("mode") == "none":
            continue
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        if "atr_period" in block:
            block["atr_period"] = max(
                2, int(round(float(block["atr_period"]) * factor)))
        if "atr_mult" in block:
            block["atr_mult"] = max(
                0.1, float(block["atr_mult"]) * factor)
    return out


def merge_stage_metrics(stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge the per-stage scenario summaries for bank storage/display."""
    pooled = [s.get("metrics", {}) for s in stages.values()
              if s.get("metrics")]
    out: dict[str, Any] = {}
    for key in METRIC_KEYS:
        medians = [float(m.get(key, {}).get("median", 0.0)) for m in pooled]
        p05 = [float(m.get(key, {}).get("p05", 0.0)) for m in pooled]
        out[key] = {
            "median": round(sum(medians) / len(medians), 4) if medians else 0.0,
            "p05": round(min(p05), 4) if p05 else 0.0,
        }
    return out


# ----------------------------------------------------------- price transforms
def perturb_parameters(spec: dict[str, Any], pct: float,
                       rng: np.random.Generator) -> dict[str, Any]:
    """Jitter indicator/ATR parameters plus fixed-pip and trade-length limits."""
    out = jitter_spec(spec, pct, rng)
    ex = out.get("exit", {})
    for key in ("stop_loss", "take_profit"):
        block = ex.get(key)
        if not isinstance(block, dict) or block.get("mode") == "none":
            continue
        if block.get("type") == "fixed" and block.get("pips") is not None:
            factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
            block["pips"] = max(0.1, float(block["pips"]) * factor)
    if int(ex.get("max_bars_in_trade", 0)) > 0:
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        ex["max_bars_in_trade"] = max(1, int(round(
            float(ex["max_bars_in_trade"]) * factor)))
    if int(ex.get("cooldown_bars", 0)) > 0:
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        ex["cooldown_bars"] = max(0, int(round(
            float(ex["cooldown_bars"]) * factor)))
    execution = out.get("execution") or {}
    for order in execution.get("orders", []):
        if order.get("type") == "market":
            continue
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        if order.get("distance_type") == "fixed" and order.get("pips"):
            order["pips"] = max(0.1, float(order["pips"]) * factor)
        elif order.get("atr_mult"):
            order["atr_mult"] = max(0.05, float(order["atr_mult"]) * factor)
            if order.get("atr_period"):
                order["atr_period"] = max(
                    2, int(round(float(order["atr_period"]) * factor)))
    if int(execution.get("expiry_bars", 0) or 0) > 0:
        factor = 1.0 + rng.uniform(-pct / 100.0, pct / 100.0)
        execution["expiry_bars"] = max(1, int(round(
            float(execution["expiry_bars"]) * factor)))
    return out


def perturb_price_noise(ctx: Context, rng: np.random.Generator,
                        sigma: float) -> Context:
    """Add multiplicative Gaussian noise to the H/L/C series.

    ``sigma`` is a fraction, e.g. ``0.0008`` for 0.08%.  Open prices are left
    untouched; high/low are re-clamped so the stressed bars stay valid OHLC.
    """
    sigma = max(0.0, float(sigma))

    def noisy(values: np.ndarray) -> np.ndarray:
        return values * (1.0 + rng.normal(0.0, sigma, len(values)))

    tf = ctx.tf_df.copy()
    m1_high = noisy(ctx.m1_high)
    m1_low = noisy(ctx.m1_low)
    m1_close = noisy(ctx.m1_close)
    tf["high"] = noisy(tf["high"].to_numpy(dtype=np.float64))
    tf["low"] = noisy(tf["low"].to_numpy(dtype=np.float64))
    tf["close"] = noisy(tf["close"].to_numpy(dtype=np.float64))

    m1_high = np.maximum(m1_high, np.maximum(ctx.m1_open, m1_close))
    m1_low = np.minimum(m1_low, np.minimum(ctx.m1_open, m1_close))
    tf["high"] = np.maximum(tf["high"], np.maximum(tf["open"], tf["close"]))
    tf["low"] = np.minimum(tf["low"], np.minimum(tf["open"], tf["close"]))

    return replace(ctx, tf_df=tf, m1_high=m1_high, m1_low=m1_low,
                   m1_close=m1_close)


@dataclass
class _PriceShape:
    log_returns: np.ndarray
    open_gap: np.ndarray
    high_ratio: np.ndarray
    low_ratio: np.ndarray
    first_close: float
    n_tf: int
    starts_v: np.ndarray
    ends_v: np.ndarray
    valid_mask: np.ndarray
    fill_pos: np.ndarray


def price_shape(ctx: Context) -> _PriceShape:
    """Precompute the invariant pieces used by Monte Carlo path synthesis."""
    close = ctx.m1_close
    first = float(close[0]) if len(close) else 1.0
    prev = np.empty_like(close)
    prev[0] = first
    prev[1:] = close[:-1]
    safe_prev = np.where(prev == 0, first, prev)
    safe_close = np.where(close == 0, first, close)
    idx = ctx.m1_tf_idx
    n_tf = len(ctx.tf_ts)
    starts = np.searchsorted(idx, np.arange(n_tf), side="left")
    ends = np.searchsorted(idx, np.arange(n_tf), side="right")
    valid = ends > starts
    starts_v = starts[valid]
    ends_v = ends[valid]
    valid_idx = np.flatnonzero(valid)
    if len(valid_idx):
        pos = np.searchsorted(valid_idx, np.arange(n_tf), side="right") - 1
        pos = np.clip(pos, 0, len(valid_idx) - 1)
        fill_pos = valid_idx[pos]
    else:
        fill_pos = np.zeros(n_tf, dtype=np.int64)
    log_returns = np.nan_to_num(
        np.diff(np.log(np.where(close > 0, close, np.nan))),
        nan=0.0, posinf=0.0, neginf=0.0)
    return _PriceShape(
        log_returns=log_returns,
        open_gap=ctx.m1_open / safe_prev,
        high_ratio=ctx.m1_high / safe_close,
        low_ratio=ctx.m1_low / safe_close,
        first_close=first,
        n_tf=n_tf,
        starts_v=starts_v,
        ends_v=ends_v,
        valid_mask=valid,
        fill_pos=fill_pos,
    )


def synthetic_context(ctx: Context, shape: _PriceShape,
                      log_returns: np.ndarray) -> Context:
    """Build a Context whose M1 prices follow ``log_returns``.

    The original intrabar shape (open gap, high/close, low/close) is preserved,
    so indicators see a coherent synthetic market and execution still walks
    realistic M1 highs and lows.
    """
    n = len(ctx.m1_close)
    if n == 0:
        return ctx
    close = np.empty(n, dtype=np.float64)
    close[0] = shape.first_close
    if n > 1:
        r = np.asarray(log_returns, dtype=np.float64)[:n - 1]
        if len(r) < n - 1:
            r = np.resize(r, n - 1)
        close[1:] = shape.first_close * np.exp(np.cumsum(r))
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    open_ = prev * shape.open_gap
    high = close * shape.high_ratio
    low = close * shape.low_ratio
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    if len(shape.starts_v):
        tf_open_v = open_[shape.starts_v]
        tf_close_v = close[shape.ends_v - 1]
        tf_high_v = np.maximum.reduceat(high, shape.starts_v)
        tf_low_v = np.minimum.reduceat(low, shape.starts_v)
    else:
        tf_open_v = np.array([close[-1]])
        tf_close_v = np.array([close[-1]])
        tf_high_v = np.array([close[-1]])
        tf_low_v = np.array([close[-1]])
    fill = shape.fill_pos
    tf_open = tf_open_v[fill][:shape.n_tf]
    tf_close = tf_close_v[fill][:shape.n_tf]
    tf_high = tf_high_v[fill][:shape.n_tf]
    tf_low = tf_low_v[fill][:shape.n_tf]
    tf_df = pd.DataFrame({
        "open": tf_open, "high": tf_high, "low": tf_low,
        "close": tf_close, "volume": np.ones(shape.n_tf, dtype=np.float64),
    })
    return replace(ctx, tf_df=tf_df, m1_open=open_, m1_high=high,
                   m1_low=low, m1_close=close)


def block_bootstrap_returns(shape: _PriceShape, rng: np.random.Generator,
                            block_size: int) -> np.ndarray:
    r = shape.log_returns
    n = len(r)
    if n == 0:
        return r
    block = max(1, min(int(block_size), n))
    blocks = int(math.ceil(n / block))
    high = n - block
    starts = (rng.integers(0, high + 1, blocks) if high > 0
              else np.zeros(blocks, dtype=np.int64))
    parts = [r[int(s):int(s) + block] for s in starts]
    return np.concatenate(parts)[:n]


@njit(cache=True, nogil=True)
def _garch_loop(normals: np.ndarray, omega: float, alpha: float,
                beta: float, var0: float) -> np.ndarray:
    out = np.empty_like(normals)
    var = max(var0, 1e-12)
    for i in range(len(normals)):
        shock = math.sqrt(var) * normals[i]
        out[i] = shock
        var = omega + alpha * shock * shock + beta * var
        if var < 1e-12:
            var = 1e-12
    return out


def garch_returns(shape: _PriceShape, rng: np.random.Generator) -> np.ndarray:
    """Simple GARCH(1,1) synthetic returns with fixed, stable coefficients.

    The fit is variance-targeted; alpha/beta are deliberately conservative
    because the purpose is stress generation, not volatility forecasting.
    """
    r = shape.log_returns[np.isfinite(shape.log_returns)]
    if len(r) == 0:
        return shape.log_returns
    mean = float(r.mean())
    eps = r - mean
    var = float(eps.var())
    alpha, beta = 0.08, 0.90
    omega = max(var * (1.0 - alpha - beta), 1e-12)
    normals = rng.standard_normal(len(shape.log_returns))
    shocks = _garch_loop(normals, omega, alpha, beta, var)
    return mean + shocks


def shuffled_max_drawdowns(pnls: np.ndarray, balance: float,
                           rounds: int, rng: np.random.Generator) -> np.ndarray:
    """Max drawdown (%) for ``rounds`` random permutations of trade returns."""
    pnls = np.asarray(pnls, dtype=np.float64)
    if len(pnls) < 2:
        return np.zeros(rounds, dtype=np.float64)
    prior = np.concatenate(([0.0], np.cumsum(pnls)[:-1]))
    equity_before = balance + prior
    returns = np.divide(pnls, equity_before,
                        out=np.zeros_like(pnls),
                        where=np.abs(equity_before) > 1e-9)
    draws = rng.permuted(np.tile(returns, (rounds, 1)), axis=1)
    equity = balance * np.cumprod(1.0 + draws, axis=1)
    curve = np.concatenate(
        (np.full((rounds, 1), balance, dtype=np.float64), equity), axis=1)
    peak = np.maximum.accumulate(curve, axis=1)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve),
                   where=peak > 0)
    return dd.max(axis=1) * 100.0


# --------------------------------------------------------------- stage runners
def _baseline(spec: dict[str, Any], ctx: Context) -> dict[str, Any]:
    return run_backtest(spec, ctx, IndicatorCache())


def run_parameter_stage(spec, ctx, base, stage: ParameterStage, cfg: StressConfig,
                        pool=None, progress=None, stop_event=None) -> dict[str, Any]:
    key = "parameter_sensitivity"

    def one(k: int) -> dict[str, Any]:
        # Each scenario gets its own width in the configured +/- band.
        local = np.random.default_rng(scenario_seed(cfg.seed, key, k + 1))
        pct = float(local.uniform(stage.perturb_min_pct, stage.perturb_max_pct))
        sc_spec = perturb_parameters(spec, pct, local)
        return run_backtest(sc_spec, ctx, IndicatorCache())["metrics_all"]

    metrics = _run_batch(one, list(range(stage.rounds)), pool, 4, key,
                         progress, stop_event)
    stopped = stop_event is not None and stop_event.is_set()
    sharpe_drop = _drop_pct(base["sharpe"], _pct(
        [float(m.get("sharpe", 0.0)) for m in metrics], 50))
    pf_drop = _drop_pct(base["profit_factor"], _pct(
        [float(m.get("profit_factor", 0.0)) for m in metrics], 50))
    profitable = _profitable_share(metrics)
    checks = [
        _check("median_sharpe_drop_pct", sharpe_drop,
               stage.max_sharpe_drop_pct, "<=", "%"),
        _check("median_profit_factor_drop_pct", pf_drop,
               stage.max_profit_factor_drop_pct, "<=", "%"),
        _check("profitable_variants_pct", profitable,
               stage.min_profitable_pct, ">=", "%"),
    ]
    passed = all(c["passed"] for c in checks) and len(metrics) == stage.rounds
    extra = {
        "baseline": {"sharpe": base["sharpe"],
                     "profit_factor": base["profit_factor"]},
        "median_sharpe": _pct([float(m.get("sharpe", 0.0)) for m in metrics], 50),
        "median_profit_factor": _pct(
            [float(m.get("profit_factor", 0.0)) for m in metrics], 50),
        "profitable_variants": sum(
            1 for m in metrics if m.get("net_return", 0.0) > 0),
    }
    return _stage_record(
        key, passed, checks, stage.rounds, metrics, extra,
        status="stopped" if stopped else None)


def run_price_cost_stage(spec, ctx, base, stage: PriceCostStage,
                         cfg: StressConfig, pool=None,
                         progress=None, stop_event=None) -> dict[str, Any]:
    key = "price_cost_sensitivity"
    risk = spec.get("risk", {})
    explicit_baseline = (stage.baseline_slippage_pips is not None
                         or stage.baseline_commission_per_lot is not None)
    base_slip = (float(stage.baseline_slippage_pips)
                 if stage.baseline_slippage_pips is not None
                 else float(risk.get("slippage_pips", 0.0)))
    base_comm = (float(stage.baseline_commission_per_lot)
                 if stage.baseline_commission_per_lot is not None
                 else float(risk.get("commission_per_lot", 0.0)))
    fallback = False
    if not explicit_baseline and base_slip == 0 and base_comm == 0:
        # 2x/3x zero is still zero. Give the cost test a small, conservative
        # floor so a strategy with no configured costs is still stressed.
        base_slip = max(0.1, float(risk.get("spread_pips", 0.0)) * 0.5)
        base_comm = 1.0
        fallback = True

    def price_one(k: int) -> dict[str, Any]:
        local = np.random.default_rng(scenario_seed(cfg.seed, key, k + 1))
        pct = float(local.uniform(stage.noise_min_pct, stage.noise_max_pct))
        sc_ctx = perturb_price_noise(ctx, local, pct / 100.0)
        return run_backtest(spec, sc_ctx, IndicatorCache())["metrics_all"]

    def cost_one(k: int) -> dict[str, Any]:
        local = np.random.default_rng(
            scenario_seed(cfg.seed, key, 10_000 + k + 1))
        mult = float(local.uniform(stage.cost_multiplier_min,
                                   stage.cost_multiplier_max))
        opts = {"slippage_pips": base_slip * mult,
                "commission_per_lot": base_comm * mult}
        return run_backtest(spec, ctx, IndicatorCache(), opts)["metrics_all"]

    price_metrics = _run_batch(price_one, list(range(stage.rounds)), pool, 2,
                               key, progress, stop_event)
    stopped = stop_event is not None and stop_event.is_set()
    cost_metrics: list[dict[str, Any]] = []
    if not stopped:
        cost_metrics = _run_batch(cost_one, list(range(stage.rounds)), pool,
                                  4, key, progress, stop_event)
        stopped = stop_event is not None and stop_event.is_set()

    price_sharpe_drop = _drop_pct(
        base["sharpe"],
        _pct([float(m.get("sharpe", 0.0)) for m in price_metrics], 50))
    cost_profitable = _profitable_share(cost_metrics)
    checks = [
        _check("price_noise_median_sharpe_drop_pct", price_sharpe_drop,
               stage.max_sharpe_drop_pct, "<=", "%"),
        _check("cost_stress_profitable_pct", cost_profitable,
               stage.min_profitable_pct, ">=", "%"),
    ]
    passed = (all(c["passed"] for c in checks)
              and len(price_metrics) == stage.rounds
              and len(cost_metrics) == stage.rounds)
    all_metrics = price_metrics + cost_metrics
    extra = {
        "price_noise": {
            "rounds": len(price_metrics),
            "median_sharpe": _pct(
                [float(m.get("sharpe", 0.0)) for m in price_metrics], 50),
            "median_net_return": _pct(
                [float(m.get("net_return", 0.0)) for m in price_metrics], 50),
            "profitable_pct": _profitable_share(price_metrics),
        },
        "cost_stress": {
            "rounds": len(cost_metrics),
            "median_net_return": _pct(
                [float(m.get("net_return", 0.0)) for m in cost_metrics], 50),
            "profitable_pct": cost_profitable,
            "baseline_slippage_pips": base_slip,
            "baseline_commission_per_lot": base_comm,
            "baseline_fallback": fallback,
        },
    }
    return _stage_record(
        key, passed, checks, stage.rounds * 2, all_metrics, extra,
        status="stopped" if stopped else None)


def run_shuffle_stage(base, stage: ShuffleStage, cfg: StressConfig,
                      progress=None, stop_event=None) -> dict[str, Any]:
    key = "trade_shuffling"
    pnls = np.asarray(base.get("_pnls", []), dtype=np.float64)
    balance = float(base.get("_balance", 10_000.0))
    base_dd = float(base["metrics_all"]["max_dd"])
    effective_base_dd = max(base_dd, 1e-6)
    if stop_event is not None and stop_event.is_set():
        return _stage_record(key, False, [], stage.rounds, [], status="stopped")
    if len(pnls) < 2:
        checks = [_check("closed_trades", len(pnls), 2, ">=", "")]
        return _stage_record(key, False, checks, stage.rounds, [],
                             {"baseline_max_dd": base_dd})

    rng = np.random.default_rng(scenario_seed(cfg.seed, key, 1))
    drawdowns = shuffled_max_drawdowns(pnls, balance, stage.rounds, rng)
    if progress:
        progress(key, stage.rounds)
    p95 = float(np.percentile(drawdowns, 95))
    p99 = float(np.percentile(drawdowns, 99))
    checks = [
        _check("p95_max_dd_multiple", p95 / effective_base_dd,
               stage.p95_max_dd_multiple, "<=", "x"),
        _check("p99_max_dd_multiple", p99 / effective_base_dd,
               stage.p99_max_dd_multiple, "<=", "x"),
    ]
    passed = all(c["passed"] for c in checks)
    extra = {
        "baseline_max_dd": base_dd,
        "p95_max_dd": round(p95, 4),
        "p99_max_dd": round(p99, 4),
        "max_dd": _distribution([float(v) for v in drawdowns]),
        "closed_trades": len(pnls),
    }
    return _stage_record(key, passed, checks, stage.rounds, [], extra,
                         tests_run_override=stage.rounds)


def run_monte_carlo_stage(spec, ctx, base, stage: MonteCarloStage,
                          cfg: StressConfig, pool=None,
                          progress=None, stop_event=None) -> dict[str, Any]:
    key = "monte_carlo"
    shape = price_shape(ctx)
    n = len(ctx.m1_close)

    def one(k: int) -> dict[str, Any]:
        local = np.random.default_rng(scenario_seed(cfg.seed, key, k + 1))
        if stage.method == "garch":
            returns = garch_returns(shape, local)
        else:
            returns = block_bootstrap_returns(shape, local, stage.block_size)
        sc_ctx = synthetic_context(ctx, shape, returns)
        return run_backtest(spec, sc_ctx, IndicatorCache())["metrics_all"]

    metrics = _run_batch(one, list(range(stage.rounds)), pool, 2, key,
                         progress, stop_event)
    stopped = stop_event is not None and stop_event.is_set()
    profitable = _profitable_share(metrics)
    checks = [_check("profitable_paths_pct", profitable,
                     stage.min_profitable_pct, ">=", "%")]
    passed = (all(c["passed"] for c in checks)
              and len(metrics) == stage.rounds)
    extra = {
        "method": stage.method,
        "block_size": stage.block_size if stage.method == "block_bootstrap" else None,
        "profitable_paths": sum(
            1 for m in metrics if m.get("net_return", 0.0) > 0),
        "bars": n,
    }
    return _stage_record(
        key, passed, checks, stage.rounds, metrics, extra,
        status="stopped" if stopped else None)


def run_cross_market_stage(spec, alternates: list[tuple[str, Context]],
                           stage: CrossMarketStage, cfg: StressConfig,
                           pool=None, progress=None,
                           stop_event=None) -> dict[str, Any]:
    key = "cross_market_validation"

    def one(k: int) -> dict[str, Any]:
        symbol, alt_ctx = alternates[k]
        res = run_backtest(spec, alt_ctx, IndicatorCache())
        return {"symbol": symbol, **res["metrics_all"]}

    metrics = _run_batch(one, list(range(len(alternates))), pool, 2, key,
                         progress, stop_event)
    stopped = stop_event is not None and stop_event.is_set()
    pfs = [float(m.get("profit_factor", 0.0)) for m in metrics]
    avg_pf = float(np.mean(pfs)) if pfs else 0.0
    checks = [_check("average_profit_factor", avg_pf,
                     stage.min_avg_profit_factor, ">", "")]
    if not alternates:
        checks.append({"name": "alternate_markets", "value": 0,
                       "threshold": 1, "op": ">=", "unit": "",
                       "passed": False})
    passed = all(c["passed"] for c in checks) and len(metrics) == len(alternates)
    extra = {
        "markets": [
            {"symbol": m.get("symbol"), "profit_factor": m.get("profit_factor"),
             "net_return": m.get("net_return"), "trades": m.get("trades")}
            for m in metrics
        ],
        "markets_tested": len(metrics),
    }
    return _stage_record(
        key, passed, checks, len(alternates), metrics, extra,
        status="stopped" if stopped else None)


# ------------------------------------------------------------------- pipeline
def run_pipeline(spec: dict[str, Any], ctx: Context, cfg: StressConfig,
                 alternates: list[tuple[str, Context]] | None = None,
                 pool=None,
                 progress: Callable[[str, int], None] | None = None,
                 stop_event=None) -> dict[str, Any]:
    """Run the five stages in order, stopping at the first failed stage."""
    alternates = alternates or []
    baseline = _baseline(spec, ctx)
    base = {
        "metrics_all": baseline["metrics_all"],
        "sharpe": float(baseline["metrics_all"]["sharpe"]),
        "profit_factor": float(baseline["metrics_all"]["profit_factor"]),
        "_pnls": np.asarray([t["pnl"] for t in baseline["trades"]],
                            dtype=np.float64),
        "_balance": float(spec.get("risk", {}).get("balance", 10_000.0)),
    }
    stages: dict[str, dict[str, Any]] = {}
    all_passed = True
    tests_run = 0

    runners = {
        "parameter_sensitivity": lambda: run_parameter_stage(
            spec, ctx, base["metrics_all"], cfg.parameter, cfg, pool,
            progress, stop_event),
        "price_cost_sensitivity": lambda: run_price_cost_stage(
            spec, ctx, base["metrics_all"], cfg.price_cost, cfg, pool,
            progress, stop_event),
        "trade_shuffling": lambda: run_shuffle_stage(
            base, cfg.shuffle, cfg, progress, stop_event),
        "monte_carlo": lambda: run_monte_carlo_stage(
            spec, ctx, base["metrics_all"], cfg.monte_carlo, cfg, pool,
            progress, stop_event),
        "cross_market_validation": lambda: run_cross_market_stage(
            spec, alternates, cfg.cross_market, cfg, pool, progress,
            stop_event),
    }

    stopped = False
    for key in STAGES:
        stage_cfg = cfg.stage_config(key)
        if not stage_cfg.enabled:
            stages[key] = {
                "stage": key, "label": STAGE_LABELS[key], "status": "skipped",
                "passed": False, "tests_requested": 0, "tests_run": 0,
                "checks": [], "checks_passed": 0, "checks_total": 0,
                "pass_rate": 0.0, "metrics": {}, "extra": {},
            }
            continue
        if stopped:
            stages[key] = {
                "stage": key, "label": STAGE_LABELS[key], "status": "stopped",
                "passed": False, "tests_requested": 0, "tests_run": 0,
                "checks": [], "checks_passed": 0, "checks_total": 0,
                "pass_rate": 0.0, "metrics": {}, "extra": {},
            }
            continue
        if not all_passed:
            stages[key] = {
                "stage": key, "label": STAGE_LABELS[key], "status": "not_run",
                "passed": False, "tests_requested": 0, "tests_run": 0,
                "checks": [], "checks_passed": 0, "checks_total": 0,
                "pass_rate": 0.0, "metrics": {}, "extra": {},
            }
            continue

        result = runners[key]()
        stages[key] = result
        tests_run += int(result.get("tests_run", 0))
        if result.get("status") == "stopped":
            stopped = True
        if not result.get("passed"):
            all_passed = False

    enabled = cfg.enabled_stages()
    survival = bool(all_passed and enabled
                    and all(stages[k].get("passed") for k in enabled))
    checks_total = sum(int(s.get("checks_total", 0)) for s in stages.values())
    checks_passed = sum(int(s.get("checks_passed", 0)) for s in stages.values())
    return {
        "survival": survival,
        "stages": stages,
        "baseline": baseline["metrics_all"],
        "metrics": merge_stage_metrics(stages),
        "tests_run": tests_run,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "pass_rate": round(checks_passed / checks_total, 4)
        if checks_total else 0.0,
    }
