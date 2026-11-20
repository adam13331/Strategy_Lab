"""MQL5 calculation snippets for exported indicators."""

from __future__ import annotations

# init_extra: MQL5 statements inserted into OnInit() (handle creation etc.)
INIT_EXTRA: dict[str, str] = {
    "sma": "sqx_handle = iMA(_Symbol, _Period, (int)period, 0, MODE_SMA, PRICE_CLOSE);",
    "ema": "sqx_handle = iMA(_Symbol, _Period, (int)period, 0, MODE_EMA, PRICE_CLOSE);",
    "wma": "sqx_handle = iMA(_Symbol, _Period, (int)period, 0, MODE_LWMA, PRICE_CLOSE);",
    "rsi": "sqx_handle = iRSI(_Symbol, _Period, (int)period, PRICE_CLOSE);",
    "macd": "sqx_handle = iMACD(_Symbol, _Period, (int)fast, (int)slow, (int)signal, PRICE_CLOSE);",
    "adx": "sqx_handle = iADX(_Symbol, _Period, (int)period);",
    "stochastic": "sqx_handle = iStochastic(_Symbol, _Period, (int)k, (int)d, (int)smooth, MODE_SMA, STO_LOWHIGH);",
    "cci": "sqx_handle = iCCI(_Symbol, _Period, (int)period, PRICE_TYPICAL);",
    "williams_r": "sqx_handle = iWPR(_Symbol, _Period, (int)period);",
    "mfi": "sqx_handle = iMFI(_Symbol, _Period, (int)period, VOLUME_TICK);",
    "atr": "sqx_handle = iATR(_Symbol, _Period, (int)period);",
    "bbands": "sqx_handle = iBands(_Symbol, _Period, (int)period, std, 0, PRICE_CLOSE);",
    "psar": "sqx_handle = iSAR(_Symbol, _Period, step, max_step);",
    "ichimoku": "sqx_handle = iIchimoku(_Symbol, _Period, (int)tenkan, (int)kijun, (int)senkou);",
    "alligator": "sqx_handle = iAlligator(_Symbol, _Period, (int)jaw, 0, (int)teeth, 0, (int)lips, 0, MODE_EMA, PRICE_MEDIAN);",
    "obv": "sqx_handle = iOBV(_Symbol, _Period, VOLUME_TICK);",
    "ad": "sqx_handle = iAD(_Symbol, _Period, VOLUME_TICK);",
    "force": "sqx_handle = iForce(_Symbol, _Period, (int)period, MODE_EMA, VOLUME_TICK);",
    "supertrend": "sqx_handle = iATR(_Symbol, _Period, (int)period);",
}

CALC: dict[str, str] = {}

# ---- handle based simple indicators ----
CALC["sma"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
if(CopyBuffer(sqx_handle, 0, 0, rates_total, buf0) <= 0) return 0;
"""
CALC["ema"] = CALC["sma"]
CALC["wma"] = CALC["sma"]
CALC["rsi"] = CALC["sma"]
CALC["cci"] = CALC["sma"]
CALC["williams_r"] = CALC["sma"]
CALC["mfi"] = CALC["sma"]
CALC["psar"] = CALC["sma"]
CALC["force"] = CALC["sma"]

CALC["macd"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double b0[], b1[], b2[];
ArrayResize(b0, rates_total); ArrayResize(b1, rates_total); ArrayResize(b2, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, b0) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, b1) <= 0) return 0;
if(CopyBuffer(sqx_handle, 2, 0, rates_total, b2) <= 0) return 0;
for(int i = 0; i < rates_total; i++) { buf0[i] = b0[i]; buf1[i] = b1[i]; buf2[i] = b2[i]; }
"""

CALC["adx"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double b0[], b1[], b2[];
ArrayResize(b0, rates_total); ArrayResize(b1, rates_total); ArrayResize(b2, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, b0) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, b1) <= 0) return 0;
if(CopyBuffer(sqx_handle, 2, 0, rates_total, b2) <= 0) return 0;
for(int i = 0; i < rates_total; i++) { buf0[i] = b0[i]; buf1[i] = b1[i]; buf2[i] = b2[i]; }
"""

CALC["stochastic"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double b0[], b1[];
ArrayResize(b0, rates_total); ArrayResize(b1, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, b0) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, b1) <= 0) return 0;
for(int i = 0; i < rates_total; i++) { buf0[i] = b0[i]; buf1[i] = b1[i]; }
"""

