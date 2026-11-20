from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import threading
from numba import njit

from . import indicators
from .settings import TF_SECONDS, m1_path, tf_path
from .store import store

try:
    import sqx_core as _sqx_core
    _HAS_RUST = True
except Exception:  # noqa: BLE001
    _sqx_core = None
    _HAS_RUST = False

PIP_SIZE = {"JPY": 0.01}


def pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


# --------------------------------------------------------------------- context
@dataclass
class Context:
    symbol: str
    timeframe: str
    tf_seconds: int
    tf_df: pd.DataFrame
    tf_ts: np.ndarray
    tf_close_ts: np.ndarray
    tf_to_m1: np.ndarray
    m1_prev_tf: np.ndarray
    m1_ts: np.ndarray
    m1_open: np.ndarray
    m1_high: np.ndarray
    m1_low: np.ndarray
    m1_close: np.ndarray
    m1_day_id: np.ndarray
    day_ts: np.ndarray
    entry_ok: np.ndarray
    force_close: np.ndarray
    split_idx: int
    split_day: int
    start_ts: int
    end_ts: int
    m1_tf_idx: np.ndarray


def _secs(index: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    # Parquet timestamps may come back at microsecond resolution; normalize
    # to whole epoch seconds explicitly instead of assuming nanoseconds.
    s = pd.to_datetime(index).astype("datetime64[s]")
    return s.astype("int64").to_numpy(dtype=np.int64)


def load_context(symbol: str, timeframe: str, oos_pct: int,
                 session: dict[str, Any],
                 execution_timeframe: str = "M1") -> Context:
    tf = timeframe.upper()
    base_tf = execution_timeframe.upper()
    src = m1_path(symbol) if tf == "M1" else tf_path(symbol, tf)
    if not src.exists():
        raise FileNotFoundError(f"Missing {symbol} {tf} bars. Resample the data first.")
    tdf = pd.read_parquet(src)
    tdf["ts"] = pd.to_datetime(tdf["ts"])
    tdf = tdf.sort_values("ts").reset_index(drop=True)
    tf_ts = _secs(tdf["ts"])
    tf_df = tdf[["open", "high", "low", "close", "volume"]].astype(float).reset_index(drop=True)
    tf_seconds = TF_SECONDS[tf]
    tf_close_ts = tf_ts + tf_seconds

    bsrc = m1_path(symbol) if base_tf == "M1" else tf_path(symbol, base_tf)
    if not bsrc.exists():
        raise FileNotFoundError(
            f"Missing {symbol} {base_tf} bars. Resample the data first.")
    bdf = pd.read_parquet(bsrc)
    bdf["ts"] = pd.to_datetime(bdf["ts"])
    bdf = bdf.sort_values("ts").reset_index(drop=True)
    base_ts = _secs(bdf["ts"])
    base_open = bdf["open"].to_numpy(dtype=np.float64)
    base_high = bdf["high"].to_numpy(dtype=np.float64)
    base_low = bdf["low"].to_numpy(dtype=np.float64)
    base_close = bdf["close"].to_numpy(dtype=np.float64)
    n_base = len(base_ts)

    start_h = int(session.get("start_hour", 0))
    end_h = int(session.get("end_hour", 23))
    days_list = sorted(int(d) for d in session.get("days", [0, 1, 2, 3, 4]))
    close_at_end = bool(session.get("close_at_end", True))

    if _HAS_RUST:
        (tf_to_base, base_prev_tf, day_id_arr, day_ts_arr, entry_ok_arr,
         force_close_arr, split_idx, split_day, base_tf_idx_arr, start_ts,
         end_ts) = _sqx_core.build_context(
            np.asarray(tf_ts, dtype=np.int64),
            np.asarray(tf_close_ts, dtype=np.int64),
            int(tf_seconds),
            np.asarray(base_ts, dtype=np.int64),
            start_h, end_h, days_list, close_at_end, int(oos_pct))
        tf_to_base = np.asarray(tf_to_base, dtype=np.int64)
        base_prev_tf = np.asarray(base_prev_tf, dtype=np.int64)
        day_id = np.asarray(day_id_arr, dtype=np.int64)
        day_ts = np.asarray(day_ts_arr, dtype=np.int64)
        entry_ok = np.asarray(entry_ok_arr, dtype=np.uint8)
        force_close = np.asarray(force_close_arr, dtype=np.uint8)
        split_idx = int(split_idx)
        split_day = int(split_day)
        base_tf_idx = np.asarray(base_tf_idx_arr, dtype=np.int64)
        start_ts = int(start_ts)
        end_ts = int(end_ts)
    else:
        tf_to_base = np.searchsorted(base_ts, tf_close_ts, side="left").astype(np.int64)
        prev = np.searchsorted(tf_close_ts, base_ts, side="right") - 1
        prev[prev < 0] = 0
        base_prev_tf = prev.astype(np.int64)

        day_int = base_ts // 86400
        day_vals, day_id = np.unique(day_int, return_inverse=True)
        day_ts = day_vals.astype(np.int64) * 86400

        hours = ((base_ts % 86400) // 3600).astype(np.int64)
        minutes = ((base_ts % 3600) // 60).astype(np.int64)
        weekdays = ((day_int + 3) % 7).astype(np.int64)  # 0 = Monday
        days = set(days_list)
        day_ok = np.isin(weekdays, np.array(sorted(days), dtype=np.int64))
        if start_h == end_h:
            hour_ok = np.ones(n_base, dtype=bool)
        elif start_h < end_h:
            hour_ok = (hours >= start_h) & (hours < end_h)
        else:
            hour_ok = (hours >= start_h) | (hours < end_h)
        entry_ok = (day_ok & hour_ok).astype(np.uint8)
        entry_ok[-1] = 0

        force_close = np.zeros(n_base, dtype=np.uint8)
        if close_at_end and start_h != end_h:
            force_close = (((hours == end_h) & (minutes == 0)) & day_ok).astype(np.uint8)

        start_ts = int(base_ts[0])
        end_ts = int(base_ts[-1])
        split_ts = start_ts + int((end_ts - start_ts) * (1 - oos_pct / 100.0))
        split_idx = int(np.searchsorted(base_ts, split_ts, side="left"))
        split_idx = max(1, min(split_idx, n_base - 2))
        split_day = int(day_id[split_idx])
        force_close[split_idx] = 1
        embargo_start = int(np.searchsorted(base_ts, base_ts[split_idx] - tf_seconds, side="left"))
        entry_ok[embargo_start:split_idx] = 0
        entry_ok[split_idx] = 0

        base_tf_idx = np.searchsorted(tf_ts, base_ts, side="right") - 1
        base_tf_idx = np.clip(base_tf_idx, 0, max(0, len(tf_ts) - 1)).astype(np.int64)

    return Context(
        symbol=symbol, timeframe=tf, tf_seconds=tf_seconds, tf_df=tf_df,
        tf_ts=tf_ts, tf_close_ts=tf_close_ts, tf_to_m1=tf_to_base,
        m1_prev_tf=base_prev_tf, m1_ts=base_ts, m1_open=base_open, m1_high=base_high,
        m1_low=base_low, m1_close=base_close, m1_day_id=day_id.astype(np.int64),
        day_ts=day_ts, entry_ok=entry_ok, force_close=force_close,
        split_idx=split_idx, split_day=split_day, start_ts=start_ts, end_ts=end_ts,
        m1_tf_idx=base_tf_idx,
    )


# --------------------------------------------------------------- indicator cache
# Process-wide, thread-safe indicator cache. Shared across all attempts/runs so
# identical (indicator, params, dataframe) computations are never repeated.
_GLOBAL_INDICATOR_CACHE: dict[tuple, dict[str, np.ndarray]] = {}
_GLOBAL_INDICATOR_LOCK = threading.Lock()
_GLOBAL_INDICATOR_BYTES = 0
_CACHE_BUDGET_STATE: dict[str, float] = {"at": 0.0, "budget": 0.0}


def _cache_budget() -> int:
    """Indicator-cache byte budget from runtime settings (reloaded ~2s)."""
    import time
    from .runtime import load_runtime
    now = time.monotonic()
    if now - _CACHE_BUDGET_STATE["at"] > 2.0:
        try:
            _CACHE_BUDGET_STATE["budget"] = float(load_runtime().cache_budget_bytes())
        except Exception:  # noqa: BLE001
            _CACHE_BUDGET_STATE["budget"] = 768.0 * 1024 * 1024
        _CACHE_BUDGET_STATE["at"] = now
    return int(_CACHE_BUDGET_STATE["budget"])


def _entry_bytes(val: dict[str, np.ndarray]) -> int:
    total = 0
    for arr in val.values():
        try:
            total += arr.nbytes
        except AttributeError:
            pass
    return total


def clear_indicator_cache() -> int:
    """Drop all cached indicator arrays. Returns bytes released."""
    global _GLOBAL_INDICATOR_BYTES
    with _GLOBAL_INDICATOR_LOCK:
        freed = _GLOBAL_INDICATOR_BYTES
        _GLOBAL_INDICATOR_CACHE.clear()
        _GLOBAL_INDICATOR_BYTES = 0
    return freed


def indicator_cache_stats() -> dict[str, int]:
    with _GLOBAL_INDICATOR_LOCK:
        return {"entries": len(_GLOBAL_INDICATOR_CACHE),
                "bytes": _GLOBAL_INDICATOR_BYTES,
                "budget": _cache_budget()}


def _store_cached(key: tuple, val: dict[str, np.ndarray]) -> None:
    """Insert an entry and evict oldest until under the RAM budget."""
    global _GLOBAL_INDICATOR_BYTES
    size = _entry_bytes(val)
    budget = _cache_budget()
    with _GLOBAL_INDICATOR_LOCK:
        old = _GLOBAL_INDICATOR_CACHE.get(key)
        if old is not None:
            _GLOBAL_INDICATOR_BYTES -= _entry_bytes(old)
        _GLOBAL_INDICATOR_CACHE[key] = val
        _GLOBAL_INDICATOR_BYTES += size
        # Never let a single oversized entry blow the budget; still allow it
        # as the only entry so it can be reused within the current batch.
        while _GLOBAL_INDICATOR_BYTES > budget and len(_GLOBAL_INDICATOR_CACHE) > 1:
            victim = next((k for k in _GLOBAL_INDICATOR_CACHE if k != key), None)
            if victim is None:
                break
            evicted = _GLOBAL_INDICATOR_CACHE.pop(victim)
            _GLOBAL_INDICATOR_BYTES -= _entry_bytes(evicted)


def _indicator_key(ind_id: str, params: dict[str, Any], df: pd.DataFrame):
    fp = (len(df), float(df["close"].iloc[0]) if len(df) else 0.0,
          float(df["close"].iloc[-1]) if len(df) else 0.0)
    return (ind_id, tuple(sorted(
        (k, float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
         else str(v))
        for k, v in params.items())), id(df), fp)


class IndicatorCache:
    def __init__(self, limit: int = 400) -> None:
        self.limit = limit

    def get(self, ind_id: str, params: dict[str, Any], df: pd.DataFrame,
            ts=None):
        key = _indicator_key(ind_id, params, df)
        with _GLOBAL_INDICATOR_LOCK:
            val = _GLOBAL_INDICATOR_CACHE.get(key)
        if val is None:
            val = indicators.compute(ind_id, df, params, ts=ts)
            _store_cached(key, val)
        return val


# ---------------------------------------------------------------- signal building
def _eval_condition(c: dict[str, Any], cache: IndicatorCache,
                    ctx: Context) -> np.ndarray:
    arr = cache.get(c["indicator"], c["params"], ctx.tf_df,
                    ts=ctx.tf_ts)[c["output"]]
    a = arr.astype(np.float64, copy=False)
    op = c["operator"]
    if op == "is_true":
        return np.nan_to_num(a, nan=0.0) > 0.5
    kind = c["operand"]["kind"]
    if kind == "price":
        b = ctx.tf_df["close"].to_numpy(dtype=np.float64)
    elif kind == "level":
        b = np.full(len(a), float(c["operand"]["value"]), dtype=np.float64)
    else:
        return np.zeros(len(a), dtype=bool)
    if op in ("gt",):
        sig = a > b
    elif op in ("ge", "gte"):
        sig = a >= b
    elif op in ("lt",):
        sig = a < b
    elif op in ("le", "lte"):
        sig = a <= b
    elif op == "cross_above":
        sig = np.zeros(len(a), dtype=bool)
        sig[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    elif op == "cross_below":
        sig = np.zeros(len(a), dtype=bool)
        sig[1:] = (a[1:] < b[1:]) & (a[:-1] >= b[:-1])
    else:
        sig = np.zeros(len(a), dtype=bool)
    return sig & np.isfinite(a) & np.isfinite(b)


def build_tf_signal(conditions: list[dict[str, Any]], cache: IndicatorCache,
                    ctx: Context) -> np.ndarray:
    n = len(ctx.tf_df)
    if not conditions:
        return np.zeros(n, dtype=bool)
    sig = np.ones(n, dtype=bool)
    for c in conditions:
        sig &= _eval_condition(c, cache, ctx)
    return sig


def _to_m1(tf_sig: np.ndarray, ctx: Context) -> np.ndarray:
    out = np.zeros(len(ctx.m1_ts), dtype=np.uint8)
    idx = ctx.tf_to_m1
    valid = (idx >= 0) & (idx < len(out))
    np.maximum.at(out, idx[valid], tf_sig[valid].astype(np.uint8))
    return out


def _atr_m1(period: int, cache: IndicatorCache, ctx: Context) -> np.ndarray:
    pct = cache.get("atr", {"period": period}, ctx.tf_df)["atr_pct"]
    price_atr = pct / 100.0 * ctx.tf_df["close"].to_numpy(dtype=np.float64)
    mapped = price_atr[ctx.m1_prev_tf]
    mapped = np.nan_to_num(mapped, nan=0.0, posinf=0.0, neginf=0.0)
    return mapped.astype(np.float64)


# -------------------------------------------------------------------- simulation
@njit(cache=True, nogil=True)
def _simulate(
    op, hi, lo, cl, day_id, n_days,
    entry_long, entry_short, exit_long, exit_short,
    entry_ok, force_close, split_idx,
    sl_atr, tp_atr, trail_atr,
    sl_mode, sl_fixed, tp_mode, tp_fixed, trail_mode,
    lots, use_risk_pct, risk_pct, leverage, contract, pip,
    half_spread, slippage, commission,
    opposite_reverse, max_bars, cooldown_bars, balance,
    daily_eq, t_entry_i, t_exit_i, t_side, t_entry_px, t_exit_px,
    t_pnl, t_reason,
    skip_entry, skip_exit, random_close,
    order_types, order_dists, order_expiry,
):
    n = cl.shape[0]
    eq = balance
    peak = balance
    maxdd = 0.0
    pos = 0
    entry_px = 0.0
    entry_i = 0
    lots_cur = 0.0
    sl = 0.0
    tp = 0.0
    trail = 0.0
    trade_n = 0
    cooldown_until = -1
    max_trades = t_pnl.shape[0]
    n_orders = order_types.shape[0]
    pending_side = np.zeros(n_orders, dtype=np.int64)
    pending_px = np.zeros(n_orders, dtype=np.float64)
    pending_until = np.zeros(n_orders, dtype=np.int64)
    has_pending = False

    for d in range(n_days):
        daily_eq[d] = balance

    for i in range(n):
        # forced exits at the bar open
        if pos != 0:
            reason = 0
            raw = 0.0
            if force_close[i] or i == split_idx:
                reason = 6 if force_close[i] else 8
                raw = op[i]
            elif max_bars > 0 and (i - entry_i) >= max_bars:
                reason = 7
                raw = op[i]
            elif random_close[i] == 1 and i > entry_i:
                reason = 10
                raw = op[i]
            if reason != 0:
                if pos == 1:
                    exit_px = raw - half_spread - slippage
                    pnl = (exit_px - entry_px) * contract * lots_cur / exit_px
                else:
                    exit_px = raw + half_spread + slippage
                    pnl = (entry_px - exit_px) * contract * lots_cur / exit_px
                eq += pnl - commission * lots_cur
                if trade_n < max_trades:
                    t_entry_i[trade_n] = entry_i
                    t_exit_i[trade_n] = i
                    t_side[trade_n] = pos
                    t_entry_px[trade_n] = entry_px
                    t_exit_px[trade_n] = exit_px
                    t_pnl[trade_n] = pnl - 2.0 * commission * lots_cur
                    t_reason[trade_n] = reason
                    trade_n += 1
                    cooldown_until = i + cooldown_bars
                pos = 0

        # indicator exit at the open
        if pos != 0:
            if skip_exit[i] == 0 and ((pos == 1 and exit_long[i]) or (pos == -1 and exit_short[i])):
                raw = op[i]
                if pos == 1:
                    exit_px = raw - half_spread - slippage
                    pnl = (exit_px - entry_px) * contract * lots_cur / exit_px
                else:
                    exit_px = raw + half_spread + slippage
                    pnl = (entry_px - exit_px) * contract * lots_cur / exit_px
                eq += pnl - commission * lots_cur
                if trade_n < max_trades:
                    t_entry_i[trade_n] = entry_i
                    t_exit_i[trade_n] = i
                    t_side[trade_n] = pos
                    t_entry_px[trade_n] = entry_px
                    t_exit_px[trade_n] = exit_px
                    t_pnl[trade_n] = pnl - 2.0 * commission * lots_cur
                    t_reason[trade_n] = 4
                    trade_n += 1
                    cooldown_until = i + cooldown_bars
                pos = 0

        # opposite signal reversal / close at open
        if pos != 0 and opposite_reverse == 1 and entry_ok[i] == 1 and skip_exit[i] == 0:
            do_rev = (pos == 1 and entry_short[i] == 1) or (pos == -1 and entry_long[i] == 1)
            if do_rev:
                raw = op[i]
                if pos == 1:
                    exit_px = raw - half_spread - slippage
                    pnl = (exit_px - entry_px) * contract * lots_cur / exit_px
                else:
                    exit_px = raw + half_spread + slippage
                    pnl = (entry_px - exit_px) * contract * lots_cur / exit_px
                eq += pnl - commission * lots_cur
                if trade_n < max_trades:
                    t_entry_i[trade_n] = entry_i
                    t_exit_i[trade_n] = i
                    t_side[trade_n] = pos
                    t_entry_px[trade_n] = entry_px
                    t_exit_px[trade_n] = exit_px
                    t_pnl[trade_n] = pnl - 2.0 * commission * lots_cur
                    t_reason[trade_n] = 5
                    trade_n += 1
                    # allow the reverse entry on this same bar
                    cooldown_until = i - 1
                pos = 0

        # pending limit/stop orders trigger when price reaches the level
        pending_fill_side = 0
        pending_fill_px = 0.0
        if pos == 0 and has_pending and entry_ok[i] == 1 and skip_entry[i] == 0:
            for k in range(n_orders):
                if pending_side[k] == 0:
                    continue
                if pending_until[k] >= 0 and i > pending_until[k]:
                    pending_side[k] = 0
                    continue
                typ = order_types[k]
                px = pending_px[k]
                hit = False
                fill = 0.0
                if typ == 1 and pending_side[k] == 1 and lo[i] <= px:
                    fill = min(op[i], px)
                    hit = True
                elif typ == 1 and pending_side[k] == -1 and hi[i] >= px:
                    fill = max(op[i], px)
                    hit = True
                elif typ == 2 and pending_side[k] == 1 and hi[i] >= px:
                    fill = max(op[i], px)
                    hit = True
                elif typ == 2 and pending_side[k] == -1 and lo[i] <= px:
                    fill = min(op[i], px)
                    hit = True
                if hit:
                    pending_fill_side = pending_side[k]
                    pending_fill_px = fill
                    break
            still_pending = 0
            for k in range(n_orders):
                if pending_side[k] != 0:
                    still_pending += 1
            if pending_fill_side != 0:
                for k in range(n_orders):
                    pending_side[k] = 0
                has_pending = False
            elif still_pending == 0:
                has_pending = False

        # pending fills and fresh entries
        if pos == 0 and i > cooldown_until and skip_entry[i] == 0:
            side = 0
            raw = 0.0
            if pending_fill_side != 0:
                side = pending_fill_side
                raw = pending_fill_px
            elif entry_ok[i] == 1:
                if entry_long[i] == 1 and entry_short[i] == 1:
                    side = 1
                elif entry_long[i] == 1:
                    side = 1
                elif entry_short[i] == 1:
                    side = -1
                if side != 0:
                    # a fresh signal replaces any still-pending orders
                    for k in range(n_orders):
                        pending_side[k] = 0
                    has_pending = False
                    has_market = False
                    for k in range(n_orders):
                        if order_types[k] == 0:
                            has_market = True
                    if has_market:
                        raw = op[i]
                    else:
                        placed = 0
                        for k in range(n_orders):
                            d = order_dists[k, i]
                            if d <= 0:
                                continue
                            if order_types[k] == 1:
                                pending_px[k] = op[i] - d if side == 1 else op[i] + d
                            else:
                                pending_px[k] = op[i] + d if side == 1 else op[i] - d
                            pending_side[k] = side
                            pending_until[k] = (i + order_expiry
                                                if order_expiry > 0 else -1)
                            placed += 1
                        if placed > 0:
                            has_pending = True
                            side = 0
                        else:
                            # no usable distance: fall back to a market fill
                            raw = op[i]
            if side != 0:
                if side == 1:
                    entry_px = raw + half_spread + slippage
                    dist_sl = sl_fixed if sl_mode == 1 else sl_atr[i]
                    dist_tp = tp_fixed if tp_mode == 1 else tp_atr[i]
                    dist_tr = trail_atr[i]
                    if sl_mode != 0 and dist_sl <= 0:
                        side = 0
                    if tp_mode != 0 and dist_tp <= 0:
                        side = 0
                    if trail_mode != 0 and dist_tr <= 0:
                        side = 0
                    if side != 0:
                        sl = entry_px - dist_sl if sl_mode != 0 else 0.0
                        tp = entry_px + dist_tp if tp_mode != 0 else 0.0
                        trail = entry_px - dist_tr if trail_mode != 0 else 0.0
                else:
                    entry_px = raw - half_spread - slippage
                    dist_sl = sl_fixed if sl_mode == 1 else sl_atr[i]
                    dist_tp = tp_fixed if tp_mode == 1 else tp_atr[i]
                    dist_tr = trail_atr[i]
                    if sl_mode != 0 and dist_sl <= 0:
                        side = 0
                    if tp_mode != 0 and dist_tp <= 0:
                        side = 0
                    if trail_mode != 0 and dist_tr <= 0:
                        side = 0
                    if side != 0:
                        sl = entry_px + dist_sl if sl_mode != 0 else 0.0
                        tp = entry_px - dist_tp if tp_mode != 0 else 0.0
                        trail = entry_px + dist_tr if trail_mode != 0 else 0.0
                if side != 0:
                    if use_risk_pct == 1 and sl_mode != 0:
                        risk_money = eq * risk_pct / 100.0
                        # pip value per lot in account currency
                        pv = (pip * contract / entry_px) if entry_px > 0 else 0.0
                        sl_pips = abs(entry_px - sl) / pip if pip > 0 else 0.0
                        if sl_pips > 0 and pv > 0:
                            lots_cur = risk_money / (sl_pips * pv)
                        else:
                            lots_cur = lots
                    else:
                        lots_cur = lots
                    max_lots = eq * leverage / contract if contract > 0 else 0.0
                    if lots_cur > max_lots:
                        lots_cur = max_lots
                    if lots_cur > 0:
                        pos = side
                        entry_i = i
                        eq -= commission * lots_cur
                    else:
                        pos = 0

        # intrabar exits (prices adjusted to the side of the book)
        if pos != 0:
            reason = 0
            exit_px = 0.0
            if pos == 1:
                om = op[i] - half_spread - slippage
                lm = lo[i] - half_spread - slippage
                hm = hi[i] - half_spread - slippage
                if sl_mode != 0 and om <= sl:
                    exit_px = om
                    reason = 1
                elif tp_mode != 0 and om >= tp:
                    exit_px = om
                    reason = 2
                elif sl_mode != 0 and lm <= sl:
                    exit_px = sl
                    reason = 1
                elif tp_mode != 0 and hm >= tp:
                    exit_px = tp
                    reason = 2
                elif trail_mode != 0 and lm <= trail:
                    exit_px = trail
                    reason = 3
            else:
                om = op[i] + half_spread + slippage
                hm = hi[i] + half_spread + slippage
                lm = lo[i] + half_spread + slippage
                if sl_mode != 0 and om >= sl:
                    exit_px = om
                    reason = 1
                elif tp_mode != 0 and om <= tp:
                    exit_px = om
                    reason = 2
                elif sl_mode != 0 and hm >= sl:
                    exit_px = sl
                    reason = 1
                elif tp_mode != 0 and lm <= tp:
                    exit_px = tp
                    reason = 2
                elif trail_mode != 0 and hm >= trail:
                    exit_px = trail
                    reason = 3
            if reason != 0:
                if pos == 1:
                    pnl = (exit_px - entry_px) * contract * lots_cur / exit_px
                else:
                    pnl = (entry_px - exit_px) * contract * lots_cur / exit_px
                eq += pnl - commission * lots_cur
                if trade_n < max_trades:
                    t_entry_i[trade_n] = entry_i
                    t_exit_i[trade_n] = i
                    t_side[trade_n] = pos
                    t_entry_px[trade_n] = entry_px
                    t_exit_px[trade_n] = exit_px
                    t_pnl[trade_n] = pnl - 2.0 * commission * lots_cur
                    t_reason[trade_n] = reason
                    trade_n += 1
                    cooldown_until = i + cooldown_bars
                pos = 0
            if pos != 0 and trail_mode != 0:
                if pos == 1:
                    cand = cl[i] - half_spread - slippage - trail_atr[i]
                    if cand > trail:
                        trail = cand
                    if trail < sl and sl_mode != 0:
                        trail = sl
                else:
                    cand = cl[i] + half_spread + slippage + trail_atr[i]
                    if trail == 0.0 or cand < trail:
                        trail = cand
                    if trail > sl and sl_mode != 0:
                        trail = sl

        # mark to market
        if pos == 1:
            eq_mark = eq + (cl[i] - entry_px) * contract * lots_cur / cl[i]
        elif pos == -1:
            eq_mark = eq + (entry_px - cl[i]) * contract * lots_cur / cl[i]
        else:
            eq_mark = eq
        daily_eq[day_id[i]] = eq_mark
        if eq_mark > peak:
            peak = eq_mark
        dd = (peak - eq_mark) / peak if peak > 0 else 0.0
        if dd > maxdd:
            maxdd = dd

    if pos != 0:
        i = n - 1
        if pos == 1:
            exit_px = cl[i] - half_spread - slippage
            pnl = (exit_px - entry_px) * contract * lots_cur / exit_px
        else:
            exit_px = cl[i] + half_spread + slippage
            pnl = (entry_px - exit_px) * contract * lots_cur / exit_px
        eq += pnl - commission * lots_cur
        if trade_n < max_trades:
            t_entry_i[trade_n] = entry_i
            t_exit_i[trade_n] = i
            t_side[trade_n] = pos
            t_entry_px[trade_n] = entry_px
            t_exit_px[trade_n] = exit_px
            t_pnl[trade_n] = pnl - 2.0 * commission * lots_cur
            t_reason[trade_n] = 9
            trade_n += 1
        daily_eq[day_id[i]] = eq

    return eq, maxdd, trade_n


def _simulate_dispatch(*args, **kwargs):
    if _HAS_RUST:
        return _sqx_core.simulate(*args, **kwargs)
    return _simulate(*args, **kwargs)


# ---------------------------------------------------------------------- metrics
def _max_dd(eq: np.ndarray) -> float:
    if len(eq) < 2:
        return 0.0
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
    return float(dd.max())


def segment_metrics(eq_segment: np.ndarray, base: float, day_ts: np.ndarray,
                    trade_pnls: np.ndarray, trades: int) -> dict[str, Any]:
    if len(eq_segment) == 0:
        return {"trades": 0, "net_return": 0.0, "max_dd": 0.0, "return_dd": 0.0,
                "sharpe": 0.0, "stagnation_days": 0, "profit_factor": 0.0,
                "win_rate": 0.0, "avg_trade": 0.0}
    eq = np.concatenate(([base], eq_segment.astype(np.float64)))
    rets = np.diff(eq) / np.where(eq[:-1] == 0, np.nan, eq[:-1])
    rets = rets[np.isfinite(rets)]
    sharpe = 0.0
    if len(rets) > 2 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(252))
    mdd = _max_dd(eq)
    net = float(eq[-1] / base - 1) if base > 0 else 0.0
    retdd = (net * 100 / (mdd * 100)) if mdd > 0 else (999.0 if net > 0 else 0.0)
    # stagnation: longest calendar-day gap without a new equity high
    peak = eq[0]
    last_high_ts = None
    stag = 0
    for k in range(1, len(eq)):
        day = int(day_ts[k - 1])
        if eq[k] > peak:
            peak = eq[k]
            if last_high_ts is not None:
                gap = (day - last_high_ts) / 86400
                if gap > stag:
                    stag = int(gap)
            last_high_ts = day
    if len(eq) > 1:
        if last_high_ts is None:
            stag = int((day_ts[-1] - day_ts[0]) / 86400)
        else:
            stag = max(stag, int((day_ts[-1] - last_high_ts) / 86400))
    pnl = trade_pnls
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else (
        999.0 if len(wins) else 0.0)
    return {
        "trades": int(trades),
        "net_return": round(net * 100, 4),
        "max_dd": round(mdd * 100, 4),
        "return_dd": round(float(retdd), 4),
        "sharpe": round(sharpe, 4),
        "stagnation_days": int(stag),
        "profit_factor": round(pf, 4),
        "win_rate": round(float(len(wins) / len(pnl) * 100), 2) if len(pnl) else 0.0,
        "avg_trade": round(float(pnl.mean()), 4) if len(pnl) else 0.0,
    }


def passes(metrics: dict[str, Any], gates: dict[str, Any]) -> tuple[bool, str]:
    if metrics["trades"] < gates["min_trades"]:
        return False, "min_trades"
    if metrics["sharpe"] < gates["min_sharpe"]:
        return False, "min_sharpe"
    if metrics["stagnation_days"] > gates["max_stagnation_days"]:
        return False, "stagnation"
    if metrics["return_dd"] < gates["min_return_dd"]:
        return False, "return_dd"
    if metrics["max_dd"] > gates["max_drawdown_pct"]:
        return False, "max_dd"
    if metrics["profit_factor"] < gates["min_profit_factor"]:
        return False, "profit_factor"
    if metrics["win_rate"] < gates["min_win_rate"]:
        return False, "win_rate"
    return True, ""


# ------------------------------------------------------------------- public run
def _opt_mask(opts: dict[str, Any], key: str, n: int) -> np.ndarray:
    arr = opts.get(key)
    if arr is None:
        return np.zeros(n, dtype=np.uint8)
    return arr


def run_backtest(spec: dict[str, Any], ctx: Context, cache: IndicatorCache,
                 options: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = options or {}
    entry = spec["entry"]
    ex = spec["exit"]
    risk = spec["risk"]
    long_sig = _to_m1(build_tf_signal(entry.get("long", []), cache, ctx), ctx)
    short_sig = _to_m1(build_tf_signal(entry.get("short", []), cache, ctx), ctx)
    exit_long = _to_m1(build_tf_signal(ex.get("conditions", []), cache, ctx), ctx)
    exit_short = exit_long

    sl = ex["stop_loss"]
    tp = ex["take_profit"]
    tr = ex["trailing"]
    if sl.get("mode") == "none":
        sl_mode, sl_fixed, sl_arr = 0, 0.0, np.zeros(len(ctx.m1_ts))
    elif sl.get("type") == "fixed":
        sl_mode, sl_fixed = 1, float(sl["pips"]) * pip_size(ctx.symbol)
        sl_arr = np.zeros(len(ctx.m1_ts))
    else:
        sl_mode, sl_fixed = 2, 0.0
        sl_arr = _atr_m1(int(sl["atr_period"]), cache, ctx) * float(sl["atr_mult"])
    if tp.get("mode") == "none":
        tp_mode, tp_fixed, tp_arr = 0, 0.0, np.zeros(len(ctx.m1_ts))
    elif tp.get("type") == "fixed":
        tp_mode, tp_fixed = 1, float(tp["pips"]) * pip_size(ctx.symbol)
        tp_arr = np.zeros(len(ctx.m1_ts))
    else:
        tp_mode, tp_fixed = 2, 0.0
        tp_arr = _atr_m1(int(tp["atr_period"]), cache, ctx) * float(tp["atr_mult"])
    if tr.get("mode") == "none":
        trail_mode, trail_arr = 0, np.zeros(len(ctx.m1_ts))
    else:
        trail_mode = 1
        trail_arr = _atr_m1(int(tr["atr_period"]), cache, ctx) * float(tr["atr_mult"])

    n_m1 = len(ctx.m1_ts)
    execution = spec.get("execution") or {}
    raw_orders = execution.get("orders") or [{"type": "market"}]
    if not raw_orders:
        raw_orders = [{"type": "market"}]
    order_types = np.zeros(len(raw_orders), dtype=np.int64)
    order_dists = np.zeros((len(raw_orders), n_m1), dtype=np.float64)
    pip = pip_size(ctx.symbol)
    for k, order in enumerate(raw_orders):
        typ = order.get("type", "market")
        if typ == "market":
            order_types[k] = 0
            continue
        order_types[k] = 1 if typ == "limit" else 2
        if order.get("distance_type", "fixed") == "fixed":
            order_dists[k, :] = float(order.get("pips", 0.0)) * pip
        else:
            order_dists[k, :] = _atr_m1(
                int(order.get("atr_period", 14)), cache, ctx) * float(
                    order.get("atr_mult", 0.5))
    order_expiry = int(execution.get("expiry_bars", 0) or 0)

    daily_eq = np.zeros(len(ctx.day_ts), dtype=np.float64)
    max_trades = 100_000
    t_entry_i = np.zeros(max_trades, dtype=np.int64)
    t_exit_i = np.zeros(max_trades, dtype=np.int64)
    t_side = np.zeros(max_trades, dtype=np.int64)
    t_entry_px = np.zeros(max_trades, dtype=np.float64)
    t_exit_px = np.zeros(max_trades, dtype=np.float64)
    t_pnl = np.zeros(max_trades, dtype=np.float64)
    t_reason = np.zeros(max_trades, dtype=np.int64)

    final_eq, maxdd, trade_n = _simulate_dispatch(
        ctx.m1_open, ctx.m1_high, ctx.m1_low, ctx.m1_close, ctx.m1_day_id,
        len(ctx.day_ts), long_sig, short_sig, exit_long, exit_short,
        ctx.entry_ok, ctx.force_close, ctx.split_idx,
        sl_arr, tp_arr, trail_arr,
        sl_mode, sl_fixed, tp_mode, tp_fixed, trail_mode,
        float(risk["fixed_lot"]), 1 if risk["sizing"] == "risk_percent" else 0,
        float(risk["risk_percent"]), float(risk["leverage"]), 100000.0,
        pip_size(ctx.symbol),
        float(opts.get("spread_pips", risk["spread_pips"])) * pip_size(ctx.symbol) / 2.0,
        float(opts.get("slippage_pips", risk["slippage_pips"])) * pip_size(ctx.symbol) / 2.0,
        float(opts.get("commission_per_lot", risk["commission_per_lot"])),
        1 if ex["opposite_signal"] == "reverse" else 0,
        int(ex["max_bars_in_trade"]), int(ex.get("cooldown_bars", 0)),
        float(risk["balance"]),
        daily_eq, t_entry_i, t_exit_i, t_side, t_entry_px, t_exit_px,
        t_pnl, t_reason,
        _opt_mask(opts, "skip_entry", n_m1),
        _opt_mask(opts, "skip_exit", n_m1),
        _opt_mask(opts, "random_close", n_m1),
        order_types, order_dists, order_expiry,
    )

    trade_n = min(int(trade_n), max_trades)
    entries = t_entry_i[:trade_n]
    exits = t_exit_i[:trade_n]
    sides = t_side[:trade_n]
    epx = t_entry_px[:trade_n]
    xpx = t_exit_px[:trade_n]
    pnls = t_pnl[:trade_n]
    reasons = t_reason[:trade_n]
    exit_days = ctx.m1_day_id[exits] if trade_n else np.array([], dtype=np.int64)
    entry_days = ctx.m1_day_id[entries] if trade_n else np.array([], dtype=np.int64)
    split_day = ctx.split_day

    is_mask = entry_days < split_day
    oos_mask = entry_days >= split_day
    is_eq = daily_eq[:split_day]
    oos_eq = daily_eq[split_day:]
    base = float(risk["balance"])
    is_metrics = segment_metrics(is_eq, base, ctx.day_ts[:split_day],
                                 pnls[is_mask], int(is_mask.sum()))
    oos_base = daily_eq[split_day - 1] if split_day > 0 else base
    oos_metrics = segment_metrics(oos_eq, float(oos_base), ctx.day_ts[split_day:],
                                  pnls[oos_mask], int(oos_mask.sum()))
    all_metrics = segment_metrics(daily_eq, base, ctx.day_ts, pnls, trade_n)

    trades = []
    for k in range(trade_n):
        trades.append({
            "entry_ts": str(pd.Timestamp(int(ctx.m1_ts[entries[k]]), unit="s")),
            "exit_ts": str(pd.Timestamp(int(ctx.m1_ts[exits[k]]), unit="s")),
            "side": "long" if sides[k] == 1 else "short",
            "entry": round(float(epx[k]), 5),
            "exit": round(float(xpx[k]), 5),
            "pnl": round(float(pnls[k]), 2),
            "reason": int(reasons[k]),
        })

    return {
        "metrics_is": is_metrics,
        "metrics_oos": oos_metrics,
        "metrics_all": all_metrics,
        "daily_eq": daily_eq,
        "day_ts": ctx.day_ts,
        "trades": trades,
        "final_equity": float(final_eq),
        "max_dd_m1": float(maxdd * 100),
        "split_day": int(split_day),
        "signal_counts": {
            "long": int(long_sig.sum()), "short": int(short_sig.sum()),
            "exit": int(exit_long.sum()),
        },
    }
