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


def _resolve_data_dir(symbol: str = "BTCUSD") -> Path:
    """Resolve the newest complete local dataset for the requested market."""
    normalized_symbol = symbol.upper().removesuffix("USD")
    if normalized_symbol == "BTC" and all((DATA_DIR / name).is_file() for name in ("candles_15m.json", "candles_1h.json")):
        return DATA_DIR

    prefix = f"delta_{normalized_symbol.lower()}"
    candidates = [
        path for path in DATA_DIR.parent.glob(f"{prefix}*")
        if (path / "candles_15m.json").is_file() and (path / "candles_1h.json").is_file()
    ]
    if candidates:
        return max(candidates, key=lambda path: max(
            (path / "candles_15m.json").stat().st_mtime,
            (path / "candles_1h.json").stat().st_mtime,
        ))
    return DATA_DIR if normalized_symbol == "BTC" else DATA_DIR.parent / prefix


def _load_json_frame(
    filename: str,
    required: tuple[str, ...],
    data_dir: Path | None = None,
    symbol: str | None = None,
) -> pd.DataFrame:
    """Load one of the checked-in BTC JSON series into a UTC-indexed frame."""
    path = (data_dir or _resolve_data_dir()) / filename
    if not path.is_file() and symbol and filename.startswith("candles_"):
        timeframe = filename.removeprefix("candles_").removesuffix(".json")
        symbol_path = path.parent / f"{symbol.upper()}_{timeframe}.json"
        if symbol_path.is_file():
            path = symbol_path
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


def load_funding(data_dir: Path | None = None) -> pd.DataFrame:
    """Return hourly funding rates, or an empty series when not downloaded."""
    directory = data_dir or _resolve_data_dir()
    if not (directory / "funding_1h.json").is_file():
        log.warning("[data] no funding_1h.json in %s; backtest funding will be zero", directory)
        return pd.DataFrame({"rate": []}, index=pd.DatetimeIndex([], tz="UTC"))
    return _load_json_frame("funding_1h.json", ("rate",), directory)


def load_mark_prices(data_dir: Path | None = None, traded_candles: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return mark OHLC; fall back to traded OHLC when no mark series exists."""
    directory = data_dir or _resolve_data_dir()
    if not (directory / "mark_15m.json").is_file():
        log.warning("[data] no mark_15m.json in %s; using traded 15m candles as mark prices", directory)
        return traded_candles.copy() if traded_candles is not None else _load_json_frame(
            "candles_15m.json", tuple(OHLCV_AGG), directory
        )
    return _load_json_frame("mark_15m.json", ("open", "high", "low", "close", "volume"), directory)


@lru_cache(maxsize=8)
def load_backtest_data(data_dir: str | Path | None = None, symbol: str = "BTCUSD"):
    """Return (15m candles, 1h candles, hourly funding, 15m mark prices)."""
    directory = Path(data_dir) if data_dir is not None else _resolve_data_dir(symbol)
    df15 = _load_json_frame("candles_15m.json", tuple(OHLCV_AGG), directory, symbol)
    df1h = _load_json_frame("candles_1h.json", tuple(OHLCV_AGG), directory, symbol)
    return df15, df1h, load_funding(directory), load_mark_prices(directory, df15)


def load_wma_backtest_data(data_dir: str | Path | None = None, symbol: str = "BTCUSD"):
    """Return the close-time-indexed 5m/15m/1h bars required by WMA-MTF."""
    directory = Path(data_dir) if data_dir is not None else _resolve_data_dir(symbol)
    bars = {}
    for label, filename in (("5min", "candles_5m.json"),
                            ("15min", "candles_15m.json"),
                            ("1h", "candles_1h.json")):
        frame = _load_json_frame(filename, tuple(OHLCV_AGG), directory, symbol)
        seconds = {"5min": 300, "15min": 900, "1h": 3600}[label]
        # Uploaded/Delta JSON timestamps are bar opens; the WMA rules align
        # signals and higher-timeframe values at each bar's close.
        frame.index = frame.index + pd.Timedelta(seconds=seconds)
        bars[label] = frame
    return bars


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
    data_dir = _resolve_data_dir()
    if (tf, htf) in json_files and all((data_dir / name).exists() for name in json_files[(tf, htf)]):
        return (_load_json_frame(json_files[(tf, htf)][0], tuple(OHLCV_AGG), data_dir),
                _load_json_frame(json_files[(tf, htf)][1], tuple(OHLCV_AGG), data_dir))
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
