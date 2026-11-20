"""Compose low-correlation portfolios from stress-tested strategy banks.

Strategies are selected greedily on their monthly profit/loss correlation, then
combined with different sizing schemes (equal, inverse volatility and a dynamic
volatility-targeted variant) so the caller can compare stability, return and
drawdown side by side.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .profiles import (DEFAULT_PROFILE, PROPFIRM_STAGES,
                       evaluate as evaluate_profile, get_profile)
from .settings import bank_dir
from .store import store

TRADING_DAYS = 252
MAX_LEVERAGE = 2.0
MAX_STRATEGIES = 200
DEFAULT_BASE = 10000.0


# ------------------------------------------------------------------ data load
def _daily_returns(bank_id: str, strategy_id: str, base: float) -> pd.Series | None:
    path = bank_dir(bank_id) / f"{strategy_id}_equity.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if df.empty or "equity" not in df.columns or "ts" not in df.columns:
        return None
    eq = df.assign(ts=pd.to_datetime(df["ts"])) \
           .set_index("ts")["equity"].astype(float).sort_index()
    eq = eq[~eq.index.duplicated(keep="last")]
    if len(eq) < 5:
        return None
    rets = eq.pct_change()
    if base > 0:
        rets.iloc[0] = eq.iloc[0] / base - 1.0
    return rets.replace([np.inf, -np.inf], np.nan).dropna()


def _monthly(returns: pd.Series) -> pd.Series:
    return (1.0 + returns).groupby(returns.index.to_period("M")).prod() - 1.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ------------------------------------------------------------------- metrics
def _metrics(returns: pd.Series) -> dict[str, Any]:
    empty = {
        "net_return": 0.0, "ann_return": 0.0, "ann_vol": 0.0, "sharpe": 0.0,
        "sortino": 0.0, "max_dd": 0.0, "return_dd": 0.0, "calmar": 0.0,
        "monthly_win_rate": 0.0, "best_month": 0.0, "worst_month": 0.0,
        "max_daily_loss": 0.0, "var95_daily": 0.0, "var95_monthly": 0.0,
        "best_day_share": 100.0, "active_days": 0, "last_6m_return": 0.0,
        "max_dd_6m": 0.0, "underwater_days": 0, "risk_stability": 0.0,
        "months": 0, "exposure": 1.0, "avg_corr": 0.0,
    }
    if len(returns) < 2:
        return empty
    equity = (1.0 + returns).cumprod()
    net = float(equity.iloc[-1] - 1.0)
    years = max(len(returns) / TRADING_DAYS, 1.0 / TRADING_DAYS)
    ann = (1.0 + net) ** (1.0 / years) - 1.0 if net > -1.0 else -1.0
    std = float(returns.std(ddof=1))
    vol = std * np.sqrt(TRADING_DAYS)
    sharpe = (float(returns.mean()) / std * np.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    downside = returns[returns < 0]
    dstd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (float(returns.mean()) / dstd * np.sqrt(TRADING_DAYS)) if dstd > 0 else 0.0
    dd = 1.0 - equity / equity.cummax()
    max_dd = float(dd.max())
    retdd = (net / max_dd) if max_dd > 0 else (999.0 if net > 0 else 0.0)
    calmar = (ann / max_dd) if max_dd > 0 else (999.0 if ann > 0 else 0.0)
    monthly = _monthly(returns)

    # Prop-firm / Darwinex style tail and consistency measures.
    max_daily_loss = max(0.0, -float(returns.min())) * 100.0
    var95_daily = max(0.0, -float(np.percentile(returns.to_numpy(), 5))) * 100.0
    var95_monthly = (max(0.0, -float(np.percentile(monthly.to_numpy(), 5))) * 100.0
                     if len(monthly) > 1 else 0.0)
    gross_profit = float(returns[returns > 0].sum())
    best_day_share = (float(returns.max()) / gross_profit * 100.0
                      if gross_profit > 1e-12 else 100.0)
    active_days = int((returns.abs() > 1e-9).sum())

    cutoff = returns.index.max() - pd.DateOffset(months=6)
    recent = returns[returns.index > cutoff]
    if len(recent) > 1:
        eq6 = (1.0 + recent).cumprod()
        max_dd_6m = float((1.0 - eq6 / eq6.cummax()).max()) * 100.0
        last_6m = float(eq6.iloc[-1] - 1.0) * 100.0
    else:
        max_dd_6m = max_dd * 100.0
        last_6m = 0.0

    peak = float(equity.iloc[0])
    last_high = equity.index[0]
    underwater = 0
    for ts, value in equity.items():
        if float(value) >= peak:
            peak = float(value)
            last_high = ts
        else:
            underwater = max(underwater, (ts - last_high).days)

    rolling = returns.rolling(63).std().dropna()
    if len(rolling) > 2 and float(rolling.mean()) > 1e-12:
        stability = _clamp(1.0 - float(rolling.std(ddof=1)) / float(rolling.mean())) * 100.0
    else:
        stability = 0.0

    return {
        "net_return": round(net * 100, 2),
        "ann_return": round(float(ann) * 100, 2),
        "ann_vol": round(float(vol) * 100, 2),
        "sharpe": round(float(sharpe), 3),
        "sortino": round(float(sortino), 3),
        "max_dd": round(max_dd * 100, 2),
        "return_dd": round(float(retdd), 3),
        "calmar": round(float(calmar), 3),
        "monthly_win_rate": round(float((monthly > 0).mean()) * 100, 1)
        if len(monthly) else 0.0,
        "best_month": round(float(monthly.max()) * 100, 2) if len(monthly) else 0.0,
        "worst_month": round(float(monthly.min()) * 100, 2) if len(monthly) else 0.0,
        "max_daily_loss": round(max_daily_loss, 2),
        "var95_daily": round(var95_daily, 2),
        "var95_monthly": round(var95_monthly, 2),
        "best_day_share": round(best_day_share, 1),
        "active_days": active_days,
        "last_6m_return": round(last_6m, 2),
        "max_dd_6m": round(max_dd_6m, 2),
        "underwater_days": int(underwater),
        "risk_stability": round(stability, 1),
        "months": int(len(monthly)),
        "exposure": 1.0,
        "avg_corr": 0.0,
    }


def _avg_pair_corr(corr: pd.DataFrame, ids: list[str]) -> float:
    vals = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in corr.index and b in corr.columns:
                c = corr.loc[a, b]
                if np.isfinite(c):
                    vals.append(float(c))
    return float(np.mean(vals)) if vals else 0.0


def _weights(mode: str, ids: list[str], daily: pd.DataFrame,
             vol_target: float) -> tuple[dict[str, float], float]:
    n = len(ids)
    vols = {sid: float(daily[sid].std(ddof=1)) for sid in ids}
    if mode == "equal" or all(v <= 0 for v in vols.values()):
        weights = {sid: 1.0 / n for sid in ids}
    else:
        inv = {sid: (1.0 / vols[sid] if vols[sid] > 0 else 0.0) for sid in ids}
        total = sum(inv.values()) or n
        weights = {sid: (inv[sid] / total if total else 1.0 / n) for sid in ids}
    exposure = 1.0
    if mode == "vol_target":
        raw = (daily[ids] * pd.Series(weights)).sum(axis=1)
        vol = float(raw.std(ddof=1)) * np.sqrt(TRADING_DAYS)
        if vol > 1e-9:
            exposure = min(MAX_LEVERAGE, vol_target / vol)
    return weights, exposure


def _equity_points(returns: pd.Series) -> list[dict[str, Any]]:
    if returns.empty:
        return []
    equity = (1.0 + returns).cumprod() * 100.0
    monthly = equity.groupby(equity.index.to_period("M")).last().dropna()
    return [{"ts": ts.to_timestamp(how="end").strftime("%Y-%m-%d"),
             "equity": round(float(v), 2)}
            for ts, v in monthly.items()]


# ------------------------------------------------------------- prop firm sim
_STAGE_INT_KEYS = {"min_days", "max_days"}
_STAGE_NUM_KEYS = {"profit_target_pct", "daily_loss_pct", "max_loss_pct",
                   "consistency_pct", "profit_split_pct", "min_days", "max_days"}


def _stage_config(user: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Merge user stage settings over the prop-firm defaults."""
    user = user or {}
    resolved: dict[str, dict[str, Any]] = {}
    for sid, default in PROPFIRM_STAGES.items():
        cfg = dict(default)
        for key, value in (user.get(sid) or {}).items():
            if key not in cfg or value is None or value == "":
                continue
            if key in _STAGE_NUM_KEYS:
                try:
                    cfg[key] = int(value) if key in _STAGE_INT_KEYS else float(value)
                except (TypeError, ValueError):
                    continue
            elif key in ("daily_mode", "max_mode"):
                if value not in ("fixed", "trailing"):
                    continue
                cfg[key] = value
        resolved[sid] = cfg
    return resolved