CALC["ichimoku"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double b0[], b1[], b2[], b3[];
ArrayResize(b0, rates_total); ArrayResize(b1, rates_total); ArrayResize(b2, rates_total); ArrayResize(b3, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, b0) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, b1) <= 0) return 0;
if(CopyBuffer(sqx_handle, 2, 0, rates_total, b2) <= 0) return 0;
if(CopyBuffer(sqx_handle, 3, 0, rates_total, b3) <= 0) return 0;
for(int i = 0; i < rates_total; i++) { buf0[i] = b0[i]; buf1[i] = b1[i]; buf2[i] = b2[i]; buf3[i] = b3[i]; }
"""

CALC["alligator"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double b0[], b1[], b2[];
ArrayResize(b0, rates_total); ArrayResize(b1, rates_total); ArrayResize(b2, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, b0) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, b1) <= 0) return 0;
if(CopyBuffer(sqx_handle, 2, 0, rates_total, b2) <= 0) return 0;
for(int i = 0; i < rates_total; i++) { buf0[i] = b0[i]; buf1[i] = b1[i]; buf2[i] = b2[i]; }
"""

CALC["bbands"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double mid[], up[], lo[];
ArrayResize(mid, rates_total); ArrayResize(up, rates_total); ArrayResize(lo, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, mid) <= 0) return 0;
if(CopyBuffer(sqx_handle, 1, 0, rates_total, up) <= 0) return 0;
if(CopyBuffer(sqx_handle, 2, 0, rates_total, lo) <= 0) return 0;
for(int i = 0; i < rates_total; i++) {
   buf0[i] = up[i];
   buf1[i] = mid[i];
   buf2[i] = lo[i];
   double denom = up[i] - lo[i];
   if(denom == 0) buf3[i] = EMPTY_VALUE; else buf3[i] = (close[i] - lo[i]) / denom;
   double bw = (mid[i] == 0) ? EMPTY_VALUE : (100.0 * denom / mid[i]);
   buf4[i] = bw;
}
"""

CALC["atr"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double a[];
ArrayResize(a, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, a) <= 0) return 0;
for(int i = 0; i < rates_total; i++) {
   if(close[i] == 0) { buf0[i] = EMPTY_VALUE; buf1[i] = EMPTY_VALUE; continue; }
   buf1[i] = 100.0 * a[i] / close[i];
   int n = MathMin(100, i + 1);
   double vals[];
   ArrayResize(vals, n);
   for(int j = 0; j < n; j++) vals[j] = a[i - n + 1 + j];
   ArraySort(vals);
   double med = (n % 2 == 1) ? vals[n / 2] : (vals[n / 2 - 1] + vals[n / 2]) / 2.0;
   if(med == 0) buf0[i] = EMPTY_VALUE; else buf0[i] = a[i] / med;
}
"""

