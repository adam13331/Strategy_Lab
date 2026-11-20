"""Vectorized indicator library and catalogue.

All functions take an OHLCV DataFrame (columns: open, high, low, close, volume)
and return a dict of numpy arrays aligned to the input index. Warm-up values are
NaN; comparisons against NaN evaluate to False downstream.

Deliberate choices:
- no forward-shifted ichimoku clouds / alligator lines (would be look-ahead),
- Donchian/breakout levels use the previous N bars so a close can break them,
- volume/volatility outputs are exposed as *ratios* so one set of thresholds
  works across instruments and timeframes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd


@dataclass
class Param:
    name: str
    label: str
    default: float
    min: float
    max: float
    step: float = 1
    integer: bool = True
    choices: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "default": self.default,
            "min": self.min, "max": self.max, "step": self.step,
            "integer": self.integer, "choices": self.choices or [],
        }


@dataclass
class Output:
    key: str
    label: str
    kind: str  # level | price | bool
    levels: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "levels": self.levels}


@dataclass
class Indicator:
    id: str
    label: str
    category: str
    description: str
    params: list[Param]
    outputs: list[Output]
    warmup: int = 50
    needs_volume: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "category": self.category,
            "description": self.description,
            "params": [p.as_dict() for p in self.params],
            "outputs": [o.as_dict() for o in self.outputs],
            "warmup": self.warmup, "needs_volume": self.needs_volume,
        }


def P(name, label, default, lo, hi, step=1, integer=True,
      choices=None) -> Param:
    return Param(name, label, default, lo, hi, step, integer, choices)


def O(key, label, kind, levels=None) -> Output:
    return Output(key, label, kind, levels or [])


def _f(df: pd.DataFrame, col: str) -> pd.Series:
    return df[col].astype(float)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _arr(s: pd.Series) -> np.ndarray:
    return s.to_numpy(dtype=np.float64)


def _b(s: pd.Series) -> np.ndarray:
    return (s.fillna(0).to_numpy() > 0.5).astype(np.float64)


def _ratio(s: pd.Series, n: int = 100) -> pd.Series:
    base = s.rolling(n, min_periods=max(5, n // 2)).median()
    return s / base.replace(0, np.nan)


# --------------------------------------------------------------------- trend
def _sma(df, period=20, **_):
    return {"value": _arr(_f(df, "close").rolling(period).mean())}


def _ema_ind(df, period=20, **_):
    return {"value": _arr(_ema(_f(df, "close"), period))}


def _wma_ind(df, period=20, **_):
    return {"value": _arr(_wma(_f(df, "close"), period))}


def _hma(df, period=20, **_):
    half = max(2, period // 2)
    root = max(2, int(round(np.sqrt(period))))
    c = _f(df, "close")
    return {"value": _arr(_wma(2 * _wma(c, half) - _wma(c, period), root))}


def _dema(df, period=20, **_):
    e1 = _ema(_f(df, "close"), period)
    return {"value": _arr(2 * e1 - _ema(e1, period))}


def _tema(df, period=20, **_):
    e1 = _ema(_f(df, "close"), period)
    e2 = _ema(e1, period)
    e3 = _ema(e2, period)
    return {"value": _arr(3 * e1 - 3 * e2 + e3)}


def _kama(df, period=10, fast=2, slow=30, **_):
    c = _f(df, "close").to_numpy(dtype=float)
    n = int(period)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return {"value": out}
    change = np.abs(c[n:] - c[:-n])
    vol = pd.Series(np.abs(np.diff(c))).rolling(n).sum().to_numpy()
    # vol[j] corresponds to window ending at index j+n
    sc = (change / np.where(vol[n - 1:] == 0, np.nan, vol[n - 1:]))
    fs = 2 / (fast + 1)
    ss = 2 / (slow + 1)
    smoothing = (sc * (fs - ss) + ss) ** 2
    prev = c[n - 1]
    out[n - 1] = prev
    for i in range(n, len(c)):
        k = i - n
        s = smoothing[k] if k < len(smoothing) else np.nan
        if not np.isfinite(s):
            s = 0.0
        prev = prev + s * (c[i] - prev)
        out[i] = prev
    return {"value": out}


def _vwma(df, period=20, **_):
    c, v = _f(df, "close"), _f(df, "volume")
    return {"value": _arr((c * v).rolling(period).sum() / v.rolling(period).sum())}


def _macd(df, fast=12, slow=26, signal=9, **_):
    c = _f(df, "close")
    line = _ema(c, fast) - _ema(c, slow)
    sig = _ema(line, signal)
    return {"macd": _arr(line), "signal": _arr(sig), "hist": _arr(line - sig)}


def _adx(df, period=14, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return {"adx": _arr(adx), "plus_di": _arr(pdi), "minus_di": _arr(mdi)}


def _aroon(df, period=14, **_):
    h, l = _f(df, "high"), _f(df, "low")
    n = int(period)
    up = 100 * h.rolling(n + 1).apply(lambda x: float(np.argmax(x)) / n, raw=True)
    dn = 100 * l.rolling(n + 1).apply(lambda x: float(np.argmin(x)) / n, raw=True)
    return {"aroon_up": _arr(up), "aroon_down": _arr(dn)}


def _supertrend(df, period=10, mult=3.0, **_):
    h, l, c = _f(df, "high").to_numpy(), _f(df, "low").to_numpy(), _f(df, "close").to_numpy()
    atr = _atr(df, int(period)).to_numpy()
    hl2 = (h + l) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    n = len(c)
    fup = np.full(n, np.nan)
    flo = np.full(n, np.nan)
    trend = np.zeros(n)
    line = np.full(n, np.nan)
    for i in range(1, n):
        if not np.isfinite(atr[i]):
            continue
        fup[i] = upper[i] if (upper[i] < fup[i - 1] or c[i - 1] > fup[i - 1] or not np.isfinite(fup[i - 1])) else fup[i - 1]
        flo[i] = lower[i] if (lower[i] > flo[i - 1] or c[i - 1] < flo[i - 1] or not np.isfinite(flo[i - 1])) else flo[i - 1]
        if not np.isfinite(fup[i - 1]):
            trend[i] = 1.0
        elif trend[i - 1] == 1.0:
            trend[i] = -1.0 if c[i] < flo[i] else 1.0
        else:
            trend[i] = 1.0 if c[i] > fup[i] else -1.0
        line[i] = flo[i] if trend[i] == 1.0 else fup[i]
    return {"super_dir": trend, "super_line": line}


def _psar(df, step=0.02, max_step=0.2, **_):
    h, l = _f(df, "high").to_numpy(), _f(df, "low").to_numpy()
    n = len(h)
    out = np.full(n, np.nan)
    if n < 2:
        return {"psar": out}
    bull = True
    af = step
    ep = h[0]
    sar = l[0]
    out[0] = sar
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if bull:
            if l[i] < sar:
                bull, sar, ep, af = False, ep, l[i], step
            else:
                if h[i] > ep:
                    ep, af = h[i], min(af + step, max_step)
        else:
            if h[i] > sar:
                bull, sar, ep, af = True, ep, h[i], step
            else:
                if l[i] < ep:
                    ep, af = l[i], min(af + step, max_step)
        out[i] = sar
    return {"psar": out}


def _ichimoku(df, tenkan=9, kijun=26, senkou=52, **_):
    h, l = _f(df, "high"), _f(df, "low")
    t = (h.rolling(tenkan).max() + l.rolling(tenkan).min()) / 2
    k = (h.rolling(kijun).max() + l.rolling(kijun).min()) / 2
    a = (t + k) / 2
    b = (h.rolling(senkou).max() + l.rolling(senkou).min()) / 2
    return {"tenkan": _arr(t), "kijun": _arr(k), "senkou_a": _arr(a), "senkou_b": _arr(b)}


def _alligator(df, jaw=13, teeth=8, lips=5, **_):
    hl2 = (_f(df, "high") + _f(df, "low")) / 2
    sm = lambda n: hl2.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return {"jaw": _arr(sm(jaw)), "teeth": _arr(sm(teeth)), "lips": _arr(sm(lips))}


def _vortex(df, period=14, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    vp = (h - l.shift(1)).abs()
    vm = (l - h.shift(1)).abs()
    trs = tr.rolling(period).sum().replace(0, np.nan)
    return {"vi_plus": _arr(vp.rolling(period).sum() / trs),
            "vi_minus": _arr(vm.rolling(period).sum() / trs)}


# ------------------------------------------------------------------ momentum
def _rsi(df, period=14, **_):
    c = _f(df, "close")
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = up / dn.replace(0, np.nan)
    return {"rsi": _arr(100 - 100 / (1 + rs))}


def _stoch(df, k=14, d=3, smooth=3, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    ll, hh = l.rolling(k).min(), h.rolling(k).max()
    raw = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    kk = raw.rolling(smooth).mean()
    dd = kk.rolling(d).mean()
    return {"k": _arr(kk), "d": _arr(dd)}


def _stochrsi(df, rsi_period=14, stoch_period=14, k=3, d=3, **_):
    r = pd.Series(_rsi(df, rsi_period)["rsi"], index=df.index)
    ll, hh = r.rolling(stoch_period).min(), r.rolling(stoch_period).max()
    raw = 100 * (r - ll) / (hh - ll).replace(0, np.nan)
    kk = raw.rolling(k).mean()
    return {"k": _arr(kk), "d": _arr(kk.rolling(d).mean())}


def _cci(df, period=20, **_):
    tp = (_f(df, "high") + _f(df, "low") + _f(df, "close")) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    return {"cci": _arr((tp - ma) / (0.015 * md.replace(0, np.nan)))}


def _williams_r(df, period=14, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    hh, ll = h.rolling(period).max(), l.rolling(period).min()
    return {"wr": _arr(-100 * (hh - c) / (hh - ll).replace(0, np.nan))}


def _mfi(df, period=14, **_):
    tp = (_f(df, "high") + _f(df, "low") + _f(df, "close")) / 3
    mf = tp * _f(df, "volume")
    pos = mf.where(tp > tp.shift(1), 0.0).rolling(period).sum()
    neg = mf.where(tp < tp.shift(1), 0.0).rolling(period).sum()
    return {"mfi": _arr(100 - 100 / (1 + pos / neg.replace(0, np.nan)))}


def _roc(df, period=10, **_):
    c = _f(df, "close")
    return {"roc": _arr(100 * (c / c.shift(period) - 1))}


def _momentum(df, period=10, **_):
    return {"mom": _arr(_f(df, "close") - _f(df, "close").shift(period))}


def _tsi(df, long=25, short=13, **_):
    m = _f(df, "close").diff()
    num = _ema(_ema(m, long), short)
    den = _ema(_ema(m.abs(), long), short)
    return {"tsi": _arr(100 * num / den.replace(0, np.nan))}


def _ultimate(df, p1=7, p2=14, p3=28, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    pc = c.shift(1)
    bp = c - pd.concat([l, pc], axis=1).min(axis=1)
    tr = pd.concat([h, pc], axis=1).max(axis=1) - pd.concat([l, pc], axis=1).min(axis=1)
    def avg(n):
        return bp.rolling(n).sum() / tr.rolling(n).sum().replace(0, np.nan)
    return {"uo": _arr(100 * (4 * avg(p1) + 2 * avg(p2) + avg(p3)) / 7)}


def _awesome(df, fast=5, slow=34, **_):
    hl2 = (_f(df, "high") + _f(df, "low")) / 2
    return {"ao": _arr(hl2.rolling(fast).mean() - hl2.rolling(slow).mean())}


def _fisher(df, period=10, **_):
    h, l = _f(df, "high").to_numpy(), _f(df, "low").to_numpy()
    n = len(h)
    hl2 = (h + l) / 2
    out = np.full(n, np.nan)
    val = 0.0
    prev = 0.0
    for i in range(period, n):
        window = hl2[i - period + 1:i + 1]
        lo, hi = np.nanmin(window), np.nanmax(window)
        if not np.isfinite(lo) or hi == lo:
            continue
        x = 2 * ((hl2[i] - lo) / (hi - lo) - 0.5)
        val = 0.33 * x + 0.67 * val
        val = max(min(val, 0.999), -0.999)
        f = 0.5 * np.log((1 + val) / (1 - val)) + 0.5 * prev
        prev = f
        out[i] = f
    return {"fisher": out}


def _demarker(df, period=14, **_):
    h, l = _f(df, "high"), _f(df, "low")
    dmax = (h - h.shift(1)).clip(lower=0).rolling(period).mean()
    dmin = (l.shift(1) - l).clip(lower=0).rolling(period).mean()
    return {"demarker": _arr(dmax / (dmax + dmin).replace(0, np.nan))}


def _trix(df, period=15, signal=9, **_):
    c = _f(df, "close")
    e = _ema(_ema(_ema(c, period), period), period)
    trix = 100 * (e / e.shift(1) - 1)
    return {"trix": _arr(trix), "signal": _arr(_ema(trix, signal))}


# ----------------------------------------------------------------- volatility
def _atr_ind(df, period=14, **_):
    a = _atr(df, period)
    return {"atr_pct": _arr(100 * a / _f(df, "close")),
            "atr_ratio": _arr(_ratio(a))}


def _bbands(df, period=20, std=2.0, **_):
    c = _f(df, "close")
    mid = c.rolling(period).mean()
    sd = c.rolling(period).std(ddof=0)
    up, lo = mid + std * sd, mid - std * sd
    pctb = (c - lo) / (up - lo).replace(0, np.nan)
    width = 100 * (up - lo) / mid.replace(0, np.nan)
    return {"upper": _arr(up), "mid": _arr(mid), "lower": _arr(lo),
            "percent_b": _arr(pctb), "bandwidth": _arr(width),
            "width_ratio": _arr(_ratio(width))}


def _keltner(df, ema_period=20, atr_period=10, mult=2.0, **_):
    mid = _ema(_f(df, "close"), ema_period)
    a = _atr(df, atr_period)
    return {"upper": _arr(mid + mult * a), "mid": _arr(mid),
            "lower": _arr(mid - mult * a)}


def _donchian(df, period=20, **_):
    h, l = _f(df, "high"), _f(df, "low")
    up = h.rolling(period).max().shift(1)
    lo = l.rolling(period).min().shift(1)
    return {"upper": _arr(up), "mid": _arr((up + lo) / 2), "lower": _arr(lo)}


def _stddev(df, period=20, **_):
    s = _f(df, "close").pct_change().rolling(period).std(ddof=0) * 100
    return {"std_pct": _arr(s), "std_ratio": _arr(_ratio(s))}


def _histvol(df, period=20, **_):
    r = np.log(_f(df, "close") / _f(df, "close").shift(1))
    hv = r.rolling(period).std(ddof=0) * np.sqrt(252) * 100
    return {"histvol": _arr(hv), "vol_ratio": _arr(_ratio(hv))}


# --------------------------------------------------------------------- volume
def _obv(df, period=20, **_):
    c, v = _f(df, "close"), _f(df, "volume")
    obv = (np.sign(c.diff().fillna(0)) * v).cumsum()
    slope = (obv - obv.shift(period)) / v.rolling(period).sum().replace(0, np.nan)
    return {"obv_slope": _arr(slope)}


def _ad(df, period=20, **_):
    h, l, c, v = _f(df, "high"), _f(df, "low"), _f(df, "close"), _f(df, "volume")
    hl = (h - l).replace(0, np.nan)
    ad = (((c - l) - (h - c)) / hl * v).cumsum()
    slope = (ad - ad.shift(period)) / v.rolling(period).sum().replace(0, np.nan)
    return {"ad_slope": _arr(slope)}


def _cmf(df, period=20, **_):
    h, l, c, v = _f(df, "high"), _f(df, "low"), _f(df, "close"), _f(df, "volume")
    mfv = (((c - l) - (h - c)) / (h - l).replace(0, np.nan)) * v
    return {"cmf": _arr(mfv.rolling(period).sum() / v.rolling(period).sum().replace(0, np.nan))}


def _volume(df, period=20, **_):
    v = _f(df, "volume")
    return {"volume_ratio": _arr(v / v.rolling(period).mean().replace(0, np.nan))}


def _force(df, period=13, **_):
    fi = _f(df, "close").diff() * _f(df, "volume")
    return {"force": _arr(_ema(fi, period))}


# ---------------------------------------------------------------- price action
def _breakout(df, lookback=20, **_):
    h, l, c = _f(df, "high"), _f(df, "low"), _f(df, "close")
    return {"breakout_high": _b(c > h.rolling(lookback).max().shift(1)),
            "breakout_low": _b(c < l.rolling(lookback).min().shift(1))}


def _patterns(df, lookback=3, body_ratio=0.1, wick_ratio=2.0, **_):
    o, h, l, c = _f(df, "open"), _f(df, "high"), _f(df, "low"), _f(df, "close")
    po, pc = o.shift(1), c.shift(1)
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    inside = (h <= h.shift(1)) & (l >= l.shift(1))
    outside = (h > h.shift(1)) & (l < l.shift(1))
    bull_eng = (c > o) & (pc < po) & (c >= po) & (o <= pc)
    bear_eng = (c < o) & (pc > po) & (c <= po) & (o >= pc)
    lower_wick = np.minimum(o, c) - l
    upper_wick = h - np.maximum(o, c)
    pin_bull = (lower_wick > wick_ratio * body) & (lower_wick > wick_ratio * upper_wick) & (c >= o)
    pin_bear = (upper_wick > wick_ratio * body) & (upper_wick > wick_ratio * lower_wick) & (c <= o)
    doji = body <= body_ratio * rng
    return {
        "inside_bar": _b(inside), "outside_bar": _b(outside),
        "bull_engulf": _b(bull_eng), "bear_engulf": _b(bear_eng),
        "pin_bull": _b(pin_bull), "pin_bear": _b(pin_bear), "doji": _b(doji),
    }


def _higher(df, lookback=3, **_):
    h, l = _f(df, "high"), _f(df, "low")
    hh = (h > h.shift(1)) & (h.shift(1) > h.shift(2))
    ll = (l < l.shift(1)) & (l.shift(1) < l.shift(2))
    return {"higher_high": _b(hh), "lower_low": _b(ll)}


def _body(df, period=14, **_):
    a = _atr(df, period)
    return {"body_atr": _arr((_f(df, "close") - _f(df, "open")) / a.replace(0, np.nan))}


def _range(df, period=20, **_):
    r = _f(df, "high") - _f(df, "low")
    return {"range_ratio": _arr(_ratio(r, period))}


def _gap(df, period=14, **_):
    a = _atr(df, period)
    return {"gap_atr": _arr((_f(df, "open") - _f(df, "close").shift(1)) / a.replace(0, np.nan))}


# ------------------------------------------------------- price-action / HTF
HTF_CHOICES = ["H1", "H4", "D1", "W1", "MN1"]


def _ts_values(df: pd.DataFrame, ts=None) -> np.ndarray:
    """Bar timestamps in epoch seconds.

    The engine passes ``ctx.tf_ts`` explicitly.  When the indicator is used
    standalone, a ``ts`` column is honoured; otherwise bars are treated as one
    day apart so the function still returns a full-length, causal series.
    """
    if ts is not None:
        return np.asarray(ts, dtype=np.int64)
    if "ts" in df.columns:
        return (pd.to_datetime(df["ts"]).astype("datetime64[s]")
                .astype("int64").to_numpy(dtype=np.int64))
    return np.arange(len(df), dtype=np.int64) * 86400


def _tf_group(ts: np.ndarray, tf: str) -> np.ndarray:
    """A monotonically increasing group id for the requested timeframe.

    Intraday buckets match the resampler's epoch-aligned bars.  W1 is
    Monday-based and MN1 follows calendar months, again matching
    ``settings.TF_PANDAS``.
    """
    tf = (tf or "D1").upper()
    if tf == "MN1":
        dt = pd.to_datetime(ts, unit="s")
        return (dt.year.to_numpy(dtype=np.int64) * 12
                + dt.month.to_numpy(dtype=np.int64) - 1)
    if tf == "W1":
        days = ts // 86400
        return ((days + 3) // 7).astype(np.int64)
    secs = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
            "H4": 14400, "D1": 86400}.get(tf, 86400)
    return (ts // secs).astype(np.int64)


def _group_bounds(group: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    starts = np.r_[0, np.flatnonzero(group[1:] != group[:-1]) + 1]
    ends = np.r_[starts[1:], len(group)]
    return starts, ends


def _group_ordinals(starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    return np.repeat(np.arange(len(starts), dtype=np.int64), ends - starts)


def _group_final(values: np.ndarray, starts: np.ndarray, kind: str) -> np.ndarray:
    if kind == "max":
        return np.maximum.reduceat(values, starts)
    return np.minimum.reduceat(values, starts)


def _group_running(values: np.ndarray, ordinals: np.ndarray,
                   kind: str) -> np.ndarray:
    s = pd.Series(values)
    g = pd.Series(ordinals)
    if kind == "max":
        return s.groupby(g, sort=False).cummax().to_numpy(dtype=np.float64)
    return s.groupby(g, sort=False).cummin().to_numpy(dtype=np.float64)


def _htf_rolling(df: pd.DataFrame, ts, tf: str, lookback,
                 field: str, kind: str) -> np.ndarray:
    """Rolling max/min over the last ``lookback`` bars of ``tf``.

    The current (possibly incomplete) timeframe bar contributes only the
    running extreme up to the current base bar.  Prior bars contribute their
    final values.  This is causal: truncating the input at bar *t* leaves the
    value at *t* unchanged.
    """
    stamps = _ts_values(df, ts)
    values = _f(df, field).to_numpy(dtype=np.float64)
    n = len(values)
    if n == 0:
        return values
    group = _tf_group(stamps, tf)
    starts, ends = _group_bounds(group)
    ordinals = _group_ordinals(starts, ends)
    running = _group_running(values, ordinals, kind)
    lookback = max(1, int(lookback))
    if lookback == 1:
        return running
    finals = _group_final(values, starts, kind)
    prior = pd.Series(finals).rolling(
        lookback - 1, min_periods=lookback - 1)
    prior = getattr(prior, kind)().shift(1).to_numpy(dtype=np.float64)
    p = prior[ordinals]
    out = running.copy()
    valid = np.isfinite(p)
    if kind == "max":
        out[valid] = np.maximum(out[valid], p[valid])
    else:
        out[valid] = np.minimum(out[valid], p[valid])
    out[ordinals < lookback - 1] = np.nan
    return out


def _htf_offset(df: pd.DataFrame, ts, tf: str, offset,
                field: str, kind: str) -> np.ndarray:
    """Level of the timeframe bar ``offset`` periods back.

    ``offset=0`` is the active bar and returns the running extreme up to the
    current base bar.  ``offset>=1`` returns the final extreme of a completed
    bar, so no future data can leak into the active period.
    """
    stamps = _ts_values(df, ts)
    values = _f(df, field).to_numpy(dtype=np.float64)
    n = len(values)
    offset = max(0, int(offset))
    if n == 0:
        return values
    group = _tf_group(stamps, tf)
    starts, ends = _group_bounds(group)
    ordinals = _group_ordinals(starts, ends)
    if offset == 0:
        return _group_running(values, ordinals, kind)
    finals = _group_final(values, starts, kind)
    out = np.full(n, np.nan, dtype=np.float64)
    src = ordinals - offset
    valid = src >= 0
    out[valid] = finals[src[valid]]
    return out


def _i_high(df, ts=None, tf="D1", lookback=20, **_):
    return {"value": _htf_rolling(df, ts, tf, lookback, "high", "max")}


def _i_low(df, ts=None, tf="D1", lookback=20, **_):
    return {"value": _htf_rolling(df, ts, tf, lookback, "low", "min")}


def _i_daily_high(df, ts=None, offset=0, **_):
    return {"value": _htf_offset(df, ts, "D1", offset, "high", "max")}


def _i_daily_low(df, ts=None, offset=0, **_):
    return {"value": _htf_offset(df, ts, "D1", offset, "low", "min")}


def _i_monthly_high(df, ts=None, offset=0, **_):
    return {"value": _htf_offset(df, ts, "MN1", offset, "high", "max")}


def _i_monthly_low(df, ts=None, offset=0, **_):
    return {"value": _htf_offset(df, ts, "MN1", offset, "low", "min")}


# --------------------------------------------------------------------- registry
INDICATORS: dict[str, Indicator] = {}


def add(ind: Indicator, fn: Callable) -> None:
    INDICATORS[ind.id] = ind
    COMPUTE[ind.id] = fn


COMPUTE: dict[str, Callable] = {}

add(Indicator("sma", "Simple MA", "Trend", "Arithmetic moving average of close.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "SMA", "price")], 120), _sma)
add(Indicator("ema", "Exponential MA", "Trend", "Exponentially weighted moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "EMA", "price")], 120), _ema_ind)
add(Indicator("wma", "Weighted MA", "Trend", "Linearly weighted moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "WMA", "price")], 120), _wma_ind)
add(Indicator("hma", "Hull MA", "Trend", "Hull moving average (low lag).",
              [P("period", "Period", 20, 10, 100, 2)],
              [O("value", "HMA", "price")], 150), _hma)
add(Indicator("dema", "DEMA", "Trend", "Double exponential moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "DEMA", "price")], 150), _dema)
add(Indicator("tema", "TEMA", "Trend", "Triple exponential moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "TEMA", "price")], 180), _tema)
add(Indicator("kama", "KAMA", "Trend", "Kaufman adaptive moving average.",
              [P("period", "Period", 10, 5, 50, 1),
               P("fast", "Fast EMA", 2, 2, 10, 1),
               P("slow", "Slow EMA", 30, 10, 60, 5)],
              [O("value", "KAMA", "price")], 200), _kama)
add(Indicator("vwma", "VWMA", "Trend", "Volume-weighted moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("value", "VWMA", "price")], 120, True), _vwma)
add(Indicator("macd", "MACD", "Trend", "Moving average convergence divergence.",
              [P("fast", "Fast", 12, 5, 30, 1), P("slow", "Slow", 26, 15, 60, 1),
               P("signal", "Signal", 9, 3, 20, 1)],
              [O("macd", "MACD", "level", [0]), O("signal", "Signal", "level", [0]),
               O("hist", "Histogram", "level", [0])], 120), _macd)
add(Indicator("adx", "ADX / DI", "Trend", "Trend strength and directional movement.",
              [P("period", "Period", 14, 7, 40, 1)],
              [O("adx", "ADX", "level", [20, 25, 30, 40]),
               O("plus_di", "+DI", "level", [15, 20, 25, 30]),
               O("minus_di", "-DI", "level", [15, 20, 25, 30])], 120), _adx)
add(Indicator("aroon", "Aroon", "Trend", "Bars since the highest high / lowest low.",
              [P("period", "Period", 14, 5, 50, 1)],
              [O("aroon_up", "Aroon Up", "level", [50, 70, 80]),
               O("aroon_down", "Aroon Down", "level", [50, 70, 80])], 100), _aroon)
add(Indicator("supertrend", "Supertrend", "Trend", "ATR band trend follower.",
              [P("period", "ATR period", 10, 5, 30, 1),
               P("mult", "Multiplier", 3.0, 1.0, 6.0, 0.25, False)],
              [O("super_dir", "Direction", "level", [0]),
               O("super_line", "Band", "price")], 120), _supertrend)
add(Indicator("psar", "Parabolic SAR", "Trend", "Parabolic stop and reverse.",
              [P("step", "Step", 0.02, 0.01, 0.1, 0.01, False),
               P("max_step", "Max step", 0.2, 0.1, 0.5, 0.05, False)],
              [O("psar", "SAR", "price")], 100), _psar)
add(Indicator("ichimoku", "Ichimoku", "Trend", "Tenkan, kijun and cloud levels (unshifted).",
              [P("tenkan", "Tenkan", 9, 5, 20, 1), P("kijun", "Kijun", 26, 10, 60, 1),
               P("senkou", "Senkou B", 52, 20, 120, 2)],
              [O("tenkan", "Tenkan", "price"), O("kijun", "Kijun", "price"),
               O("senkou_a", "Senkou A", "price"), O("senkou_b", "Senkou B", "price")], 150), _ichimoku)
add(Indicator("alligator", "Alligator", "Trend", "Smoothed median price lines (unshifted).",
              [P("jaw", "Jaw", 13, 5, 30, 1), P("teeth", "Teeth", 8, 4, 20, 1),
               P("lips", "Lips", 5, 3, 15, 1)],
              [O("jaw", "Jaw", "price"), O("teeth", "Teeth", "price"),
               O("lips", "Lips", "price")], 120), _alligator)
add(Indicator("vortex", "Vortex", "Trend", "Positive/negative vortex movement.",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("vi_plus", "VI+", "level", [1.0]), O("vi_minus", "VI-", "level", [1.0])], 80), _vortex)

add(Indicator("rsi", "RSI", "Momentum", "Relative strength index.",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("rsi", "RSI", "level", [20, 30, 50, 70, 80])], 100), _rsi)
add(Indicator("stochastic", "Stochastic", "Momentum", "Stochastic oscillator %K / %D.",
              [P("k", "K period", 14, 5, 40, 1), P("d", "D period", 3, 1, 10, 1),
               P("smooth", "Smooth", 3, 1, 10, 1)],
              [O("k", "%K", "level", [20, 50, 80]), O("d", "%D", "level", [20, 50, 80])], 100), _stoch)
add(Indicator("stochrsi", "Stoch RSI", "Momentum", "Stochastic of RSI.",
              [P("rsi_period", "RSI period", 14, 5, 40, 1),
               P("stoch_period", "Stoch period", 14, 5, 40, 1),
               P("k", "K", 3, 1, 10, 1), P("d", "D", 3, 1, 10, 1)],
              [O("k", "%K", "level", [20, 50, 80]), O("d", "%D", "level", [20, 50, 80])], 150), _stochrsi)
add(Indicator("cci", "CCI", "Momentum", "Commodity channel index.",
              [P("period", "Period", 20, 5, 60, 1)],
              [O("cci", "CCI", "level", [-200, -100, 0, 100, 200])], 100), _cci)
add(Indicator("williams_r", "Williams %R", "Momentum", "Williams percent range.",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("wr", "%R", "level", [-80, -50, -20])], 100), _williams_r)
add(Indicator("mfi", "MFI", "Momentum", "Money flow index (volume weighted RSI).",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("mfi", "MFI", "level", [20, 50, 80])], 100, True), _mfi)
add(Indicator("roc", "Rate of Change", "Momentum", "Percent change over N bars.",
              [P("period", "Period", 10, 3, 60, 1)],
              [O("roc", "ROC", "level", [0])], 80), _roc)
add(Indicator("momentum", "Momentum", "Momentum", "Close difference over N bars.",
              [P("period", "Period", 10, 3, 60, 1)],
              [O("mom", "Momentum", "level", [0])], 80), _momentum)
add(Indicator("tsi", "TSI", "Momentum", "True strength index.",
              [P("long", "Long", 25, 10, 50, 1), P("short", "Short", 13, 5, 30, 1)],
              [O("tsi", "TSI", "level", [-25, 0, 25])], 150), _tsi)
add(Indicator("ultimate", "Ultimate Osc", "Momentum", "Ultimate oscillator.",
              [P("p1", "Period 1", 7, 3, 15, 1), P("p2", "Period 2", 14, 7, 30, 1),
               P("p3", "Period 3", 28, 14, 60, 1)],
              [O("uo", "UO", "level", [30, 50, 70])], 120), _ultimate)
add(Indicator("awesome", "Awesome Osc", "Momentum", "SMA(median,5) - SMA(median,34).",
              [P("fast", "Fast", 5, 2, 15, 1), P("slow", "Slow", 34, 20, 80, 2)],
              [O("ao", "AO", "level", [0])], 120), _awesome)
add(Indicator("fisher", "Fisher Transform", "Momentum", "Fisher transform of price.",
              [P("period", "Period", 10, 5, 40, 1)],
              [O("fisher", "Fisher", "level", [-2, -1, 0, 1, 2])], 100), _fisher)
add(Indicator("demarker", "DeMarker", "Momentum", "DeMarker oscillator.",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("demarker", "DeMarker", "level", [0.3, 0.5, 0.7])], 100), _demarker)
add(Indicator("trix", "TRIX", "Momentum", "Triple smoothed rate of change.",
              [P("period", "Period", 15, 5, 40, 1), P("signal", "Signal", 9, 3, 20, 1)],
              [O("trix", "TRIX", "level", [0]), O("signal", "Signal", "level", [0])], 150), _trix)

add(Indicator("atr", "ATR", "Volatility", "Average true range as % and ratio to its median.",
              [P("period", "Period", 14, 5, 40, 1)],
              [O("atr_ratio", "ATR ratio", "level", [0.8, 1.0, 1.2, 1.5]),
               O("atr_pct", "ATR %", "level", [0.05, 0.1, 0.2])], 120), _atr_ind)
add(Indicator("bbands", "Bollinger Bands", "Volatility", "Mean +/- N standard deviations.",
              [P("period", "Period", 20, 10, 80, 1),
               P("std", "Std dev", 2.0, 1.0, 4.0, 0.25, False)],
              [O("upper", "Upper", "price"), O("mid", "Middle", "price"),
               O("lower", "Lower", "price"), O("percent_b", "%B", "level", [0, 0.5, 1]),
               O("width_ratio", "Width ratio", "level", [0.8, 1.0, 1.3, 1.8])], 120), _bbands)
add(Indicator("keltner", "Keltner", "Volatility", "EMA +/- ATR multiple.",
              [P("ema_period", "EMA period", 20, 5, 80, 1),
               P("atr_period", "ATR period", 10, 5, 40, 1),
               P("mult", "Multiplier", 2.0, 0.5, 5.0, 0.25, False)],
              [O("upper", "Upper", "price"), O("mid", "Middle", "price"),
               O("lower", "Lower", "price")], 120), _keltner)
add(Indicator("donchian", "Donchian", "Volatility", "N-bar high/low channel (prior bars).",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("upper", "Upper", "price"), O("mid", "Middle", "price"),
               O("lower", "Lower", "price")], 120), _donchian)
add(Indicator("stddev", "Std Dev", "Volatility", "Close return volatility.",
              [P("period", "Period", 20, 5, 80, 1)],
              [O("std_ratio", "Std ratio", "level", [0.8, 1.0, 1.3, 1.8]),
               O("std_pct", "Std %", "level", [0.05, 0.1, 0.2])], 120), _stddev)
add(Indicator("histvol", "Historical Vol", "Volatility", "Annualized close-to-close volatility.",
              [P("period", "Period", 20, 10, 80, 1)],
              [O("vol_ratio", "Vol ratio", "level", [0.8, 1.0, 1.3, 1.8]),
               O("histvol", "Hist vol %", "level", [5, 10, 20])], 120), _histvol)

add(Indicator("obv", "OBV", "Volume", "On-balance volume slope.",
              [P("period", "Period", 20, 5, 60, 1)],
              [O("obv_slope", "OBV slope", "level", [-0.1, 0, 0.1])], 100, True), _obv)
add(Indicator("ad", "A/D Line", "Volume", "Accumulation/distribution slope.",
              [P("period", "Period", 20, 5, 60, 1)],
              [O("ad_slope", "A/D slope", "level", [-0.1, 0, 0.1])], 100, True), _ad)
add(Indicator("cmf", "Chaikin MF", "Volume", "Chaikin money flow.",
              [P("period", "Period", 20, 5, 60, 1)],
              [O("cmf", "CMF", "level", [-0.1, 0, 0.1])], 100, True), _cmf)
add(Indicator("volume", "Volume", "Volume", "Volume relative to its moving average.",
              [P("period", "Period", 20, 5, 100, 1)],
              [O("volume_ratio", "Volume ratio", "level", [1.0, 1.2, 1.5, 2.0])], 100, True), _volume)
add(Indicator("force", "Force Index", "Volume", "Smoothed price change times volume.",
              [P("period", "Period", 13, 3, 40, 1)],
              [O("force", "Force", "level", [0])], 100, True), _force)

add(Indicator("breakout", "N-bar Breakout", "Price Action", "Close breaks the prior N-bar high/low.",
              [P("lookback", "Lookback", 20, 5, 100, 1)],
              [O("breakout_high", "Breakout up", "bool"),
               O("breakout_low", "Breakout down", "bool")], 100), _breakout)
add(Indicator("patterns", "Candlestick Patterns", "Price Action", "Engulfing, pin bar, inside/outside, doji.",
              [P("lookback", "Lookback", 3, 2, 10, 1),
               P("body_ratio", "Doji body ratio", 0.1, 0.05, 0.3, 0.05, False),
               P("wick_ratio", "Wick ratio", 2.0, 1.0, 4.0, 0.25, False)],
              [O("bull_engulf", "Bullish engulfing", "bool"),
               O("bear_engulf", "Bearish engulfing", "bool"),
               O("pin_bull", "Bullish pin", "bool"), O("pin_bear", "Bearish pin", "bool"),
               O("inside_bar", "Inside bar", "bool"), O("outside_bar", "Outside bar", "bool"),
               O("doji", "Doji", "bool")], 20), _patterns)
add(Indicator("higher", "Higher High / Lower Low", "Price Action", "Two consecutive higher highs or lower lows.",
              [P("lookback", "Lookback", 3, 2, 10, 1)],
              [O("higher_high", "Higher high", "bool"),
               O("lower_low", "Lower low", "bool")], 20), _higher)
add(Indicator("body", "Candle Body", "Price Action", "Body size normalized by ATR.",
              [P("period", "ATR period", 14, 5, 40, 1)],
              [O("body_atr", "Body / ATR", "level", [-1.0, -0.5, 0, 0.5, 1.0])], 100), _body)
add(Indicator("range", "Range Expansion", "Price Action", "Bar range relative to its median.",
              [P("period", "Median period", 20, 5, 100, 1)],
              [O("range_ratio", "Range ratio", "level", [1.0, 1.2, 1.5, 2.0])], 100), _range)
add(Indicator("gap", "Gap", "Price Action", "Open gap from previous close, in ATR units.",
              [P("period", "ATR period", 14, 5, 40, 1)],
              [O("gap_atr", "Gap / ATR", "level", [-1.0, -0.5, 0, 0.5, 1.0])], 100), _gap)

add(Indicator("iHigh", "HTF High", "Price Action",
              "Highest high over N bars of a higher timeframe, aligned causally to base bars.",
              [P("tf", "Timeframe", "D1", 0, 0, 1, True, HTF_CHOICES),
               P("lookback", "Lookback", 20, 2, 200, 1)],
              [O("value", "HTF high", "price")], 200), _i_high)
add(Indicator("iLow", "HTF Low", "Price Action",
              "Lowest low over N bars of a higher timeframe, aligned causally to base bars.",
              [P("tf", "Timeframe", "D1", 0, 0, 1, True, HTF_CHOICES),
               P("lookback", "Lookback", 20, 2, 200, 1)],
              [O("value", "HTF low", "price")], 200), _i_low)
add(Indicator("iDailyHigh", "Daily High", "Price Action",
              "High of the daily bar at an offset (0 = active day, 1 = prior completed day).",
              [P("offset", "Offset", 0, 0, 20, 1)],
              [O("value", "Daily high", "price")], 40), _i_daily_high)
add(Indicator("iDailyLow", "Daily Low", "Price Action",
              "Low of the daily bar at an offset (0 = active day, 1 = prior completed day).",
              [P("offset", "Offset", 0, 0, 20, 1)],
              [O("value", "Daily low", "price")], 40), _i_daily_low)
add(Indicator("iMonthlyHigh", "Monthly High", "Price Action",
              "High of the monthly bar at an offset (0 = active month, 1 = prior completed month).",
              [P("offset", "Offset", 0, 0, 24, 1)],
              [O("value", "Monthly high", "price")], 60), _i_monthly_high)
add(Indicator("iMonthlyLow", "Monthly Low", "Price Action",
              "Low of the monthly bar at an offset (0 = active month, 1 = prior completed month).",
              [P("offset", "Offset", 0, 0, 24, 1)],
              [O("value", "Monthly low", "price")], 60), _i_monthly_low)


def default_indicator_config() -> dict[str, Any]:
    """All indicators enabled with their catalogue min/max as the sample range."""
    out: dict[str, Any] = {}
    for ind in INDICATORS.values():
        out[ind.id] = {
            "enabled": True,
            "params": {
                p.name: {"min": p.min, "max": p.max, "step": p.step}
                for p in ind.params if not p.choices
            },
        }
    return out


def catalogue() -> list[dict[str, Any]]:
    return [ind.as_dict() for ind in INDICATORS.values()]


def compute(indicator_id: str, df: pd.DataFrame, params: dict[str, Any],
            ts=None) -> dict[str, np.ndarray]:
    ind = INDICATORS.get(indicator_id)
    if ind is None:
        raise KeyError(f"Unknown indicator: {indicator_id}")
    clean = {}
    for p in ind.params:
        v = params.get(p.name, p.default)
        if p.choices:
            value = str(v) if v is not None else str(p.default)
            if value not in p.choices:
                # Accept a numeric index for compact/legacy configs.
                try:
                    value = p.choices[int(round(float(v)))]
                except (TypeError, ValueError, IndexError):
                    value = str(p.default)
            clean[p.name] = value
        elif p.integer:
            clean[p.name] = int(round(float(v)))
        else:
            clean[p.name] = float(v)
    return COMPUTE[indicator_id](df, ts=ts, **clean)
