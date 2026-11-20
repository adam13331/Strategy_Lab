from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sqx import indicators


def make_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    steps = rng.normal(0, 0.15, n).cumsum()
    close = 150.0 + steps
    open_ = close + rng.normal(0, 0.05, n)
    high = np.maximum(open_, close) + rng.random(n) * 0.2
    low = np.minimum(open_, close) - rng.random(n) * 0.2
    vol = rng.integers(50, 500, n).astype(float)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": vol,
    })


@pytest.mark.parametrize("ind", indicators.catalogue(), ids=lambda d: d["id"])
def test_every_indicator_computes(ind):
    df = make_df()
    params = {p["name"]: p["default"] for p in ind["params"]}
    out = indicators.compute(ind["id"], df, params)
    assert set(out) >= {o["key"] for o in ind["outputs"]}
    for key, arr in out.items():
        assert len(arr) == len(df), f"{ind['id']}.{key} length mismatch"
        assert np.isfinite(np.nan_to_num(arr)).all(), f"{ind['id']}.{key} has infinity"
        assert not np.isnan(arr).all(), f"{ind['id']}.{key} is all NaN"


def test_rsi_bounds():
    df = make_df()
    rsi = indicators.compute("rsi", df, {"period": 14})["rsi"]
    finite = rsi[np.isfinite(rsi)]
    assert finite.min() >= 0.0
    assert finite.max() <= 100.0


def test_bollinger_band_order():
    df = make_df()
    out = indicators.compute("bbands", df, {"period": 20, "std": 2.0})
    lower, upper = out["lower"], out["upper"]
    ok = np.isfinite(lower) & np.isfinite(upper)
    assert np.all(lower[ok] <= upper[ok])


def test_unknown_indicator():
    with pytest.raises(KeyError):
        indicators.compute("nope", make_df(), {})


def test_catalogue_defaults_match_ranges():
    for ind in indicators.catalogue():
        for p in ind["params"]:
            if p["choices"]:
                assert p["default"] in p["choices"], (ind["id"], p["name"])
                continue
            assert p["min"] <= p["default"] <= p["max"], (ind["id"], p["name"])


def _htf_bars(start: str, periods: int) -> tuple[pd.DataFrame, np.ndarray]:
    ts_idx = pd.date_range(start, periods=periods, freq="h")
    ts = ts_idx.astype("datetime64[s]").astype("int64").to_numpy()
    x = np.arange(periods, dtype=np.float64)
    close = 100.0 + x * 0.05 + np.sin(x / 7.0)
    open_ = close - 0.02
    high = np.maximum(open_, close) + 0.10 + (x % 5) * 0.03
    low = np.minimum(open_, close) - 0.10 - (x % 3) * 0.02
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": np.ones(periods)})
    return df, ts


def test_daily_high_offset_levels_are_causal():
    df, ts = _htf_bars("2024-01-01", 72)  # three UTC days
    highs = df["high"].to_numpy()
    day = ts // 86400
    for offset in (0, 1):
        out = indicators.compute("iDailyHigh", df, {"offset": offset},
                                 ts=ts)["value"]
        assert len(out) == len(df)
        for i in range(len(df)):
            same_day = np.flatnonzero(day == day[i])
            first = int(same_day[0])
            if offset == 0:
                expected = highs[first:i + 1].max()
            else:
                prior = np.unique(day[day < day[i]])
                if len(prior) < offset:
                    assert np.isnan(out[i])
                    continue
                target = prior[-offset]
                expected = highs[day == target].max()
            assert out[i] == pytest.approx(expected), (offset, i)


def test_daily_low_offset_levels_are_causal():
    df, ts = _htf_bars("2024-01-01", 72)
    lows = df["low"].to_numpy()
    day = ts // 86400
    out = indicators.compute("iDailyLow", df, {"offset": 1}, ts=ts)["value"]
    for i in range(len(df)):
        prior = np.unique(day[day < day[i]])
        if not len(prior):
            assert np.isnan(out[i])
            continue
        expected = lows[day == prior[-1]].min()
        assert out[i] == pytest.approx(expected)


