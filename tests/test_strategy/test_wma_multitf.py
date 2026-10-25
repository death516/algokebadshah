"""Regression checks for the ALGO COMPETITION WMA multi-timeframe strategy."""
import numpy as np
import pandas as pd

from app.strategy.wma_multitf import compute_signals


def _bars(n=2000):
    index = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    rng = np.random.default_rng(5)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = np.r_[close[0], close[:-1]]
    volume = rng.uniform(500.0, 1500.0, n)
    five = pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + 1.0,
        "low": np.minimum(open_, close) - 1.0, "close": close, "volume": volume,
    }, index=index)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return {
        "5min": five,
        "15min": five.resample("15min", label="right", closed="right").agg(agg).dropna(),
        "1h": five.resample("1h", label="right", closed="right").agg(agg).dropna(),
    }


def test_wma_mtf_emits_valid_sides_triggers_and_adaptive_brackets():
    bars = _bars()
    signals = compute_signals(bars)
    active = signals[signals["side"] != ""]

    assert len(active) > 0
    assert set(active["side"]).issubset({"buy", "sell"})
    assert set(active["trigger_id"]).issubset({2, 3, 4, 5, 6, 7})
    assert active["stop_mult"].between(1.4, 3.0).all()
    target_mult = active["tp_rr"] * active["stop_mult"]
    assert target_mult.between(2.8, 6.5).all()


def test_wma_mtf_signals_do_not_use_future_bars():
    bars = _bars()
    full = compute_signals(bars)
    cut_index = bars["5min"].index[-100]
    prefix_bars = {tf: frame.loc[:cut_index] for tf, frame in bars.items()}
    prefix = compute_signals(prefix_bars)
    assert full.loc[cut_index, "side"] == prefix.loc[cut_index, "side"]
    assert full.loc[cut_index, "trigger_id"] == prefix.loc[cut_index, "trigger_id"]
