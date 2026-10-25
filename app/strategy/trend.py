"""Trend-following strategy implementing the `Strategy` contract (Role B).

Features:
- Dual-timeframe: 15m entry candles aligned with 1h higher-timeframe trend filter.
- Regime-switching: ADX trend vs ranging detection with EMA-cross trending legs and
  mean-reversion RSI/Bollinger legs.
- Vectorized `compute_signals` for high-throughput backtesting.
- Real-time `generate_signal` for the live trading engine loop.
- Pure functions: no network, no hidden state.
"""
from dataclasses import dataclass, replace
from typing import Optional, List

import numpy as np
import pandas as pd

from app.data.indicators import ema, atr, adx, rsi as _rsi, bollinger as _bb
from app.models import Candle, Signal
from app.strategy.base import BaseStrategy


@dataclass(frozen=True)
class TrendParams:
    entry_mode: str = "regime"      # production default; other modes are research baselines
    ema_fast: int = 20
    ema_slow: int = 50
    adx_period: int = 14
    adx_min: float = 20.0
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr: float = 2.0                  # take-profit = rr * risk
    htf_ema: int = 50
    breakout_lookback: int = 20      # Donchian channel length (breakout mode)
    use_htf_filter: bool = True

    # --- regime switch (entry_mode="regime") ---
    adx_trend: float = 22.0          # ADX >= this -> trending -> trend-follow (BTC)
    adx_range: float = 18.0          # ADX <  this -> ranging  -> mean-revert (BTC)
    rsi_period: int = 14
    rsi_os: float = 25.0             # oversold -> fade long (BTC)
    rsi_ob: float = 75.0             # overbought -> fade short (BTC)
    bb_period: int = 20
    bb_mult: float = 2.0
    # per-regime exit plans (ATR multiples; tp_rr NaN = trail out)
    trend_stop_mult: float = 2.0
    trend_trail_mult: float = 4.0
    trend_tp_ceiling_r: float = 6.0  # live bracket TP for trailing legs (executor trails the stop)
    mr_stop_mult: float = 1.5
    mr_tp_rr: float = 1.0            # mean-reversion target in R (bounce to mean)


# Single production configuration shared by paper execution and backtesting.
PRODUCTION_PARAMS = TrendParams(
    entry_mode="regime", ema_fast=20, ema_slow=50,
    adx_trend=22, adx_range=18, rsi_os=25, rsi_ob=75,
    trend_stop_mult=2.0, trend_trail_mult=4.0,
    mr_stop_mult=1.5, mr_tp_rr=1.0,
)

# Asset-specific presets selected from the 2025-10-25 through 2026-10-25
# 15m/1h parameter sweep. BTC retains the original production configuration.
ASSET_PRODUCTION_PARAMS = {
    "BTCUSD": PRODUCTION_PARAMS,
    "ETHUSD": replace(
        PRODUCTION_PARAMS,
        adx_trend=28, adx_range=20, rsi_os=30, rsi_ob=70,
        trend_trail_mult=4.0, mr_tp_rr=1.0,
    ),
    "SOLUSD": replace(
        PRODUCTION_PARAMS,
        adx_trend=28, adx_range=20, rsi_os=25, rsi_ob=75,
        trend_trail_mult=4.0, mr_tp_rr=1.0,
    ),
}


def production_params_for(symbol: str) -> TrendParams:
    """Return the backtested production preset for a supported USD market."""
    normalized = str(symbol or "BTCUSD").upper().replace("/", "").replace("-", "")
    if not normalized.endswith("USD"):
        normalized += "USD"
    return ASSET_PRODUCTION_PARAMS.get(normalized, PRODUCTION_PARAMS)


OHLCV = ["open", "high", "low", "close", "volume"]


def _candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    df = pd.DataFrame([c.model_dump() for c in candles])
    df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df[OHLCV]


