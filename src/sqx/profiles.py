"""Portfolio profiles: which metrics matter for whom, and at what threshold.

Thresholds are research-backed for the three use cases:

* **Prop firm** — FTMO's published objectives for the 2-Step challenge (10%
  profit target, 5% max daily loss, 10% static max loss, 4 trading days per
  phase) plus the best-day consistency rule of the 1-Step/Instant accounts
  (best day at most 50% of total profit). Any breach of the first two rules is
  disqualifying, so they carry the most weight.
* **Personal account** — risk-adjusted return quality: Sharpe/Sortino/Calmar,
  drawdown depth and recovery time are the standard measures of whether a
  return stream is investable and survivable.
* **Darwinex DARWIN** — Darwinex standardises DARWINs to a 6.5% monthly VaR
  (95%) ceiling, scores risk stability (Rs) over the last 12 D-periods, and its
  DarwinIA rating weights the trailing 6-month return heavily (67%) with a
  recent drawdown component (11%) and a 6-18 month track-record bonus.

The rules below are stored data, not code paths, so a profile can be edited
without touching the composer.
"""

from __future__ import annotations

from typing import Any

PROPFIRM_STAGES: dict[str, dict[str, Any]] = {
    "stage1": {
        "label": "Stage 1 · Challenge",
        "profit_target_pct": 10.0,
        "daily_loss_pct": 5.0,
        "daily_mode": "fixed",
        "max_loss_pct": 10.0,
        "max_mode": "fixed",
        "min_days": 4,
        "max_days": None,
        "consistency_pct": None,
    },
    "stage2": {
        "label": "Stage 2 · Verification",
        "profit_target_pct": 5.0,
        "daily_loss_pct": 5.0,
        "daily_mode": "fixed",
        "max_loss_pct": 10.0,
        "max_mode": "fixed",
        "min_days": 4,
        "max_days": None,
        "consistency_pct": None,
    },
    "funded": {
        "label": "Funded",
        "profit_target_pct": 0.0,
        "daily_loss_pct": 5.0,
        "daily_mode": "fixed",
        "max_loss_pct": 10.0,
        "max_mode": "fixed",
        "min_days": 0,
        "max_days": None,
        "consistency_pct": None,
        "profit_split_pct": 80.0,
    },
}