CALC["obv"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double obv[];
ArrayResize(obv, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, obv) <= 0) return 0;
int period = (int)period;
for(int i = 0; i < rates_total; i++) {
   if(i < period) { buf0[i] = EMPTY_VALUE; continue; }
   double volsum = 0;
   for(int j = 0; j < period; j++) volsum += (double)volume[i - j];
   if(volsum == 0) buf0[i] = EMPTY_VALUE; else buf0[i] = (obv[i] - obv[i - period]) / volsum;
}
"""

CALC["ad"] = CALC["obv"]

CALC["supertrend"] = """
if(sqx_handle == INVALID_HANDLE) return 0;
double a[];
ArrayResize(a, rates_total);
if(CopyBuffer(sqx_handle, 0, 0, rates_total, a) <= 0) return 0;
double mult = (double)mult;
double fup = 0, flo = 0; bool have=false;
for(int i = 0; i < rates_total; i++) {
   if(!MathIsValidNumber(a[i])) { buf0[i] = 0; buf1[i] = EMPTY_VALUE; continue; }
   double hl2 = (high[i] + low[i]) / 2.0;
   double upper = hl2 + mult * a[i];
   double lower = hl2 - mult * a[i];
   if(!have) { fup = upper; flo = lower; have = true; }
   else {
      if(upper < fup || close[i-1] > fup) fup = upper; else fup = fup;
      if(lower > flo || close[i-1] < flo) flo = lower; else flo = flo;
   }
   // trend
   static int trend = 1;
   if(i == 0) trend = 1;
   else {
      if(trend == 1) { if(close[i] < flo) trend = -1; }
      else { if(close[i] > fup) trend = 1; }
   }
   buf0[i] = (trend == 1) ? 1.0 : -1.0;
   buf1[i] = (trend == 1) ? flo : fup;
}
"""

# ---- manual indicators ----
CALC["vwma"] = """
int p = (int)period;
for(int i = 0; i < rates_total; i++) {
   if(i < p - 1) { buf0[i] = EMPTY_VALUE; continue; }
   double pv = 0, vv = 0;
   for(int j = 0; j < p; j++) { pv += close[i - j] * (double)volume[i - j]; vv += (double)volume[i - j]; }
   if(vv == 0) buf0[i] = EMPTY_VALUE; else buf0[i] = pv / vv;
}
"""

CALC["hma"] = """
int p = (int)period;
int half = MathMax(2, p / 2);
int root = MathMax(2, (int)MathRound(MathSqrt((double)p)));
double wHalf[], wFull[], tmp[];
ArrayResize(wHalf, rates_total); ArrayResize(wFull, rates_total); ArrayResize(tmp, rates_total);
for(int i = 0; i < rates_total; i++) {
   if(i < half - 1) { wHalf[i] = EMPTY_VALUE; wFull[i] = EMPTY_VALUE; tmp[i] = EMPTY_VALUE; continue; }
   double s = 0, w = 0;
   for(int j = 0; j < half; j++) { double weight = half - j; s += close[i - j] * weight; w += weight; }
   wHalf[i] = s / w;
   s = 0; w = 0;
   for(int j = 0; j < p; j++) { double weight = p - j; s += close[i - j] * weight; w += weight; }
   wFull[i] = s / w;
   if(wHalf[i] == EMPTY_VALUE || wFull[i] == EMPTY_VALUE) tmp[i] = EMPTY_VALUE; else tmp[i] = 2.0 * wHalf[i] - wFull[i];
}
for(int i = 0; i < rates_total; i++) {
   if(i < half + root - 2) { buf0[i] = EMPTY_VALUE; continue; }
   double s = 0, w = 0; bool ok = true;
   for(int j = 0; j < root; j++) {
      if(tmp[i - j] == EMPTY_VALUE) { ok = false; break; }
      double weight = root - j; s += tmp[i - j] * weight; w += weight;
   }
   if(!ok || w == 0) buf0[i] = EMPTY_VALUE; else buf0[i] = s / w;
}
"""

CALC["dema"] = """
int p = (int)period;
double a = 2.0 / (p + 1.0);
double e1[], e2[];
ArrayResize(e1, rates_total); ArrayResize(e2, rates_total);
double sum = 0;
for(int j = 0; j < p && j < rates_total; j++) sum += close[j];
double ema = (rates_total >= p) ? sum / p : 0;
for(int i = 0; i < rates_total; i++) {
   if(i < p - 1) { e1[i] = EMPTY_VALUE; continue; }
   if(i == p - 1) e1[i] = ema; else { ema = a * close[i] + (1 - a) * ema; e1[i] = ema; }
}
// ema2
double ema2 = 0; bool have2 = false;
if(rates_total >= 2 * p - 1) {
   double s2 = 0;
   for(int j = 0; j < p; j++) s2 += e1[p - 1 + j];
   ema2 = s2 / p; have2 = true;
}
for(int i = 0; i < rates_total; i++) {
   if(i < 2 * p - 2) { e2[i] = EMPTY_VALUE; continue; }
   if(i == 2 * p - 2 && have2) e2[i] = ema2;
   else if(i > 2 * p - 2) { ema2 = a * e1[i] + (1 - a) * ema2; e2[i] = ema2; }
}
for(int i = 0; i < rates_total; i++) {
   if(e1[i] == EMPTY_VALUE || e2[i] == EMPTY_VALUE) buf0[i] = EMPTY_VALUE; else buf0[i] = 2.0 * e1[i] - e2[i];
}
"""

CALC["tema"] = """
int p = (int)period;
double a = 2.0 / (p + 1.0);
double e1[], e2[], e3[];
ArrayResize(e1, rates_total); ArrayResize(e2, rates_total); ArrayResize(e3, rates_total);
double sum = 0;
for(int j = 0; j < p && j < rates_total; j++) sum += close[j];
double ema = (rates_total >= p) ? sum / p : 0;
for(int i = 0; i < rates_total; i++) {
   if(i < p - 1) { e1[i] = EMPTY_VALUE; continue; }
   if(i == p - 1) e1[i] = ema; else { ema = a * close[i] + (1 - a) * ema; e1[i] = ema; }
}
// e2 = EMA(e1)
double ema2 = 0; bool have2 = false;
if(rates_total >= 2*p-1) { double s2=0; for(int j=0;j<p;j++) s2+=e1[p-1+j]; ema2=s2/p; have2=true; }
for(int i=0;i<rates_total;i++){
   if(i < 2*p-2){e2[i]=EMPTY_VALUE;continue;}
   if(i==2*p-2 && have2) e2[i]=ema2;
   else if(i>2*p-2){ ema2 = a*e1[i]+(1-a)*ema2; e2[i]=ema2;}
}
// e3 = EMA(e2)
double ema3=0; bool have3=false;
if(rates_total >= 3*p-3) { double s3=0; for(int j=0;j<p;j++) s3+=e2[2*p-2+j]; ema3=s3/p; have3=true; }
for(int i=0;i<rates_total;i++){
   if(i < 3*p-3){e3[i]=EMPTY_VALUE;continue;}
   if(i==3*p-3 && have3) e3[i]=ema3;
   else if(i>3*p-3){ ema3 = a*e2[i]+(1-a)*ema3; e3[i]=ema3;}
}
for(int i=0;i<rates_total;i++){
   if(e1[i]==EMPTY_VALUE||e2[i]==EMPTY_VALUE||e3[i]==EMPTY_VALUE) buf0[i]=EMPTY_VALUE;
   else buf0[i]=3.0*e1[i]-3.0*e2[i]+e3[i];
}
"""

CALC["kama"] = """
int p = (int)period;
int fast = (int)fast;
int slow = (int)slow;
double fs = 2.0 / (fast + 1.0);
double ss = 2.0 / (slow + 1.0);
double c[]; ArrayCopy(c, close); // not allowed? ArrayCopy needs resize
"""
# ArrayCopy requires destination sized. Use manual.
CALC["kama"] = """
int p = (int)period;
int fast = (int)fast;
int slow = (int)slow;
double fs = 2.0 / (fast + 1.0);
double ss = 2.0 / (slow + 1.0);
for(int i = 0; i < rates_total; i++) buf0[i] = EMPTY_VALUE;
if(rates_total <= p) return 0;
// change and vol
double change[], vol[];
ArrayResize(change, rates_total); ArrayResize(vol, rates_total);
for(int i = 0; i < rates_total; i++) { change[i] = EMPTY_VALUE; vol[i] = EMPTY_VALUE; }
for(int i = p; i < rates_total; i++) {
   change[i] = MathAbs(close[i] - close[i - p]);
   double v = 0;
   for(int j = 1; j <= p; j++) v += MathAbs(close[i - j + 1] - close[i - j]);
   vol[i] = v;
}
double prev = close[p - 1];
buf0[p - 1] = prev;
for(int i = p; i < rates_total; i++) {
   double sc = (vol[i] == 0) ? 0 : change[i] / vol[i];
   double smooth = MathPow(sc * (fs - ss) + ss, 2);
   if(!MathIsValidNumber(smooth)) smooth = 0;
   prev = prev + smooth * (close[i] - prev);
   buf0[i] = prev;
}
"""

CALC["aroon"] = """
int p = (int)period;
for(int i = 0; i < rates_total; i++) { buf0[i] = EMPTY_VALUE; buf1[i] = EMPTY_VALUE; }
for(int i = p; i < rates_total; i++) {
   int hi = 0, lo = 0;
   double maxv = high[i - p], minv = low[i - p];
   for(int j = 1; j <= p; j++) {
      if(high[i - p + j] >= maxv) { maxv = high[i - p + j]; hi = j; }
      if(low[i - p + j] <= minv) { minv = low[i - p + j]; lo = j; }
   }
   buf0[i] = 100.0 * hi / p;
   buf1[i] = 100.0 * lo / p;
}
"""

CALC["vortex"] = """
int p = (int)period;
for(int i = 0; i < rates_total; i++) { buf0[i] = EMPTY_VALUE; buf1[i] = EMPTY_VALUE; }
double trs = 0, vp = 0, vm = 0;
for(int i = 1; i < rates_total; i++) {
   double tr = MathMax(high[i] - low[i], MathMax(MathAbs(high[i] - close[i-1]), MathAbs(low[i] - close[i-1])));
   double vpp = MathAbs(high[i] - low[i-1]);
   double vmm = MathAbs(low[i] - high[i-1]);
   // rolling sum over p
   // simple: accumulate last p
   if(i < p) continue;
   trs = 0; vp = 0; vm = 0;
   for(int j = 0; j < p; j++) {
      int idx = i - j;
      double trr = MathMax(high[idx] - low[idx], MathMax(MathAbs(high[idx] - close[idx-1]), MathAbs(low[idx] - close[idx-1])));
      trs += trr;
      vp += MathAbs(high[idx] - low[idx-1]);
      vm += MathAbs(low[idx] - high[idx-1]);
   }
   if(trs == 0) { buf0[i] = EMPTY_VALUE; buf1[i] = EMPTY_VALUE; }
   else { buf0[i] = vp / trs; buf1[i] = vm / trs; }
}
"""

CALC["stochrsi"] = """
// placeholder: Stochastic of RSI
for(int i=0;i<rates_total;i++){ buf0[i]=close[i]; buf1[i]=close[i]; }
"""