def _run_stage(daily: np.ndarray, start: int, cfg: dict[str, Any],
               equity0: float, target_pct: float | None) -> dict[str, Any]:
    """Walk one stage from ``start``; returns pass/fail, reason, days, equity."""
    equity = float(equity0)
    peak = equity
    best_day = 0.0
    target = (equity * (1.0 + target_pct / 100.0)
              if target_pct is not None else None)
    day_limit = (cfg.get("daily_loss_pct") or 0.0) / 100.0
    max_limit = (cfg.get("max_loss_pct") or 0.0) / 100.0
    min_days = int(cfg.get("min_days") or 0)
    max_days = cfg.get("max_days")
    consistency = cfg.get("consistency_pct")
    days = 0
    for r in daily[start:]:
        days += 1
        prev = equity
        equity = prev * (1.0 + float(r))
        if day_limit > 0:
            base_equity = prev if cfg.get("daily_mode") == "trailing" else equity0
            if prev - equity > day_limit * base_equity + 1e-12:
                return {"ok": False, "reason": "daily_loss",
                        "days": days, "equity": equity}
        if max_limit > 0:
            anchor = peak if cfg.get("max_mode") == "trailing" else equity0
            if equity < anchor * (1.0 - max_limit) - 1e-12:
                return {"ok": False, "reason": "max_loss",
                        "days": days, "equity": equity}
        peak = max(peak, equity)
        if equity > prev:
            best_day = max(best_day, equity - prev)
        if target is not None and equity >= target and days >= min_days:
            profit = equity - equity0
            if (consistency and profit > 0
                    and best_day / profit * 100.0 > float(consistency)):
                return {"ok": False, "reason": "consistency",
                        "days": days, "equity": equity}
            return {"ok": True, "reason": "target",
                    "days": days, "equity": equity}
        if max_days is not None and days >= int(max_days):
            return {"ok": False, "reason": "timeout",
                    "days": days, "equity": equity}
    return {"ok": True, "reason": "end_of_data",
            "days": days, "equity": equity}


