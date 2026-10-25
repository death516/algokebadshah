"""Download one year of a USDT USD-M perpetual's candles and funding history.

Writes candles_5m.json, candles_15m.json, candles_1h.json, and funding_1h.json
for the WMA multi-timeframe backtest. No API key is required.
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://fapi.binance.com"
DATA_ROOT = Path(__file__).resolve().parent / "data"


def get_json(path: str, params: dict):
    url = f"{BASE_URL}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "algo-backtest-data-downloader/1.0"})
    for attempt in range(6):
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == 5:
                raise RuntimeError(f"Binance request failed: {url}: {exc}") from exc
            time.sleep(min(2 ** attempt, 16))


def download_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = get_json("/fapi/v1/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": 1500,
        })
        if not batch:
            break
        for row in batch:
            open_ms = int(row[0])
            # Exclude the still-forming candle at the end of the download window.
            if start_ms <= open_ms < end_ms and int(row[6]) < end_ms:
                rows.append({
                    "ts": open_ms // 1000,
                    "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]),
                    "volume": float(row[5]),
                })
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            raise RuntimeError(f"Binance returned a non-advancing {interval} page")
        cursor = next_cursor
        print(f"{interval}: downloaded {len(rows):,} candles", end="\r", flush=True)
        time.sleep(0.08)
    print(f"{interval}: downloaded {len(rows):,} candles")
    return rows


def download_funding(symbol: str, start_ms: int, end_ms: int):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = get_json("/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cursor,
            "endTime": end_ms, "limit": 1000,
        })
        if not batch:
            break
        for row in batch:
            event_ms = int(row["fundingTime"])
            if start_ms <= event_ms <= end_ms:
                # Binance publishes fundingRate as a decimal fraction; the backtest
                # expects percentage points, so 0.0001 is stored as 0.01 (0.01%).
                rows.append({"ts": event_ms // 1000,
                             "rate": float(row["fundingRate"]) * 100.0})
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            raise RuntimeError("Binance returned a non-advancing funding page")
        cursor = next_cursor
        print(f"funding: downloaded {len(rows):,} records", end="\r", flush=True)
        time.sleep(0.08)
    print(f"funding: downloaded {len(rows):,} records")
    return rows


def save_json(path: Path, rows):
    path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ETHUSDT",
                        help="USDT perpetual symbol (default: ETHUSDT)")
    parser.add_argument("--out-dir", type=Path,
                        help="output folder (default: backtest/data/<asset>_1y)")
    parser.add_argument("--days", type=int, default=365,
                        help="history length in days (default: 365)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be a positive integer")
    symbol = args.symbol.strip().upper()
    if not symbol.endswith("USDT") or not symbol[:-4].isalnum():
        parser.error("--symbol must be a USDT perpetual symbol such as SOLUSDT")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out_dir = (args.out_dir or DATA_ROOT / f"{symbol[:-4].lower()}_1y").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {symbol} USD-M perpetual data from {start.isoformat()} to {end.isoformat()}")
    save_json(out_dir / "candles_5m.json", download_klines(symbol, "5m", start_ms, end_ms))
    save_json(out_dir / "candles_15m.json", download_klines(symbol, "15m", start_ms, end_ms))
    save_json(out_dir / "candles_1h.json", download_klines(symbol, "1h", start_ms, end_ms))
    save_json(out_dir / "funding_1h.json", download_funding(symbol, start_ms, end_ms))
    print(f"Saved dataset to {out_dir}")


if __name__ == "__main__":
    main()
