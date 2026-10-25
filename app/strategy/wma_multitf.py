"""Multi-timeframe WMA-cross strategy adapted from ALGO COMPETITION.

The execution grid is 5m. Six WMA cross combinations may trigger on 5m or
15m bars, using only completed 15m/1h values. Longs require RSI and volume
confirmation; shorts follow the supplied strategy's intentionally asymmetric
rules. Brackets adapt to each signal timeframe's relative volatility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from app.models import Candle, Signal
from app.strategy.base import BaseStrategy


TF_RULES = {"5m": "5min", "15m": "15min", "1h": "1h"}
TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}
COMBOS = (
    (2, "15min", "1h", 50, None),
    (3, "15min", "15min", 50, "1h"),
    (4, "15min", "15min", 20, "1h"),
    (5, "5min", "5min", 50, "15min"),
    (6, "5min", "5min", 20, "15min"),
    (7, "5min", "15min", 50, None),
)


@dataclass(frozen=True)
class WmaMultiTfParams:
    wma_fast: int = 20
    wma_slow: int = 50
    rsi_period: int = 14
    volume_sma_period: int = 20
    atr_period: int = 14
    volatility_median_days: int = 30
    sl_base: float = 2.0
    tp_base: float = 4.0
    sl_exp: float = -0.25
    tp_exp: float = 0.15
    sl_min: float = 1.4
    sl_max: float = 3.0
    tp_min: float = 2.8
    tp_max: float = 6.5


DEFAULT_PARAMS = WmaMultiTfParams()


def _wma(values: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1.0, period + 1.0)
    return values.rolling(period, min_periods=period).apply(
        lambda window: float(np.dot(window, weights) / weights.sum()), raw=True
    )


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous = frame["close"].shift(1)
    tr = pd.concat(
        [frame["high"] - frame["low"],
         (frame["high"] - previous).abs(),
         (frame["low"] - previous).abs()], axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff().fillna(0.0)
    up = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    down = (-delta).clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    ratio = up / down.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + ratio)).astype(float)


def _candle_frame(candles: Sequence[Candle], timeframe: str) -> pd.DataFrame:
    rows = [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in candles]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC"))
    frame = pd.DataFrame(rows)
    # Candle.ts is candle-open time. Index at close time so ffill/asof only sees
    # higher-timeframe candles that have actually finished.
    frame.index = (pd.to_datetime(frame.pop("ts"), unit="s", utc=True)
                   + pd.Timedelta(seconds=TF_SECONDS[timeframe]))
    return frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()


def _bars_from_candles(candles_by_tf: Mapping[str, Sequence[Candle]]) -> dict[str, pd.DataFrame]:
    bars = {}
    for label, rule in TF_RULES.items():
        rows = candles_by_tf.get(label, ())
        if rows:
            bars[rule] = _candle_frame(rows, label)
    if "5min" not in bars:
        raise ValueError("WMA strategy requires 5m candles")
    # A 15m series may be built from completed 5m bars if the feed omits it.
    if "15min" not in bars:
        bars["15min"] = bars["5min"].resample("15min", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna()
    if "1h" not in bars:
        raise ValueError("WMA strategy requires 1h candles")
    return bars


def compute_signals(bars: Mapping[str, pd.DataFrame],
                    params: WmaMultiTfParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """Create 5m-indexed entry signals and per-signal adaptive bracket data."""
    required = {"5min", "15min", "1h"}
    if not required.issubset(bars):
        raise ValueError("WMA strategy requires 5m, 15m, and 1h bars")
    base = bars["5min"].sort_index()
    if base.empty:
        return pd.DataFrame(index=base.index)

    wmas: dict[tuple[str, int], pd.Series] = {}
    volume_ok: dict[str, pd.Series] = {}
    for tf, frame in bars.items():
        for length in {length for _, _, wtf, length, _ in COMBOS if wtf == tf}:
            wmas[(tf, length)] = _wma(frame["close"], length)
        volume_average = frame["volume"].rolling(params.volume_sma_period,
                                                  min_periods=params.volume_sma_period).mean()
        volume_ok[tf] = frame["volume"] > volume_average

    long_base = np.zeros(len(base), dtype=bool)
    short_base = np.zeros(len(base), dtype=bool)
    long_trigger = np.zeros(len(base), dtype=np.int8)
    short_trigger = np.zeros(len(base), dtype=np.int8)
    for trigger_id, candle_tf, wma_tf, length, gate_tf in COMBOS:
        frame = bars[candle_tf]
        w = wmas[(wma_tf, length)].reindex(frame.index, method="ffill")
        up = (frame["open"] < w) & (frame["close"] > w)
        down = (frame["open"] > w) & (frame["close"] < w)
        if gate_tf:
            gate = wmas[(gate_tf, params.wma_slow)].reindex(frame.index, method="ffill")
            up &= frame["close"] > gate
            down &= frame["close"] < gate

        rsi_ok = _rsi(frame["close"], params.rsi_period) > 50.0
        vol_ok = volume_ok[wma_tf].reindex(frame.index, method="ffill").fillna(False)
        # Asymmetric filters are intentional in the provided strategy: longs
        # need RSI/volume confirmation; shorts do not.
        long_signal = (up & rsi_ok & vol_ok).reindex(base.index, fill_value=False).to_numpy(bool)
        short_signal = down.reindex(base.index, fill_value=False).to_numpy(bool)
        take_long = long_signal & (long_trigger == 0)
        take_short = short_signal & (short_trigger == 0)
        long_trigger[take_long] = trigger_id
        short_trigger[take_short] = trigger_id
        long_base |= long_signal
        short_base |= short_signal

    both = long_base & short_base
    side = np.where(both, 0, np.where(long_base, 1, np.where(short_base, -1, 0))).astype(np.int8)
    trigger = np.where(side > 0, long_trigger, np.where(side < 0, short_trigger, 0)).astype(np.int8)

    bracket = {}
    median_windows = {"5min": params.volatility_median_days * 24 * 12,
                      "15min": params.volatility_median_days * 24 * 4}
    for tf, window in median_windows.items():
        frame = bars[tf]
        atr = _atr(frame, params.atr_period)
        relative_vol = atr / frame["close"].replace(0.0, np.nan)
        median = relative_vol.rolling(window, min_periods=max(1, window // 4)).median()
        z = (relative_vol / median.replace(0.0, np.nan)).clip(0.5, 2.0).fillna(1.0)
        sl_k = (params.sl_base * z.pow(params.sl_exp)).clip(params.sl_min, params.sl_max)
        tp_k = (params.tp_base * z.pow(params.tp_exp)).clip(params.tp_min, params.tp_max)
        bracket[tf] = tuple(series.reindex(base.index, method="ffill").to_numpy(float)
                            for series in (atr, sl_k, tp_k))

    a5, sl5, tp5 = bracket["5min"]
    a15, sl15, tp15 = bracket["15min"]
    use5 = trigger >= 5
    atr_values = np.where(use5, a5, a15)
    sl_values = np.where(use5, sl5, sl15)
    tp_values = np.where(use5, tp5, tp15)
    out = base.copy()
    out["side"] = np.where(side > 0, "buy", np.where(side < 0, "sell", ""))
    out["trigger_id"] = trigger
    out["atr"] = atr_values
    out["stop_mult"] = sl_values
    out["tp_rr"] = tp_values / np.maximum(sl_values, 1e-12)
    out["trail_mult"] = 0.0
    out["reason"] = np.where(trigger > 0, "WMA multi-timeframe trigger " + trigger.astype(str), "")
    return out


class WmaMultiTfStrategy(BaseStrategy):
    """ALGO COMPETITION main WMA strategy on a 5m execution grid."""

    required_timeframes = ("5m", "15m", "1h")
    execution_timeframe = "5m"
    timeframe_limits = {"5m": 100, "15m": 100, "1h": 100}

    def __init__(self, symbol: str = "BTCUSD", params: WmaMultiTfParams = DEFAULT_PARAMS):
        self.symbol = symbol
        self.p = params

    def generate_signal_multi(self, candles_by_tf: Mapping[str, Sequence[Candle]]) -> Optional[Signal]:
        bars = _bars_from_candles(candles_by_tf)
        result = compute_signals(bars, self.p)
        if result.empty:
            return None
        row = result.iloc[-1]
        side = row["side"]
        atr = float(row["atr"])
        stop_mult = float(row["stop_mult"])
        tp_rr = float(row["tp_rr"])
        entry = float(row["close"])
        if not side or not np.isfinite(atr) or atr <= 0:
            return None
        distance = stop_mult * atr
        direction = 1.0 if side == "buy" else -1.0
        trigger = int(row["trigger_id"])
        return Signal(
            symbol=self.symbol,
            side=side,
            entry_price=entry,
            stop_loss=entry - direction * distance,
            take_profit=entry + direction * tp_rr * distance,
            reason=f"{row['reason']} ({'5m' if trigger >= 5 else '15m'} entry)",
            ts=int(result.index[-1].timestamp()) - TF_SECONDS["5m"],
            atr=atr,
        )

    def generate_signal(self, candles: Sequence[Candle], htf_candles: Sequence[Candle]) -> Optional[Signal]:
        """Protocol fallback; the engine supplies all required frames via multi API."""
        return self.generate_signal_multi({"5m": candles, "1h": htf_candles})