def _heatmap(monthly: pd.Series) -> dict[str, Any]:
    years = sorted({int(p.year) for p in monthly.index})
    grid: list[list[float | None]] = []
    for year in years:
        row: list[float | None] = []
        for month in range(1, 13):
            vals = [float(v) for p, v in monthly.items()
                    if int(p.year) == year and int(p.month) == month]
            row.append(round(vals[0] * 100.0, 2) if vals else None)
        grid.append(row)
    return {"years": years, "values": grid}


def _simulate_challenge(returns: pd.Series, stages: dict[str, dict[str, Any]],
                        base: float) -> dict[str, Any]:
    """Estimate per-stage pass rates and funded monthly performance."""
    daily = returns.to_numpy(dtype=float)
    n = len(daily)
    reasons: dict[str, dict[str, int]] = {"stage1": {}, "stage2": {}, "funded": {}}
    monthly = _monthly(returns)
    split = float(stages["funded"].get("profit_split_pct") or 80.0)
    mean_monthly = float(monthly.mean()) * 100.0 if len(monthly) else 0.0
    summary: dict[str, Any] = {
        "attempts": 0, "overall_pass_rate": 0.0,
        "stages": [], "config": stages,
        "monthly": {
            "mean": round(mean_monthly, 2),
            "median": round(float(monthly.median()) * 100.0, 2) if len(monthly) else 0.0,
            "positive_pct": round(float((monthly > 0).mean()) * 100.0, 1) if len(monthly) else 0.0,
            "best": round(float(monthly.max()) * 100.0, 2) if len(monthly) else 0.0,
            "worst": round(float(monthly.min()) * 100.0, 2) if len(monthly) else 0.0,
            "payout": round(base * mean_monthly / 100.0 * split / 100.0, 2),
            "profit_split_pct": round(split, 1),
            "months": int(len(monthly)),
        },
        "heatmap": _heatmap(monthly),
    }
    if n <= 20:
        summary["stages"] = [
            {"id": "stage1", "label": stages["stage1"]["label"], "pass_rate": 0.0,
             "attempts": 0, "avg_days": 0, "breaches": {}},
            {"id": "stage2", "label": stages["stage2"]["label"], "pass_rate": 0.0,
             "attempts": 0, "avg_days": 0, "breaches": {}},
            {"id": "funded", "label": stages["funded"]["label"], "pass_rate": 0.0,
             "attempts": 0, "avg_days": 0, "breaches": {}},
        ]
        return summary

    stride = max(1, (n - 20) // 250)
    starts = list(range(0, n - 20, stride))
    s1 = s2 = funded_ok = 0
    days1: list[int] = []
    days2: list[int] = []
    for start in starts:
        r1 = _run_stage(daily, start, stages["stage1"], base,
                        float(stages["stage1"].get("profit_target_pct") or 0.0))
        if not r1["ok"]:
            key = r1["reason"]
            reasons["stage1"][key] = reasons["stage1"].get(key, 0) + 1
            continue
        s1 += 1
        days1.append(int(r1["days"]))
        start2 = start + int(r1["days"])
        r2 = _run_stage(daily, start2, stages["stage2"], float(r1["equity"]),
                        float(stages["stage2"].get("profit_target_pct") or 0.0))
        if not r2["ok"]:
            key = r2["reason"]
            reasons["stage2"][key] = reasons["stage2"].get(key, 0) + 1
            continue
        s2 += 1
        days2.append(int(r2["days"]))
        r3 = _run_stage(daily, start2 + int(r2["days"]),
                        stages["funded"], float(r2["equity"]), None)
        funded_ok += 1 if r3["ok"] else 0
        if not r3["ok"]:
            key = r3["reason"]
            reasons["funded"][key] = reasons["funded"].get(key, 0) + 1

    attempts = len(starts)
    summary["attempts"] = attempts
    summary["stages"] = [
        {"id": "stage1", "label": stages["stage1"]["label"],
         "pass_rate": round(s1 / attempts * 100.0, 1),
         "attempts": attempts,
         "avg_days": round(float(np.mean(days1)), 1) if days1 else 0,
         "breaches": reasons["stage1"]},
        {"id": "stage2", "label": stages["stage2"]["label"],
         "pass_rate": round(s2 / s1 * 100.0, 1) if s1 else 0.0,
         "attempts": s1,
         "avg_days": round(float(np.mean(days2)), 1) if days2 else 0,
         "breaches": reasons["stage2"]},
        {"id": "funded", "label": stages["funded"]["label"],
         "pass_rate": round(funded_ok / s2 * 100.0, 1) if s2 else 0.0,
         "attempts": s2,
         "avg_days": 0,
         "breaches": reasons["funded"]},
    ]
    summary["overall_pass_rate"] = round(s2 / attempts * 100.0, 1)
    return summary


# ------------------------------------------------------------------- compose
def compose(bank_id: str, strategy_ids: list[str] | None = None,
            max_corr: float = 0.5, vol_target: float = 0.12,
            min_months: int = 6,
            profile: str = DEFAULT_PROFILE,
            profile_overrides: dict[str, Any] | None = None,
            stages: dict[str, Any] | None = None) -> dict[str, Any]:
    bank = store.get_bank(bank_id)
    if not bank:
        raise ValueError("Strategy bank not found")
    rows = store.list_bank_strategies(bank_id, limit=1000)
    if strategy_ids:
        wanted = set(strategy_ids)
        rows = [r for r in rows if r["strategy_id"] in wanted]
    if not rows:
        raise ValueError("No strategies selected for the portfolio")
    rows = rows[:MAX_STRATEGIES]

    config = bank.get("config") or {}
    base = float(((config.get("risk") or {}).get("balance") or DEFAULT_BASE))
    resolved_stages = _stage_config(stages)

    daily: dict[str, pd.Series] = {}
    monthly: dict[str, pd.Series] = {}
    info: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = row["strategy_id"]
        rets = _daily_returns(bank_id, sid, base)
        item = {
            "strategy_id": sid,
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "trades": (row.get("metrics_oos") or {}).get("trades")
            or (row.get("metrics_is") or {}).get("trades"),
            "sharpe": (row.get("metrics_oos") or {}).get("sharpe"),
            "return_dd": (row.get("metrics_oos") or {}).get("return_dd"),
            "has_data": rets is not None,
            "months": 0,
            "monthly_vol": 0.0,
            "monthly_win_rate": 0.0,
            "included": False,
            "reason": "",
        }
        if rets is not None:
            m = _monthly(rets)
            if len(m) >= min_months:
                daily[sid] = rets
                monthly[sid] = m
                item["months"] = int(len(m))
                item["monthly_vol"] = round(float(m.std(ddof=1)) * 100, 2) if len(m) > 1 else 0.0
                item["monthly_win_rate"] = round(float((m > 0).mean()) * 100, 1)
            else:
                item["reason"] = f"only {len(m)} months of history"
        else:
            item["reason"] = "no equity data in this bank"
        info[sid] = item

    if not daily:
        raise ValueError(
            "None of the selected strategies has enough monthly history to compose")

    ids_all = list(daily)
    mdf = pd.concat(monthly, axis=1)
    corr = mdf.corr(min_periods=min_months)

    def _corr(a: str, b: str) -> float:
        if a in corr.index and b in corr.columns:
            c = corr.loc[a, b]
            if np.isfinite(c):
                return float(c)
        return float("nan")

    def _strength(sid: str) -> float:
        return float(info[sid]["sharpe"] or 0.0)

    selected: list[str] = []
    for sid in sorted(ids_all, key=_strength, reverse=True):
        conflict = None
        for other in selected:
            c = _corr(sid, other)
            if np.isfinite(c) and abs(c) > max_corr:
                conflict = (other, c)
                break
        if conflict:
            info[sid]["reason"] = (f"monthly correlation {conflict[1]:.2f} "
                                   f"with {conflict[0]}")
            continue
        selected.append(sid)
        info[sid]["included"] = True
        info[sid]["reason"] = "selected"

    if not selected:
        raise ValueError("No strategy survived the correlation limit")

    ddf = pd.concat({sid: daily[sid] for sid in selected}, axis=1).fillna(0.0)
    selected_corr = mdf[selected].corr(min_periods=min_months)
    avg_corr = _avg_pair_corr(selected_corr, selected)

    portfolios: list[dict[str, Any]] = []
    for mode, label in (
        ("equal", "Equal weight"),
        ("inverse_vol", "Inverse volatility"),
        ("vol_target", f"Volatility target {vol_target * 100:.0f}%"),
    ):
        weights, exposure = _weights(mode, selected, ddf, vol_target)
        rets = (ddf[selected] * pd.Series(weights)).sum(axis=1) * exposure
        metrics = _metrics(rets)
        metrics["exposure"] = round(float(exposure), 3)
        metrics["trades"] = int(sum(
            (info[sid]["trades"] or 0) for sid in selected))
        metrics["avg_corr"] = round(avg_corr, 3)
        challenge = None
        if profile == "propfirm":
            challenge = _simulate_challenge(rets, resolved_stages, base)
            for stage in challenge["stages"]:
                metrics[f"{stage['id']}_pass_rate"] = stage["pass_rate"]
        profiled = evaluate_profile(profile, metrics, profile_overrides)
        entry = {
            "id": mode,
            "label": label,
            "weights": {sid: round(float(w), 4) for sid, w in weights.items()},
            "metrics": metrics,
            "score": profiled["score"],
            "profile": profiled,
            "equity": _equity_points(rets),
        }
        if challenge is not None:
            entry["challenge"] = challenge
        portfolios.append(entry)

    best = max(portfolios, key=lambda p: p["score"])
    months_index = mdf.index
    return {
        "bank_id": bank_id,
        "bank_name": bank.get("name"),
        "base_balance": base,
        "window": {
            "start": str(months_index.min()),
            "end": str(months_index.max()),
            "months": int(len(months_index)),
        },
        "max_corr": max_corr,
        "vol_target": vol_target,
        "strategies": list(info.values()),
        "selected": selected,
        "avg_corr": round(avg_corr, 3),
        "correlation": {
            "ids": selected,
            "matrix": [[
                (round(float(_corr(a, b)), 3) if np.isfinite(_corr(a, b)) else None)
                for b in selected
            ] for a in selected],
        },
        "portfolios": portfolios,
        "best": best["id"],
        "profile": get_profile(profile),
        "stages": resolved_stages if profile == "propfirm" else None,
    }
