"""Load the raw 1m parquet and resample to the strategy timeframes.

Resampled frames are cached as parquet so repeated backtests are fast.
"""
import logging
import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger("backtest.data")

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "crypto_BTC_USD_1m.parquet"
CACHE = ROOT / "backtest" / "cache"
DATA_DIR = ROOT / "backtest" / "data" / "btc_1y"
OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _load_json_frame(filename: str, required: tuple[str, ...]) -> pd.DataFrame:
    """Load one of the checked-in BTC JSON series into a UTC-indexed frame."""
    path = DATA_DIR / filename
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = next((raw[k] for k in ("candles", "data", "result", "items")
                    if isinstance(raw.get(k), list)), raw)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must contain a non-empty JSON list")
    frame = pd.DataFrame(raw)
    frame.columns = [str(c).lower().strip() for c in frame.columns]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
    ts = pd.to_numeric(frame.pop("ts"), errors="raise")
    # The supplied series use epoch seconds; allow milliseconds for future imports.
    if ts.abs().median() > 1e11:
        ts = ts / 1000
    frame.index = pd.to_datetime(ts, unit="s", utc=True)
    frame.index.name = "ts"
    frame = frame.loc[:, list(required)].apply(pd.to_numeric, errors="raise")
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def load_funding() -> pd.DataFrame:
    """Return hourly funding rates indexed by their settlement timestamp."""
    return _load_json_frame("funding_1h.json", ("rate",))


def load_mark_prices() -> pd.DataFrame:
    """Return 15m mark-price OHLC data, separate from traded candle prices."""
    return _load_json_frame("mark_15m.json", ("open", "high", "low", "close", "volume"))


@lru_cache(maxsize=1)
def load_backtest_data():
    """Return (15m candles, 1h candles, hourly funding, 15m mark prices)."""
    df15 = _load_json_frame("candles_15m.json", tuple(OHLCV_AGG))
    df1h = _load_json_frame("candles_1h.json", tuple(OHLCV_AGG))
    return df15, df1h, load_funding(), load_mark_prices()


def load_1m() -> pd.DataFrame:
    df = pd.read_parquet(RAW)
    df = df.rename(columns={c: c.lower() for c in df.columns})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df1m.resample(rule, label="left", closed="left").agg(OHLCV_AGG).dropna()
    return out


def load_candles(tf: str = "15min", htf: str = "1h", use_cache: bool = True):
    """Return (df_tf, df_htf) OHLCV frames with a UTC DatetimeIndex."""
    # Prefer the supplied full-year data when present; parquet caches may be stale
    # or belong to an older sample and must not silently override these files.
    json_files = {
        ("15min", "1h"): ("candles_15m.json", "candles_1h.json"),
        ("15m", "1h"): ("candles_15m.json", "candles_1h.json"),
    }
    if (tf, htf) in json_files and all((DATA_DIR / name).exists() for name in json_files[(tf, htf)]):
        return (_load_json_frame(json_files[(tf, htf)][0], tuple(OHLCV_AGG)),
                _load_json_frame(json_files[(tf, htf)][1], tuple(OHLCV_AGG)))
    CACHE.mkdir(exist_ok=True)
    f_tf, f_htf = CACHE / f"{tf}.parquet", CACHE / f"{htf}.parquet"
    if use_cache and f_tf.exists() and f_htf.exists():
        return pd.read_parquet(f_tf), pd.read_parquet(f_htf)
    df1m = load_1m()
    log.info("[data] loaded %d 1m rows %s -> %s", len(df1m), df1m.index[0], df1m.index[-1])
    df_tf, df_htf = resample(df1m, tf), resample(df1m, htf)
    df_tf.to_parquet(f_tf)
    df_htf.to_parquet(f_htf)
    log.info("[data] resampled: %d %s bars, %d %s bars", len(df_tf), tf, len(df_htf), htf)
    return df_tf, df_htf
