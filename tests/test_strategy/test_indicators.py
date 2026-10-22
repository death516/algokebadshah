"""Indicator checks: known values + invariants (Role B)."""
import numpy as np
import pandas as pd

from app.data.indicators import ema, rsi, atr, adx, true_range


def test_ema_constant_series():
    s = pd.Series([5.0] * 50)
    assert np.allclose(ema(s, 10), 5.0)


def test_ema_known_values():
    # span=2 -> alpha = 2/3; adjust=False recursion.
    s = pd.Series([1.0, 2.0, 3.0])
    out = ema(s, 2)
    # e0=1; e1=1 + 2/3*(2-1)=1.6667; e2=1.6667 + 2/3*(3-1.6667)=2.5556
    assert np.isclose(out.iloc[0], 1.0)
    assert np.isclose(out.iloc[1], 1.0 + (2 / 3) * 1.0)
    assert np.isclose(out.iloc[2], out.iloc[1] + (2 / 3) * (3 - out.iloc[1]))


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))        # strictly rising
    down = pd.Series(np.arange(60, 1, -1, dtype=float))  # strictly falling
    r_up, r_down = rsi(up), rsi(down)
    assert (r_up.dropna() >= 0).all() and (r_up.dropna() <= 100).all()
    assert np.isclose(r_up.iloc[-1], 100.0)   # only gains -> 100
    assert np.isclose(r_down.iloc[-1], 0.0)   # only losses -> 0


def test_true_range_simple():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([8.0, 9.0])
    close = pd.Series([9.0, 11.0])
    tr = true_range(high, low, close)
    assert np.isclose(tr.iloc[0], 2.0)              # first bar: high-low
    assert np.isclose(tr.iloc[1], max(3.0, 3.0, 0.0))  # 12-9, |12-9|, |9-9|


def test_atr_positive():
    n = 100
    rng = np.random.default_rng(0)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    high = close + rng.uniform(0, 2, n)
    low = close - rng.uniform(0, 2, n)
    a = atr(high, low, close, 14)
    assert (a.dropna() > 0).all()


def test_adx_bounds_and_trend():
    n = 200
    # Clean uptrend should score a high ADX; flat noise should score low.
    trend_close = pd.Series(np.linspace(100, 200, n))
    trend = adx(trend_close + 0.5, trend_close - 0.5, trend_close, 14)
    flat = pd.Series(100 + np.sin(np.arange(n)))
    flat_adx = adx(flat + 0.5, flat - 0.5, flat, 14)
    assert (trend.dropna() >= 0).all() and (trend.dropna() <= 100).all()
    assert trend.iloc[-1] > flat_adx.iloc[-1]