def timeframe_delta(timeframe: str) -> pd.Timedelta:
    """Parse timeframe labels (e.g. 15m, 1h, 4h, 1d) as positive durations."""
    import re
    match = re.fullmatch(r"\s*(\d+)\s*(m|min|h|hr|d|day|w|week)s?\s*", str(timeframe), re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Use labels such as 5m, 30m, 2h, or 1d.")
    unit = match.group(2).lower()
    seconds = {"m": 60, "min": 60, "h": 3600, "hr": 3600,
               "d": 86400, "day": 86400, "w": 604800, "week": 604800}[unit]
    return pd.Timedelta(seconds=int(match.group(1)) * seconds)


def _align_htf(df15: pd.DataFrame, df1h: pd.DataFrame, htf_ema: int,
               htf_timeframe: str = "1h") -> pd.DataFrame:
    """Attach the last CLOSED higher-timeframe EMA/close without lookahead."""
    htf = pd.DataFrame(index=df1h.index)
    htf["htf_close"] = df1h["close"]
    htf["htf_ema"] = ema(df1h["close"], htf_ema)
    htf["avail"] = (htf.index + timeframe_delta(htf_timeframe)).as_unit("ns")  # close time
    htf = htf.sort_values("avail")
    left = df15.sort_index().copy()
    left["_ts"] = left.index.as_unit("ns")
    merged = pd.merge_asof(
        left,
        htf[["avail", "htf_close", "htf_ema"]],
        left_on="_ts",
        right_on="avail",
        direction="backward",
    )
    merged.index = left.index
    return merged


def compute_signals(df15: pd.DataFrame, df1h: pd.DataFrame, p: TrendParams = TrendParams(),
                    htf_timeframe: str = "1h") -> pd.DataFrame:
    """Return df15 annotated with indicators and per-bar signal columns.

    Added columns: ema_fast, ema_slow, adx, atr, htf_close, htf_ema,
    side ('buy'/'sell'/''), entry, stop, tp, reason.
    `side` is the signal for a bar that has CLOSED at that timestamp; the engine
    acts on the next bar.
    """
    df = df15.copy()
    df["ema_fast"] = ema(df["close"], p.ema_fast)
    df["ema_slow"] = ema(df["close"], p.ema_slow)
    df["adx"] = adx(df["high"], df["low"], df["close"], p.adx_period)
    df["atr"] = atr(df["high"], df["low"], df["close"], p.atr_period)
    df["rsi"] = _rsi(df["close"], p.rsi_period)
    df["bb_mid"], df["bb_up"], df["bb_lo"] = _bb(df["close"], p.bb_period, p.bb_mult)

    htf = _align_htf(df, df1h, p.htf_ema, htf_timeframe)
    df["htf_close"] = htf["htf_close"].values
    df["htf_ema"] = htf["htf_ema"].values

    fast, slow = df["ema_fast"], df["ema_slow"]
    df["side"] = ""
    # per-signal exit plan: NaN -> engine uses its own cfg default
    df["stop_mult"] = np.nan
    df["tp_rr"] = np.nan
    df["trail_mult"] = np.nan
    df["reason"] = ""

    def _apply(mask_long, mask_short, stop_mult, tp_rr, trail_mult, tag):
        for mask, sd in ((mask_long, "buy"), (mask_short, "sell")):
            m = mask & (df["side"] == "")   # don't overwrite an existing signal
            df.loc[m, "side"] = sd
            df.loc[m, "stop_mult"] = stop_mult
            df.loc[m, "tp_rr"] = tp_rr
            df.loc[m, "trail_mult"] = trail_mult
            df.loc[m, "reason"] = tag + (" long" if sd == "buy" else " short") \
                + " ADX" + df.loc[m, "adx"].round(0).astype(int).astype(str) \
                + " RSI" + df.loc[m, "rsi"].round(0).astype(int).astype(str)

    if p.entry_mode == "regime":
        trending = df["adx"] >= p.adx_trend
        ranging = df["adx"] < p.adx_range
        # trend-follow leg (EMA cross confirmed by trend, ATR trail out)
        t_long = trending & (fast > slow) & (fast.shift(1) <= slow.shift(1))
        t_short = trending & (fast < slow) & (fast.shift(1) >= slow.shift(1))
        _apply(t_long, t_short, p.trend_stop_mult, np.nan, p.trend_trail_mult, "TREND")
        # Match the ALGO COMPETITION strategy: fade an outer Bollinger band
        # when RSI confirms an extreme during the low-ADX ranging regime.
        mr_long = ranging & (df["close"] < df["bb_lo"]) & (df["rsi"] < p.rsi_os)
        mr_short = ranging & (df["close"] > df["bb_up"]) & (df["rsi"] > p.rsi_ob)
        _apply(mr_long, mr_short, p.mr_stop_mult, p.mr_tp_rr, 0.0, "MR")
    else:
        strong = df["adx"] > p.adx_min
        htf_up = (df["htf_close"] > df["htf_ema"]) if p.use_htf_filter else True
        htf_dn = (df["htf_close"] < df["htf_ema"]) if p.use_htf_filter else True
        if p.entry_mode == "breakout":
            hi = df["high"].rolling(p.breakout_lookback).max().shift(1)
            lo = df["low"].rolling(p.breakout_lookback).min().shift(1)
            long_trig = (df["close"] > hi) & (fast > slow)
            short_trig = (df["close"] < lo) & (fast < slow)
        elif p.entry_mode == "pullback":
            long_trig = (fast > slow) & (df["close"] > fast) & (df["close"].shift(1) <= fast.shift(1))
            short_trig = (fast < slow) & (df["close"] < fast) & (df["close"].shift(1) >= fast.shift(1))
        else:  # ema_cross
            long_trig = (fast > slow) & (fast.shift(1) <= slow.shift(1))
            short_trig = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        _apply(long_trig & strong & htf_up, short_trig & strong & htf_dn,
               np.nan, np.nan, np.nan, p.entry_mode)

    # Absolute SL/TP prices for the live Signal, per leg.
    sm = np.where(df["stop_mult"].notna(), df["stop_mult"], p.atr_stop_mult)
    trails = df["trail_mult"].fillna(0.0) > 0
    rr_eff = np.where(df["tp_rr"].notna(), df["tp_rr"],
                      np.where(trails, p.trend_tp_ceiling_r, p.rr))
    stop_dist = sm * df["atr"].to_numpy()
    is_buy = (df["side"] == "buy").to_numpy()
    is_sell = (df["side"] == "sell").to_numpy()
    entry = df["close"].to_numpy()
    df["entry"] = entry
    df["stop"] = np.where(is_buy, entry - stop_dist,
                          np.where(is_sell, entry + stop_dist, entry))
    df["tp"] = np.where(is_buy, entry + rr_eff * stop_dist,
                        np.where(is_sell, entry - rr_eff * stop_dist, entry))
    return df


class TrendStrategy(BaseStrategy):
    """Implements the Strategy Protocol / BaseStrategy for production execution."""

    execution_timeframe = "15m"

    def __init__(self, symbol: str = "BTCUSD", params: Optional[TrendParams] = None,
                 htf_timeframe: str = "1h"):
        self.symbol = symbol
        self.p = params or production_params_for(symbol)
        self.htf_timeframe = htf_timeframe
        self._min_bars = max(self.p.ema_slow, self.p.adx_period) + 5

    def set_symbol(self, symbol: str) -> None:
        """Select the matching production preset when the dashboard changes market."""
        self.symbol = str(symbol).upper()
        self.p = production_params_for(self.symbol)
        self._min_bars = max(self.p.ema_slow, self.p.adx_period) + 5

    def generate_signal(self, candles: List[Candle], htf_candles: List[Candle]) -> Optional[Signal]:
        if len(candles) < self._min_bars or len(htf_candles) < self.p.htf_ema + 1:
            return None
        df15 = _candles_to_df(candles)
        df1h = _candles_to_df(htf_candles)
        sig = compute_signals(df15, df1h, self.p, self.htf_timeframe)
        last = sig.iloc[-1]
        if not last["side"]:
            return None
        return Signal(
            symbol=self.symbol,
            side=last["side"],
            entry_price=float(last["entry"]),
            stop_loss=float(last["stop"]),
            take_profit=float(last["tp"]),
            reason=str(last["reason"]),
            ts=int(candles[-1].ts),
            atr=float(last["atr"]),
            trail_atr_mult=float(last["trail_mult"] if pd.notna(last["trail_mult"]) else 0.0),
            use_take_profit=bool(pd.notna(last["tp_rr"])),
        )


# Backward-compatible alias for existing tests and contracts
TrendFollowingStrategy = TrendStrategy
