from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .indicators import INDICATORS, catalogue, default_indicator_config

OPERATORS = ["gt", "lt", "ge", "le", "cross_above", "cross_below"]


class ParamRange(BaseModel):
    min: float
    max: float
    step: float = 1


class IndicatorSetting(BaseModel):
    enabled: bool = True
    params: dict[str, ParamRange] = Field(default_factory=dict)


class Policy(BaseModel):
    mode: Literal["always", "never", "may"] = "may"
    type: Literal["fixed", "atr"] = "atr"
    pips_min: float = 20
    pips_max: float = 80
    pips_step: float = 1
    atr_period_min: int = 7
    atr_period_max: int = 21
    atr_period_step: int = 2
    atr_mult_min: float = 1.5
    atr_mult_max: float = 4.0
    atr_mult_step: float = 0.25


class EntryOrders(BaseModel):
    """How entry signals are turned into fills.

    ``types`` is the pool the generator samples from.  ``market`` fills on the
    next M1 bar; ``limit`` and ``stop`` become pending orders at a sampled
    distance from the signal price.  When ``max_orders`` is greater than one,
    several pending orders are placed together and the first one to trigger
    wins (the rest are cancelled).
    """

    types: list[Literal["market", "limit", "stop"]] = Field(
        default_factory=lambda: ["market", "limit", "stop"])
    max_orders: int = 2
    distance_type: Literal["fixed", "atr"] = "atr"
    pips_min: float = 5.0
    pips_max: float = 40.0
    pips_step: float = 1.0
    atr_period_min: int = 7
    atr_period_max: int = 21
    atr_period_step: int = 2
    atr_mult_min: float = 0.25
    atr_mult_max: float = 1.5
    atr_mult_step: float = 0.25
    expiry_bars_min: int = 1
    expiry_bars_max: int = 60
    expiry_bars_step: int = 1

    @field_validator("types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        out = [t for t in ("market", "limit", "stop") if t in set(v)]
        return out or ["market"]

    @field_validator("max_orders")
    @classmethod
    def _max_orders(cls, v: int) -> int:
        return min(3, max(1, int(v)))


class Trailing(BaseModel):
    mode: Literal["none", "use"] = "none"
    atr_period_min: int = 7
    atr_period_max: int = 21
    atr_period_step: int = 2
    atr_mult_min: float = 1.5
    atr_mult_max: float = 4.0
    atr_mult_step: float = 0.25


class Risk(BaseModel):
    sizing: Literal["fixed_lot", "risk_percent"] = "fixed_lot"
    fixed_lot: float = 0.10
    risk_percent: float = 1.0
    leverage: float = 30.0
    balance: float = 10000.0
    spread_pips: float = 0.3
    commission_per_lot: float = 0.0
    slippage_pips: float = 0.0


class Session(BaseModel):
    start_hour: int = 0
    end_hour: int = 23
    close_at_end: bool = True
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        return sorted({int(d) for d in v if 0 <= int(d) <= 6})


class Gates(BaseModel):
    min_trades: int = 20
    min_sharpe: float = 0.3
    max_stagnation_days: int = 365
    min_return_dd: float = 0.5
    max_drawdown_pct: float = 100.0
    min_profit_factor: float = 0.0
    min_win_rate: float = 0.0


class Acceptance(BaseModel):
    is_gates: Gates = Field(default_factory=lambda: Gates(
        min_trades=20, min_sharpe=0.1, max_stagnation_days=1500, min_return_dd=0.2))
    oos_gates: Gates = Field(default_factory=lambda: Gates(
        min_trades=10, min_sharpe=0.05, max_stagnation_days=1700, min_return_dd=0.1))


class AutoCorrelation(BaseModel):
    enabled: bool = True
    threshold: float = 0.95
    method: Literal["pearson", "spearman"] = "pearson"


class GenerateConfig(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    timeframe: str = "H4"
    direction: Literal["long", "short", "both"] = "both"
    opposite_signal: Literal["ignore", "reverse", "ignore_or_reverse"] = "ignore_or_reverse"
    stop_loss: Policy = Field(default_factory=lambda: Policy(mode="may", type="atr"))
    take_profit: Policy = Field(default_factory=lambda: Policy(mode="may", type="fixed"))
    orders: EntryOrders = Field(default_factory=EntryOrders)
    trailing: Trailing = Field(default_factory=Trailing)
    max_entry_indicators: int = 3
    max_exit_indicators: int = 1
    indicators: dict[str, IndicatorSetting] = Field(default_factory=dict)
    session: Session = Field(default_factory=Session)
    risk: Risk = Field(default_factory=Risk)
    acceptance: Acceptance = Field(default_factory=Acceptance)
    autocorrelation: AutoCorrelation = Field(default_factory=AutoCorrelation)
    oos_pct: int = 40
    target_accepted: int = 200
    seed: int = 42

    @field_validator("oos_pct")
    @classmethod
    def _oos(cls, v: int) -> int:
        if not 5 <= v <= 90:
            raise ValueError("OOS percent must be between 5 and 90")
        return v


def default_config(symbols: list[str] | None = None) -> GenerateConfig:
    return GenerateConfig(
        symbols=symbols or [],
        indicators={k: IndicatorSetting(**v) for k, v in default_indicator_config().items()},
    )


# ------------------------------------------------------------------ helpers
def _grid(rng: random.Random, lo: float, hi: float, step: float, integer: bool = True):
    if hi < lo:
        lo, hi = hi, lo
    step = step if step and step > 0 else 1
    n = int(round((hi - lo) / step))
    value = lo + rng.randint(0, n) * step
    value = min(max(value, lo), hi)
    return int(round(value)) if integer else round(float(value), 6)


def sample_params(rng: random.Random, ind_id: str,
                  settings: dict[str, IndicatorSetting]) -> dict[str, Any]:
    ind = INDICATORS[ind_id]
    cfg = settings.get(ind_id)
    out: dict[str, float] = {}
    for p in ind.params:
        if p.choices:
            out[p.name] = rng.choice(p.choices)
            continue
        r = cfg.params.get(p.name) if cfg else None
        lo = r.min if r else p.min
        hi = r.max if r else p.max
        step = r.step if r else p.step
        out[p.name] = _grid(rng, lo, hi, step, p.integer)
    return out


def _condition(rng: random.Random, ind_id: str,
               settings: dict[str, IndicatorSetting]) -> dict[str, Any]:
    ind = INDICATORS[ind_id]
    out = rng.choice(ind.outputs)
    params = sample_params(rng, ind_id, settings)
    if out.kind == "bool":
        operator = "is_true"
        operand = {"kind": "none", "value": None}
    elif out.kind == "price":
        operator = rng.choice(OPERATORS)
        operand = {"kind": "price", "value": None}
    else:
        operator = rng.choice(OPERATORS)
        level = rng.choice(out.levels) if out.levels else 0
        operand = {"kind": "level", "value": level}
    return {
        "indicator": ind_id,
        "output": out.key,
        "operator": operator,
        "params": params,
        "operand": operand,
    }


def _enabled_rule_indicators(cfg: GenerateConfig) -> list[str]:
    ids = []
    for ind_id, setting in cfg.indicators.items():
        if setting.enabled and ind_id in INDICATORS:
            ids.append(ind_id)
    if not ids:
        raise ValueError("No indicators enabled")
    return ids


def _resolve_policy(rng: random.Random, policy: Policy, kind: str) -> dict[str, Any]:
    mode = policy.mode
    if mode == "may":
        mode = "always" if rng.random() < 0.5 else "never"
    if mode == "never":
        return {"mode": "none", "type": None}
    typ = policy.type
    if typ == "fixed":
        pips = _grid(rng, policy.pips_min, policy.pips_max, policy.pips_step, False)
        return {"mode": kind, "type": "fixed", "pips": pips}
    period = _grid(rng, policy.atr_period_min, policy.atr_period_max,
                   policy.atr_period_step, True)
    mult = _grid(rng, policy.atr_mult_min, policy.atr_mult_max,
                 policy.atr_mult_step, False)
    return {"mode": kind, "type": "atr", "atr_period": period, "atr_mult": mult}


def _resolve_orders(rng: random.Random, cfg: GenerateConfig) -> dict[str, Any]:
    policy = cfg.orders
    types = list(policy.types) or ["market"]
    if types == ["market"]:
        return {"orders": [{"type": "market"}],
                "expiry_bars": 0, "oco": True}

    max_n = min(3, policy.max_orders, len(types))
    n = rng.randint(1, max_n)
    chosen = rng.sample(types, n)
    if "market" in chosen and len(chosen) > 1:
        # A market fill always wins immediately, so pairing it with pending
        # orders would be pointless.  Fall back to the pending types.
        chosen = [t for t in chosen if t != "market"] or ["market"]

    orders: list[dict[str, Any]] = []
    for typ in chosen:
        if typ == "market":
            orders.append({"type": "market"})
            continue
        if policy.distance_type == "fixed":
            orders.append({
                "type": typ, "distance_type": "fixed",
                "pips": _grid(rng, policy.pips_min, policy.pips_max,
                              policy.pips_step, False),
            })
        else:
            orders.append({
                "type": typ, "distance_type": "atr",
                "atr_period": _grid(rng, policy.atr_period_min,
                                    policy.atr_period_max,
                                    policy.atr_period_step, True),
                "atr_mult": _grid(rng, policy.atr_mult_min,
                                  policy.atr_mult_max,
                                  policy.atr_mult_step, False),
            })
    expiry = _grid(rng, policy.expiry_bars_min, policy.expiry_bars_max,
                   policy.expiry_bars_step, True)
    if all(o["type"] == "market" for o in orders):
        expiry = 0
    return {"orders": orders, "expiry_bars": int(expiry), "oco": True}


def _resolve_trailing(rng: random.Random, cfg: GenerateConfig) -> dict[str, Any]:
    if cfg.trailing.mode == "none":
        return {"mode": "none"}
    t = cfg.trailing
    return {
        "mode": "use",
        "type": "atr",
        "atr_period": _grid(rng, t.atr_period_min, t.atr_period_max, t.atr_period_step, True),
        "atr_mult": _grid(rng, t.atr_mult_min, t.atr_mult_max, t.atr_mult_step, False),
    }


def sample_strategy(rng: random.Random, cfg: GenerateConfig,
                    symbol: str | None = None) -> dict[str, Any]:
    pool = _enabled_rule_indicators(cfg)
    sym = symbol or rng.choice(cfg.symbols)
    direction = cfg.direction
    entry: dict[str, Any] = {"long": [], "short": []}
    sides = ["long", "short"] if direction == "both" else [direction]
    for side in sides:
        hi = max(1, cfg.max_entry_indicators)
        lo = max(1, hi - 1) if hi >= 2 else 1
        n = rng.randint(lo, hi)
        used: set[tuple] = set()
        for _ in range(n):
            for _try in range(12):
                ind_id = rng.choice(pool)
                c = _condition(rng, ind_id, cfg.indicators)
                key = (c["indicator"], c["output"], c["operator"],
                       c["operand"]["value"])
                if key not in used:
                    used.add(key)
                    entry[side].append(c)
                    break
    n_exit = rng.randint(0, max(0, cfg.max_exit_indicators))
    exits = []
    used: set[tuple] = set()
    for _ in range(n_exit):
        for _try in range(12):
            ind_id = rng.choice(pool)
            c = _condition(rng, ind_id, cfg.indicators)
            key = (c["indicator"], c["output"], c["operator"], c["operand"]["value"])
            if key not in used:
                used.add(key)
                exits.append(c)
                break

    opposite = cfg.opposite_signal
    if opposite == "ignore_or_reverse":
        opposite = "reverse" if rng.random() < 0.5 else "ignore"

    sl = _resolve_policy(rng, cfg.stop_loss, "stop_loss")
    tp = _resolve_policy(rng, cfg.take_profit, "take_profit")
    orders = _resolve_orders(rng, cfg)
    trailing = _resolve_trailing(rng, cfg)

    if (sl["mode"] == "none" and tp["mode"] == "none" and trailing["mode"] == "none"
            and not exits and opposite == "ignore"):
        # guarantee an exit path
        sl = {"mode": "stop_loss", "type": "atr",
              "atr_period": 14, "atr_mult": 3.0}

    spec = {
        "schema": 1,
        "symbol": sym,
        "timeframe": cfg.timeframe,
        "direction": direction,
        "entry": entry,
        "execution": orders,
        "exit": {
            "conditions": exits,
            "opposite_signal": opposite,
            "stop_loss": sl,
            "take_profit": tp,
            "trailing": trailing,
            "max_bars_in_trade": rng.choice([60, 120, 240, 480]),
            "cooldown_bars": rng.choice([0, 24, 96, 240, 480]),
            "close_at_session_end": cfg.session.close_at_end,
        },
        "session": cfg.session.model_dump(),
        "risk": cfg.risk.model_dump(),
        "meta": {"seed_source": "run", "generator": "1.0.0"},
    }
    return spec


def spec_hash(spec: dict[str, Any]) -> str:
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


OP_LABEL = {"gt": ">", "lt": "<", "ge": ">=", "le": "<=",
            "gte": ">=", "lte": "<=",
            "cross_above": "crosses above",
            "cross_below": "crosses below", "is_true": "is true"}


def condition_text(c: dict[str, Any]) -> str:
    ind = INDICATORS[c["indicator"]]
    out = next(o for o in ind.outputs if o.key == c["output"])
    ps = ", ".join(str(c["params"][p.name]) for p in ind.params)
    name = f"{ind.label}({ps})" if ps else ind.label
    if len(ind.outputs) > 1:
        name += f".{out.label}"
    if c["operator"] == "is_true":
        return f"{name} is true"
    operand = c["operand"]
    if operand["kind"] == "price":
        rhs = "close"
    elif operand["kind"] == "level":
        rhs = str(operand["value"])
    else:
        rhs = ""
    return f"{name} {OP_LABEL[c['operator']]} {rhs}".strip()


def describe(spec: dict[str, Any]) -> dict[str, Any]:
    e = spec["exit"]
    sl, tp, tr = e["stop_loss"], e["take_profit"], e["trailing"]
    def pol(p, label):
        if p.get("mode") in (None, "none"):
            return f"{label}: none"
        if p["type"] == "fixed":
            return f"{label}: {p['pips']} pips"
        return f"{label}: {p['atr_mult']} x ATR({p['atr_period']})"
    execution = spec.get("execution") or {}
    order_parts = []
    for o in execution.get("orders", []) or [{"type": "market"}]:
        typ = o.get("type", "market")
        if typ == "market":
            order_parts.append("Market")
            continue
        if o.get("distance_type") == "fixed":
            dist = f"{o.get('pips', 0)} pips"
        else:
            dist = f"{o.get('atr_mult', 0)} x ATR({o.get('atr_period', 14)})"
        order_parts.append(f"{typ.capitalize()} {dist}")
    orders = ", ".join(order_parts) if order_parts else "Market"
    if len(order_parts) > 1:
        orders += " (OCO)"
    if int(execution.get("expiry_bars", 0) or 0) > 0:
        orders += f", expiry {int(execution['expiry_bars'])} bars"
    return {
        "long": [condition_text(c) for c in spec["entry"].get("long", [])],
        "short": [condition_text(c) for c in spec["entry"].get("short", [])],
        "exits": [condition_text(c) for c in e["conditions"]],
        "orders": orders,
        "stop_loss": pol(sl, "SL"),
        "take_profit": pol(tp, "TP"),
        "trailing": "Trailing: none" if tr.get("mode") == "none"
                    else f"Trailing: {tr['atr_mult']} x ATR({tr['atr_period']})",
        "opposite_signal": e["opposite_signal"],
        "max_bars": e["max_bars_in_trade"],
    }