def test_htf_rolling_high_includes_only_running_active_bar():
    df, ts = _htf_bars("2024-01-01", 96)  # four UTC days
    highs = df["high"].to_numpy()
    day = ts // 86400
    out = indicators.compute("iHigh", df,
                             {"tf": "D1", "lookback": 2}, ts=ts)["value"]
    unique = np.unique(day)
    for i in range(len(df)):
        current = day[i]
        earlier = unique[unique < current]
        if len(earlier) < 1:
            assert np.isnan(out[i])
            continue
        first_today = int(np.flatnonzero(day == current)[0])
        expected = max(highs[day == earlier[-1]].max(),
                       highs[first_today:i + 1].max())
        assert out[i] == pytest.approx(expected), i


def test_htf_rolling_low_mirrors_high():
    df, ts = _htf_bars("2024-01-01", 72)
    lows = df["low"].to_numpy()
    day = ts // 86400
    out = indicators.compute("iLow", df,
                             {"tf": "D1", "lookback": 3}, ts=ts)["value"]
    for i in range(len(df)):
        current = day[i]
        earlier = np.unique(day[day < current])
        if len(earlier) < 2:
            assert np.isnan(out[i])
            continue
        first_today = int(np.flatnonzero(day == current)[0])
        expected = min(lows[np.isin(day, earlier[-2:])].min(),
                       lows[first_today:i + 1].min())
        assert out[i] == pytest.approx(expected)


def test_monthly_offset_uses_completed_months():
    df, ts = _htf_bars("2024-01-20", 24 * 50)  # Jan tail, Feb, early Mar
    highs = df["high"].to_numpy()
    dt = pd.to_datetime(ts, unit="s")
    month = (dt.year * 12 + dt.month - 1).to_numpy()
    out = indicators.compute("iMonthlyHigh", df, {"offset": 1},
                             ts=ts)["value"]
    for i in range(len(df)):
        prior = np.unique(month[month < month[i]])
        if not len(prior):
            assert np.isnan(out[i])
            continue
        expected = highs[month == prior[-1]].max()
        assert out[i] == pytest.approx(expected)
    active = indicators.compute("iMonthlyHigh", df, {"offset": 0},
                                ts=ts)["value"]
    for i in range(0, len(df), 17):
        first = int(np.flatnonzero(month == month[i])[0])
        assert active[i] == pytest.approx(highs[first:i + 1].max())


def test_htf_indicators_have_no_lookahead_under_truncation():
    df, ts = _htf_bars("2024-01-01", 72)
    for ind_id, params in (("iHigh", {"tf": "D1", "lookback": 3}),
                           ("iDailyHigh", {"offset": 0}),
                           ("iDailyLow", {"offset": 0}),
                           ("iMonthlyHigh", {"offset": 0})):
        full = indicators.compute(ind_id, df, params, ts=ts)["value"]
        for t in (10, 25, 47, 60):
            part = indicators.compute(ind_id, df.iloc[:t + 1], params,
                                      ts=ts[:t + 1])["value"]
            if np.isnan(full[t]):
                assert np.isnan(part[-1]), (ind_id, t)
            else:
                assert part[-1] == pytest.approx(full[t]), (ind_id, t)


def test_htf_compute_accepts_timeframe_index_and_column():
    df, ts = _htf_bars("2024-01-01", 48)
    by_index = indicators.compute("iHigh", df, {"tf": 2, "lookback": 5},
                                  ts=ts)["value"]
    by_name = indicators.compute("iHigh", df, {"tf": "D1", "lookback": 5},
                                 ts=ts)["value"]
    assert np.allclose(by_index, by_name, equal_nan=True)
    framed = df.assign(ts=pd.to_datetime(ts, unit="s"))
    from_col = indicators.compute("iHigh", framed,
                                  {"tf": "D1", "lookback": 5})["value"]
    assert np.allclose(from_col, by_name, equal_nan=True)
