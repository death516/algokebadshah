"""Strategy checks: signal geometry + guards, on synthetic data (Role B)."""
import numpy as np
import pandas as pd

from app.strategy.trend import TrendStrategy, TrendParams, compute_signals


def _frame(close: np.ndarray, start="2026-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq="15min", tz="UTC")
    close = pd.Series(close, index=idx)
    return pd.DataFrame(
        {"open": close.shift(1).fillna(close), "high": close + 2,
         "low": close - 2, "close": close, "volume": 1.0},
        index=idx,
    )


def _htf(df15: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df15.resample("1h").agg(agg).dropna()


def _noisy_trend(drift: float, n: int = 1200, seed: int = 0) -> np.ndarray:
    # Random walk with drift + mild noise -> many EMA crosses while the 1h
    # trend filter is already aligned, which is how a real signal fires.
    rng = np.random.default_rng(seed)
    steps = drift + rng.normal(0, abs(drift) * 4 + 0.5, n)
    return 100.0 + np.cumsum(steps)


def test_uptrend_produces_long_signal_with_valid_geometry():
    df15 = _frame(_noisy_trend(+0.15))
    sig = compute_signals(df15, _htf(df15))
    buys = sig[sig["side"] == "buy"]
    assert len(buys) >= 1, "expected at least one long signal in an uptrend"
    for _, b in buys.iterrows():
        assert b["stop"] < b["entry"] < b["tp"]
        rr = (b["tp"] - b["entry"]) / (b["entry"] - b["stop"])
        assert np.isclose(rr, TrendParams().rr, atol=1e-6)


def test_downtrend_produces_short_signal():
    df15 = _frame(_noisy_trend(-0.15))
    sig = compute_signals(df15, _htf(df15))
    sells = sig[sig["side"] == "sell"]
    assert len(sells) >= 1
    for _, s in sells.iterrows():
        assert s["stop"] > s["entry"] > s["tp"]


def test_generate_signal_needs_enough_bars():
    from app.models import Candle
    few = [Candle(ts=i * 900, open=1, high=1, low=1, close=1, volume=1) for i in range(10)]
    assert TrendStrategy().generate_signal(few, few) is None


def test_no_htf_lookahead():
    # htf_close attached to a 15m bar must come from an already-closed 1h candle.
    up = np.linspace(60, 160, 400)
    df15 = _frame(up)
    sig = compute_signals(df15, _htf(df15))
    # First usable htf value only appears after the first full hour has closed.
    first_valid = sig["htf_close"].first_valid_index()
    assert first_valid >= df15.index[0] + pd.Timedelta(hours=1)