PROFILES: dict[str, dict[str, Any]] = {
    "propfirm": {
        "id": "propfirm",
        "label": "Prop firm challenge",
        "subtitle": "FTMO-style evaluation: pass each stage, survive the loss limits, and keep a consistent funded curve.",
        "stage_defaults": PROPFIRM_STAGES,
        "rules": [
            {"key": "stage1_pass_rate", "label": "Stage 1 pass rate", "op": ">=",
             "threshold": 60.0, "unit": "%", "weight": 15,
             "rule": "Share of historical start dates that clear the challenge target"},
            {"key": "stage2_pass_rate", "label": "Stage 2 pass rate", "op": ">=",
             "threshold": 60.0, "unit": "%", "weight": 10,
             "rule": "Verification pass rate, conditional on passing stage 1"},
            {"key": "funded_pass_rate", "label": "Funded survival rate", "op": ">=",
             "threshold": 70.0, "unit": "%", "weight": 10,
             "rule": "Funded accounts that never breach the loss limits afterwards"},
            {"key": "max_dd", "label": "Max drawdown", "op": "<=",
             "threshold": 10.0, "unit": "%", "weight": 15,
             "rule": "FTMO max loss 10% (static floor, 2-Step)"},
            {"key": "max_daily_loss", "label": "Worst daily loss", "op": "<=",
             "threshold": 5.0, "unit": "%", "weight": 15,
             "rule": "FTMO max daily loss 5% (3% on 1-Step/Instant)"},
            {"key": "net_return", "label": "Net return", "op": ">=",
             "threshold": 10.0, "unit": "%", "weight": 10,
             "rule": "FTMO phase 1 profit target 10% (phase 2 is 5%)"},
            {"key": "best_day_share", "label": "Best day share of profit",
             "op": "<=", "threshold": 50.0, "unit": "%", "weight": 10,
             "rule": "FTMO 1-Step best-day rule: <= 50% of total profit"},
            {"key": "return_dd", "label": "Return / drawdown", "op": ">=",
             "threshold": 1.5, "unit": "x", "weight": 5,
             "rule": "Comfortable buffer above the 10% loss limit"},
            {"key": "sharpe", "label": "Sharpe ratio", "op": ">=",
             "threshold": 0.8, "unit": "", "weight": 5,
             "rule": "Consistent equity growth across the challenge window"},
            {"key": "monthly_win_rate", "label": "Monthly win rate", "op": ">=",
             "threshold": 55.0, "unit": "%", "weight": 5,
             "rule": "Avoid a single bad month ending the challenge"},
        ],
        "notes": [
            "Stage rules are editable: fixed or trailing daily/overall drawdown, "
            "optional maximum days and optional consistency cap.",
            "Daily loss is measured on the worst single day; FTMO resets the "
            "limit at 00:00 CE(S)T from that day's starting balance.",
            "Pass rates are estimated by starting the challenge at up to 250 "
            "evenly spaced historical dates and simulating each stage.",
        ],
        "sources": [
            "https://ftmo.com/en/trading-objectives",
            "https://tradereview.app/blog/ftmo-rules/",
        ],
    },
    "personal": {
        "id": "personal",
        "label": "Personal account",
        "subtitle": "Risk-adjusted quality: Sharpe, Sortino and Calmar first, then drawdown depth and recovery time.",
        "rules": [
            {"key": "sharpe", "label": "Sharpe ratio", "op": ">=",
             "threshold": 1.0, "unit": "", "weight": 20,
             "rule": "Sharpe >= 1 is the usual bar for a deployable strategy"},
            {"key": "max_dd", "label": "Max drawdown", "op": "<=",
             "threshold": 20.0, "unit": "%", "weight": 20,
             "rule": "A 20% drawdown is the common personal risk budget"},
            {"key": "sortino", "label": "Sortino ratio", "op": ">=",
             "threshold": 1.5, "unit": "", "weight": 15,
             "rule": "Downside-only risk should be well compensated"},
            {"key": "calmar", "label": "Calmar ratio", "op": ">=",
             "threshold": 1.0, "unit": "", "weight": 15,
             "rule": "Annual return should at least match max drawdown"},
            {"key": "return_dd", "label": "Return / drawdown", "op": ">=",
             "threshold": 2.0, "unit": "x", "weight": 10,
             "rule": "Reward-to-pain buffer on top of Calmar"},
            {"key": "monthly_win_rate", "label": "Monthly win rate", "op": ">=",
             "threshold": 55.0, "unit": "%", "weight": 10,
             "rule": "Smoother income, fewer losing months to sit through"},
            {"key": "ann_vol", "label": "Annualised volatility", "op": "<=",
             "threshold": 20.0, "unit": "%", "weight": 5,
             "rule": "Keeps leverage and margin swings manageable"},
            {"key": "underwater_days", "label": "Longest underwater period",
             "op": "<=", "threshold": 365, "unit": "d", "weight": 5,
             "rule": "Recovery within a year keeps the strategy usable"},
        ],
        "notes": [
            "Drawdown and underwater time are measured on the composed equity "
            "curve, so they already include the diversification benefit.",
            "Volatility below 20% leaves room to upsize later without changing "
            "the strategy logic.",
        ],
        "sources": [
            "https://www.investopedia.com/terms/s/sharperatio.asp",
            "https://www.investopedia.com/terms/s/sortinoratio.asp",
        ],
    },
    "darwinex": {
        "id": "darwinex",
        "label": "Darwinex DARWIN",
        "subtitle": "Darwinex-compatible risk: 6.5% monthly VaR ceiling, stable risk, long track record, 6-month momentum.",
        "rules": [
            {"key": "var95_monthly", "label": "Monthly VaR (95%)", "op": "<=",
             "threshold": 6.5, "unit": "%", "weight": 25,
             "rule": "DARWINs are replicated to a max 6.5% monthly VaR"},
            {"key": "max_dd_6m", "label": "Max drawdown, last 6 months",
             "op": "<=", "threshold": 15.0, "unit": "%", "weight": 15,
             "rule": "DarwinIA rating includes the recent 6-month max drawdown (11%)"},
            {"key": "months", "label": "Track record", "op": ">=",
             "threshold": 24, "unit": "m", "weight": 15,
             "rule": "Darwinex recommends 2-3+ years of consistent returns"},
            {"key": "last_6m_return", "label": "Return, last 6 months",
             "op": ">=", "threshold": 0.0, "unit": "%", "weight": 15,
             "rule": "DarwinIA weights the trailing 6-month return at 67%"},
            {"key": "risk_stability", "label": "Risk stability", "op": ">=",
             "threshold": 60.0, "unit": "%", "weight": 10,
             "rule": "Rs attribute: stable risk is easier for the risk engine to replicate"},
            {"key": "avg_corr", "label": "Avg pairwise correlation", "op": "<=",
             "threshold": 0.5, "unit": "", "weight": 10,
             "rule": "Market-correlation/diversification the DARWIN portfolio needs"},
            {"key": "sharpe", "label": "Sharpe ratio", "op": ">=",
             "threshold": 1.0, "unit": "", "weight": 10,
             "rule": "Return-curve quality behind the D-Score"},
        ],
        "notes": [
            "Darwinex scales every DARWIN to a 3.25%-6.5% monthly VaR, so the "
            "monthly VaR is the primary gate here.",
            "Risk stability is estimated from the stability of rolling 3-month "
            "volatility over the portfolio's history.",
            "The 6-month return and recent drawdown mirror the DarwinIA rating "
            "inputs (22% current month, 67% 6-month return, 11% drawdown).",
        ],
        "sources": [
            "https://help.darwinex.com/risk-stability-attribute",
            "https://help.darwinex.com/d-score",
            "https://help.darwinex.com/darwinia-rating",
        ],
    },
}

