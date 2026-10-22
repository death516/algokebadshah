"""Vectorized technical indicators using pandas/numpy (Role B).

All functions take/return pandas Series and use Wilder's smoothing where that is
the standard (RSI, ATR, ADX) so values match TradingView / ta-lib conventions.
Pure functions: no I/O, no state.
Also includes Candle-list wrappers (compute_ema, compute_atr, compute_adx, compute_rsi).
"""
from typing import List, Tuple
import numpy as np
import pandas as pd

from app.models import Candle


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (standard span-based, adjust=False)."""
    return series.ewm(span=period, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing = EMA with alpha = 1/period."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, bounded [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 -> RSI 100; avg_gain == 0 -> RSI 0
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(avg_gain != 0.0, 0.0)
    return out.astype(float)


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period).mean()


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Return (middle SMA, upper band, lower band)."""
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return mid, mid + mult * sd, mid - mult * sd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range calculation across high, low, close."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return _wilder(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder), bounded [0, 100]."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    tr = true_range(high, low, close)
    atr_ = _wilder(tr, period)
    plus_di = 100.0 * _wilder(plus_dm, period) / atr_
    minus_di = 100.0 * _wilder(minus_dm, period) / atr_

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    return _wilder(dx, period).astype(float)


# -----------------------------------------------------------------------------
# Candle-list wrappers for high-level callers
# -----------------------------------------------------------------------------

def _candles_to_series(candles: List[Candle]):
    if not candles:
        empty = pd.Series(dtype=float)
        return empty, empty, empty, empty
    high = pd.Series([c.high for c in candles], dtype=float)
    low = pd.Series([c.low for c in candles], dtype=float)
    close = pd.Series([c.close for c in candles], dtype=float)
    volume = pd.Series([c.volume for c in candles], dtype=float)
    return high, low, close, volume


def compute_ema(candles: List[Candle], period: int = 20) -> List[float]:
    """Calculate Exponential Moving Average from Candle list."""
    _, _, close, _ = _candles_to_series(candles)
    if close.empty:
        return []
    return ema(close, period).tolist()


def compute_atr(candles: List[Candle], period: int = 14) -> List[float]:
    """Calculate Average True Range from Candle list."""
    high, low, close, _ = _candles_to_series(candles)
    if close.empty:
        return []
    return atr(high, low, close, period).tolist()


def compute_adx(candles: List[Candle], period: int = 14) -> List[float]:
    """Calculate Average Directional Index from Candle list."""
    high, low, close, _ = _candles_to_series(candles)
    if close.empty:
        return []
    return adx(high, low, close, period).tolist()


def compute_rsi(candles: List[Candle], period: int = 14) -> List[float]:
    """Calculate Relative Strength Index from Candle list."""
    _, _, close, _ = _candles_to_series(candles)
    if close.empty:
        return []
    return rsi(close, period).tolist()
