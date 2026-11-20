"""Export a strategy spec as an MQL5 Expert Advisor.

The generated EA reads its symbol and timeframe from inputs (``InpSymbol``,
``InpTimeframe``) instead of baking them in, so the same compiled file can be
attached to any chart or driven by the strategy tester.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .indicators import INDICATORS

TF_ENUM = {
    "M1": "PERIOD_M1",
    "M5": "PERIOD_M5",
    "M15": "PERIOD_M15",
    "M30": "PERIOD_M30",
    "H1": "PERIOD_H1",
    "H4": "PERIOD_H4",
    "D1": "PERIOD_D1",
    "W1": "PERIOD_W1",
    "MN1": "PERIOD_MN1",
}


def _safe(name: str) -> str:
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    ident = "".join(out)
    if not ident or ident[0].isdigit():
        ident = "x_" + ident
    return ident


def _num(value: Any) -> str:
    try:
        return f"{float(value):.10g}"
    except (TypeError, ValueError):
        return "0"


def _params_list(cond: dict[str, Any]) -> str:
    ind = INDICATORS[cond["indicator"]]
    vals = []
    for p in ind.params:
        v = cond["params"].get(p.name, p.default)
        if p.choices:
            vals.append(f'"{v}"')
        elif p.integer:
            vals.append(str(int(round(float(v)))))
        else:
            vals.append(_num(v))
    return ", ".join(vals)


def _buffer_index(cond: dict[str, Any]) -> int:
    ind = INDICATORS[cond["indicator"]]
    for i, o in enumerate(ind.outputs):
        if o.key == cond["output"]:
            return i
    return 0


def _rhs(cond: dict[str, Any], shift: int) -> str:
    """Right hand side of a condition at the given bar shift (1 = last closed)."""
    kind = cond["operand"]["kind"]
    if kind == "price":
        return f"iClose(SYM(), InpTimeframe, {shift})"
    if kind == "level":
        return _num(cond["operand"]["value"])
    return "0"


def _magic(strategy_id: str) -> int:
    raw = hashlib.sha1(strategy_id.encode()).hexdigest()[:6]
    return int(raw, 16) % 90000000 + 1000000


def build_ea(spec: dict[str, Any], strategy_id: str = "sqx_strategy",
             indicator_dir: str = "") -> str:
    """Render an EA. ``indicator_dir`` is the folder under MQL5/Indicators
    that holds the exported SQX indicators (empty when they sit in the root)."""
    plan_symbol = spec.get("symbol", "") or ""
    plan_tf = spec.get("timeframe", "H1") or "H1"
    direction = spec.get("direction", "both") or "both"
    entry = spec.get("entry", {}) or {}
    ex = spec.get("exit", {}) or {}
    session = spec.get("session", {}) or {}
    risk = spec.get("risk", {}) or {}

    long_conds = entry.get("long", []) or []
    short_conds = entry.get("short", []) or []
    exit_conds = ex.get("conditions", []) or []

    # One handle per (indicator, params) pair; one buffer array per output used.
    unique: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for c in long_conds + short_conds + exit_conds:
        key = (c["indicator"], tuple(sorted(c["params"].items())))
        if key not in seen:
            seen.add(key)
            unique.append(c)

    def key_index(c: dict[str, Any]) -> tuple[int, int]:
        key = (c["indicator"], tuple(sorted(c["params"].items())))
        for i, u in enumerate(unique):
            if (u["indicator"], tuple(sorted(u["params"].items()))) == key:
                return i, _buffer_index(c)
        return 0, 0

    buffer_keys: list[tuple[int, int]] = []
    for c in long_conds + short_conds + exit_conds:
        kb = key_index(c)
        if kb not in buffer_keys:
            buffer_keys.append(kb)

    def cond_expr(c: dict[str, Any]) -> str:
        ui, bi = key_index(c)
        arr = f"buf_{ui}_{bi}"
        op = c["operator"]
        if op == "is_true":
            return f"({arr}[1] > 0.5)"
        if c["operand"]["kind"] not in ("price", "level"):
            return "false"
        if op == "gt":
            return f"({arr}[1] > {_rhs(c, 1)})"
        if op in ("ge", "gte"):
            return f"({arr}[1] >= {_rhs(c, 1)})"
        if op == "lt":
            return f"({arr}[1] < {_rhs(c, 1)})"
        if op in ("le", "lte"):
            return f"({arr}[1] <= {_rhs(c, 1)})"
        if op == "cross_above":
            return f"({arr}[1] > {_rhs(c, 1)} && {arr}[2] <= {_rhs(c, 2)})"
        if op == "cross_below":
            return f"({arr}[1] < {_rhs(c, 1)} && {arr}[2] >= {_rhs(c, 2)})"
        return "false"

    def run_expr(conds: list[dict[str, Any]]) -> str:
        return " && ".join(cond_expr(c) for c in conds) if conds else "true"

    long_expr = run_expr(long_conds)
    short_expr = run_expr(short_conds)
    exit_expr = run_expr(exit_conds)

    sl = ex.get("stop_loss", {}) or {}
    tp = ex.get("take_profit", {}) or {}
    tr = ex.get("trailing", {}) or {}
    max_bars = int(ex.get("max_bars_in_trade", 0) or 0)
    close_session = bool(session.get("close_at_end", True))
    start_hour = int(session.get("start_hour", 0))
    end_hour = int(session.get("end_hour", 23))
    days = session.get("days", [0, 1, 2, 3, 4]) or []

    # ATR periods actually needed by SL, TP and trailing.
    atr_periods: set[int] = set()
    for pol, on in ((sl, sl.get("type") == "atr"),
                    (tp, tp.get("type") == "atr"),
                    (tr, tr.get("mode") == "use")):
        if on:
            atr_periods.add(int(pol.get("atr_period", 14) or 14))

    def policy_dist(pol: dict[str, Any]) -> str:
        if pol.get("mode") in (None, "none"):
            return "0"
        if pol.get("type") == "fixed":
            return f"(({_num(pol.get('pips', 0))}) * 10.0 * _Point)"
        if pol.get("type") == "atr":
            period = int(pol.get("atr_period", 14) or 14)
            return f"({_num(pol.get('atr_mult', 1))} * atr_{period})"
        return "0"

    sl_expr = policy_dist(sl)
    tp_expr = policy_dist(tp)
    trailing_on = "true" if tr.get("mode") == "use" else "false"
    trailing_mult = _num(tr.get("atr_mult", 1))
    trailing_period = int(tr.get("atr_period", 14) or 14)

    if start_hour <= end_hour:
        session_check = f"(hour >= {start_hour} && hour <= {end_hour})"
    else:
        session_check = f"(hour >= {start_hour} || hour <= {end_hour})"
    if days:
        # SQX day 0 = Monday; MqlDateTime.day_of_week 0 = Sunday.
        mql_days = sorted({(int(d) + 1) % 7 for d in days})
        day_check = "(" + " || ".join(f"dt.day_of_week == {d}" for d in mql_days) + ")"
    else:
        day_check = "true"

    reverse = (ex.get("opposite_signal") or "ignore") == "reverse"

    prefix = indicator_dir.strip().strip("\\/")
    fixed_lot = _num(risk.get("fixed_lot", 0.1) or 0.1)

    L: list[str] = []
    add = L.append

    add("//+------------------------------------------------------------------+")
    add(f"//| {strategy_id} - SQX exported Expert Advisor (MQL5)")
    add(f"//| sampled on {plan_symbol or 'any symbol'} {plan_tf}")
    add("//+------------------------------------------------------------------+")
    add('#property copyright "SQX Strategy Lab"')
    add('#property version   "1.10"')
    add("#include <Trade\\Trade.mqh>")
    add("")
    add("CTrade trade;")
    add("")
    add('input string          InpSymbol    = "";              // Symbol (empty = chart symbol)')
    add("input ENUM_TIMEFRAMES InpTimeframe = PERIOD_CURRENT;  // Signal timeframe")
    add(f"input double          InpLots      = {fixed_lot};      // Fixed lot size")
    add(f"input int             InpMagic     = {_magic(strategy_id)};  // Magic number")
    add("input ulong           InpDeviation = 30;               // Max deviation (points)")
    add("")
    add('string SYM() { return (InpSymbol == "" ? _Symbol : InpSymbol); }')
    add("")
    for i, c in enumerate(unique):
        add(f"int h_{i} = INVALID_HANDLE;  // {c['indicator']}")
    for period in sorted(atr_periods):
        add(f"int h_atr_{period} = INVALID_HANDLE;")
    add("")
    for ui, bi in buffer_keys:
        add(f"double buf_{ui}_{bi}[3];")
    add("")
    add("int OnInit()")
    add("{")
    for i, c in enumerate(unique):
        resource = f"{prefix}\\{c['indicator']}" if prefix else c["indicator"]
        resource = resource.replace("\\", "\\\\")
        params = _params_list(c)
        args = f", {params}" if params else ""
        add(f'   h_{i} = iCustom(SYM(), InpTimeframe, "{resource}"{args});')
        add(f'   if(h_{i} == INVALID_HANDLE) Print("SQX: could not attach {c["indicator"]}");')
    for period in sorted(atr_periods):
        add(f"   h_atr_{period} = iATR(SYM(), InpTimeframe, {period});")
    add("   trade.SetExpertMagicNumber(InpMagic);")
    add("   trade.SetDeviationInPoints(InpDeviation);")
    add("   return INIT_SUCCEEDED;")
    add("}")
    add("")
    add("void OnDeinit(const int reason)")
    add("{")
    add("}")
    add("")
    add("void OnTick()")
    add("{")
    add("   static datetime lastBar = 0;")
    add("   datetime curBar = iTime(SYM(), InpTimeframe, 0);")
    add("   if(curBar == 0) return;")
    add("   if(curBar == lastBar) return;")
    add("   lastBar = curBar;")
    add("")
    for ui, bi in buffer_keys:
        add(f"   if(CopyBuffer(h_{ui}, {bi}, 0, 3, buf_{ui}_{bi}) <= 0) return;")
    add("")
    for period in sorted(atr_periods):
        add(f"   double atr_{period} = 0.0;")
        add(f"   if(h_atr_{period} != INVALID_HANDLE)")
        add("   {")
        add(f"      double ab_{period}[];")
        add(f"      if(CopyBuffer(h_atr_{period}, 0, 1, 1, ab_{period}) > 0) atr_{period} = ab_{period}[0];")
        add("   }")
    add("")
    add("   MqlDateTime dt;")
    add("   TimeToStruct(TimeCurrent(), dt);")
    add("   int hour = dt.hour;")
    add(f"   bool sessionOk = {session_check};")
    add(f"   bool dayOk = {day_check};")
    add("")
    add(f"   bool longSignal = sessionOk && dayOk && ({long_expr});")
    add(f"   bool shortSignal = sessionOk && dayOk && ({short_expr});")
    add(f"   bool exitSignal = ({exit_expr});")
    add("")
    add("   if(PositionSelect(SYM()))")
    add("   {")
    add("      long posType = (long)PositionGetInteger(POSITION_TYPE);")
    add("      datetime entryTime = (datetime)PositionGetInteger(POSITION_TIME);")
    add("      int barsInTrade = iBarShift(SYM(), InpTimeframe, entryTime, false);")
    add("")
    add(f"      if({trailing_on})")
    add("      {")
    add(f"         double trailDist = {trailing_mult} * atr_{trailing_period};")
    add("         if(trailDist > 0.0)")
    add("         {")
    add("            double posSL = PositionGetDouble(POSITION_SL);")
    add("            double posTP = PositionGetDouble(POSITION_TP);")
    add("            if(posType == POSITION_TYPE_BUY)")
    add("            {")
    add("               double bid = SymbolInfoDouble(SYM(), SYMBOL_BID);")
    add("               double newSL = bid - trailDist;")
    add("               if(newSL > posSL && newSL < bid) trade.PositionModify(SYM(), newSL, posTP);")
    add("            }")
    add("            else")
    add("            {")
    add("               double ask = SymbolInfoDouble(SYM(), SYMBOL_ASK);")
    add("               double newSL = ask + trailDist;")
    add("               if((posSL == 0.0 || newSL < posSL) && newSL > ask) trade.PositionModify(SYM(), newSL, posTP);")
    add("            }")
    add("         }")
    add("      }")
    add("")
    add(f"      if({max_bars} > 0 && barsInTrade >= {max_bars})")
    add("      {")
    add("         trade.PositionClose(SYM());")
    add("         return;")
    add("      }")
    if close_session:
        add(f"      if(hour == {end_hour})")
        add("      {")
        add("         trade.PositionClose(SYM());")
        add("         return;")
        add("      }")
    if reverse:
        add("      if(posType == POSITION_TYPE_BUY && shortSignal)")
        add("      {")
        add("         trade.PositionClose(SYM());")
        add("         longSignal = false;")
        add("      }")
        add("      if(posType == POSITION_TYPE_SELL && longSignal)")
        add("      {")
        add("         trade.PositionClose(SYM());")
        add("         shortSignal = false;")
        add("      }")
    add("      if(exitSignal)")
    add("      {")
    add("         trade.PositionClose(SYM());")
    add("         return;")
    add("      }")
    add("   }")
    add("")
    add("   if(!PositionSelect(SYM()))")
    add("   {")
    add(f"      double slDist = {sl_expr};")
    add(f"      double tpDist = {tp_expr};")
    allow_long = "true" if direction in ("long", "both") else "false"
    allow_short = "true" if direction in ("short", "both") else "false"
    add(f"      if({allow_long} && longSignal)")
    add("      {")
    add("         double ask = SymbolInfoDouble(SYM(), SYMBOL_ASK);")
    add("         double sl = (slDist > 0.0 ? ask - slDist : 0.0);")
    add("         double tp = (tpDist > 0.0 ? ask + tpDist : 0.0);")
    add('         trade.Buy(InpLots, SYM(), ask, sl, tp, "SQX");')
    add("      }")
    add(f"      else if({allow_short} && shortSignal)")
    add("      {")
    add("         double bid = SymbolInfoDouble(SYM(), SYMBOL_BID);")
    add("         double sl = (slDist > 0.0 ? bid + slDist : 0.0);")
    add("         double tp = (tpDist > 0.0 ? bid - tpDist : 0.0);")
    add('         trade.Sell(InpLots, SYM(), bid, sl, tp, "SQX");')
    add("      }")
    add("   }")
    add("}")
    add("")
    return "\n".join(L)
