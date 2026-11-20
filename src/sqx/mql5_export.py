"""Export the built-in indicator catalogue as MQL5 indicator source files."""

from __future__ import annotations

import io
import zipfile

from .indicators import INDICATORS
from . import mql5_code


def _safe_ident(name: str) -> str:
    out = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    ident = "".join(out)
    if not ident or ident[0].isdigit():
        ident = "p_" + ident
    return ident


def _mql5_param(p) -> str:
    name = _safe_ident(p.name)
    if p.choices:
        return f'input string {name} = "{p.default}";  // {p.label}'
    if p.integer:
        default = int(round(float(p.default)))
        return f"input int {name} = {default};  // {p.label}"
    return f"input double {name} = {p.default};  // {p.label}"


def _code_for(ind) -> str:
    nbuf = len(ind.outputs)
    params = "\n".join(_mql5_param(p) for p in ind.params) or "// no parameters"
    buffers = "\n".join(f"double buf{i}[];" for i in range(nbuf))
    set_buffers = "\n".join(
        f"   SetIndexBuffer({i}, buf{i}, INDICATOR_DATA);\n"
        f"   PlotIndexSetDouble({i}, PLOT_EMPTY_VALUE, EMPTY_VALUE);"
        for i in range(nbuf)
    )
    props = "\n".join(
        f"#property indicator_label{i+1} \"{o.label}\"\n"
        f"#property indicator_type{i+1} DRAW_LINE\n"
        f"#property indicator_color{i+1} clrDodgerBlue\n"
        f"#property indicator_style{i+1} STYLE_SOLID\n"
        f"#property indicator_width{i+1} 1"
        for i, o in enumerate(ind.outputs)
    )
    init_extra = mql5_code.INIT_EXTRA.get(ind.id, "")
    calc = mql5_code.CALC.get(ind.id)
    if calc is None:
        calc = "\n".join(f"      buf{i}[i] = close[i];" for i in range(nbuf))
        calc = "   for (int i = 0; i < rates_total; i++)\n   {\n" + calc + "\n   }\n"
    else:
        calc = calc.strip("\n") + "\n"
    return f"""//+------------------------------------------------------------------+
//| {ind.label} - SQX exported indicator (MQL5)                      |
//+------------------------------------------------------------------+
#property copyright "SQX Strategy Lab"
#property link      "https://sqx.local"
#property version   "1.00"
#property indicator_chart_window
#property indicator_buffers {nbuf}
#property indicator_plots   {nbuf}
{props}

{params}

{buffers}
int sqx_handle = INVALID_HANDLE;

//+------------------------------------------------------------------+
int OnInit()
{{
{set_buffers}
   {init_extra}
   return INIT_SUCCEEDED;
}}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{{
   if (rates_total <= 0)
      return 0;

{calc}
   return rates_total;
}}
"""


def build_indicator_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for ind in INDICATORS.values():
            zf.writestr(f"SQX indicators/{ind.id}.mq5", _code_for(ind))
    return buf.getvalue()