DEFAULT_PROFILE = "personal"


def get_profile(profile_id: str | None) -> dict[str, Any]:
    return PROFILES.get(profile_id or "", PROFILES[DEFAULT_PROFILE])


def list_profiles() -> list[dict[str, Any]]:
    return list(PROFILES.values())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _ratio(op: str, value: float | None, threshold: float) -> tuple[float, bool]:
    if value is None:
        return 0.0, False
    if op == ">=":
        ok = value >= threshold
        ratio = (value / threshold) if threshold > 0 else (1.0 if ok else 0.0)
    else:
        ok = value <= threshold
        if threshold <= 0:
            ratio = 1.0 if ok else 0.0
        else:
            ratio = (threshold / value) if value > 0 else 1.0
    return _clamp(ratio), ok


def evaluate(profile_id: str | None, metrics: dict[str, Any],
             overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score a portfolio against the profile's thresholds (0-100).

    ``overrides`` replaces individual rule thresholds by rule key.
    """
    profile = get_profile(profile_id)
    overrides = overrides or {}
    checks: list[dict[str, Any]] = []
    score = 0.0
    for rule in profile["rules"]:
        threshold = float(rule["threshold"])
        override = overrides.get(rule["key"])
        if override is not None:
            try:
                threshold = float(override)
            except (TypeError, ValueError):
                pass
        value = metrics.get(rule["key"])
        ratio, ok = _ratio(rule["op"], value, threshold)
        score += ratio * rule["weight"]
        checks.append({
            "key": rule["key"],
            "label": rule["label"],
            "value": value,
            "op": rule["op"],
            "threshold": round(threshold, 4),
            "default_threshold": rule["threshold"],
            "custom": override is not None,
            "unit": rule["unit"],
            "weight": rule["weight"],
            "rule": rule["rule"],
            "ok": ok,
            "points": round(ratio * rule["weight"], 1),
        })
    passed = sum(1 for c in checks if c["ok"])
    return {
        "id": profile["id"],
        "label": profile["label"],
        "score": round(score, 1),
        "rules_passed": passed,
        "rules_total": len(checks),
        "all_passed": passed == len(checks),
        "checks": checks,
    }
