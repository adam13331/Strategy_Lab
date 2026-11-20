from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sqx import data


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_bar_cache(self, *args) -> None:
        self.calls.append(args)


@pytest.fixture()
def market(tmp_path, monkeypatch):
    ts = pd.date_range("2021-03-01 00:00", periods=60, freq="min")
    close = np.arange(60, dtype=float) + 100.0
    df = pd.DataFrame({
        "ts": ts, "open": close - 0.5, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": np.ones(60),
    })
    m1 = tmp_path / "m1.parquet"
    df.to_parquet(m1, index=False)
    fake = FakeStore()
    monkeypatch.setattr(data, "m1_path", lambda symbol: m1)
    monkeypatch.setattr(data, "tf_path", lambda symbol, tf: tmp_path / f"{tf.lower()}.parquet")
    monkeypatch.setattr(data, "store", fake)
    return tmp_path, df, fake


def test_resample_ohlcv(market):
    tmp, m1, fake = market
    out = data.resample("TEST", ["M5", "H1"])
    assert [r["timeframe"] for r in out] == ["M5", "H1"]
    assert len(fake.calls) == 2

    m5 = pd.read_parquet(tmp / "m5.parquet")
    assert len(m5) == 12
    first = m5.iloc[0]
    assert first["open"] == m1.iloc[0]["open"]
    assert first["high"] == m1.iloc[:5]["high"].max()
    assert first["low"] == m1.iloc[:5]["low"].min()
    assert first["close"] == m1.iloc[4]["close"]
    assert first["volume"] == m1.iloc[:5]["volume"].sum()
    assert m5.iloc[-1]["close"] == m1.iloc[-1]["close"]


def test_resample_drops_incomplete_trailing_bucket(market):
    tmp, _, _ = market
    data.resample("TEST", ["H1"])
    h1 = pd.read_parquet(tmp / "h1.parquet")
    # only the first full hour exists in the fixture
    assert len(h1) == 1


def test_resample_requires_m1(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "m1_path", lambda symbol: tmp_path / "missing.parquet")
    with pytest.raises(FileNotFoundError):
        data.resample("NOPE", ["M5"])
