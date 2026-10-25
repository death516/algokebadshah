"""Download Delta Exchange BTC OHLCV candles for backtesting.

Delta limits each history response to 2,000 candles. This script divides the
requested UTC time range into smaller requests, removes boundary duplicates,
and writes each timeframe as an app-compatible JSON candle list.

Examples (from the repository root)::

    python -m backtest.download_delta_candles
    python -m backtest.download_delta_candles --start 2025-01-01 --end 2026-01-01
    python -m backtest.download_delta_candles --output-dir backtest/data/uploads
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.india.delta.exchange/v2/history/candles"
TIMEFRAMES = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}
MAX_CANDLES_PER_REQUEST = 2000
LOG = logging.getLogger("download_delta_candles")


def _parse_utc(value: str) -> datetime:
    """Parse a date or ISO timestamp; date-only inputs mean midnight UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_timeframe(
    client: httpx.Client,
    api_url: str,
    symbol: str,
    resolution: str,
    start_ts: int,
    end_ts: int,
    checkpoint_path: Path | None = None,
    partial_path: Path | None = None,
    resume_default_range: bool = False,
) -> list[dict[str, Any]]:
    step = TIMEFRAMES[resolution]
    # Align to candle boundaries so adjacent paged requests have no gaps.
    cursor = (start_ts // step) * step
    final_ts = (end_ts // step) * step
    candles: dict[int, dict[str, Any]] = {}

    # Keep completed pages on disk so a long 1m download can resume after a
    # dropped connection or interrupted process instead of starting over.
    if checkpoint_path and partial_path and checkpoint_path.exists() and partial_path.exists():
        try:
            state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            same_request = state.get("symbol") == symbol and state.get("resolution") == resolution
            same_start = int(state.get("start_ts", -1)) == cursor
            can_resume_default = resume_default_range and same_request
            if same_request and (same_start or can_resume_default):
                cursor = int(state.get("next_ts", cursor))
                if can_resume_default:
                    start_ts = int(state["start_ts"])
                with partial_path.open("r", encoding="utf-8") as partial:
                    for line in partial:
                        if line.strip():
                            row = json.loads(line)
                            candles[int(row["ts"])] = row
                LOG.info("%s: resuming at %s (%d candles checkpointed)", resolution,
                         datetime.fromtimestamp(cursor, timezone.utc).isoformat(), len(candles))
            else:
                raise RuntimeError(
                    f"A checkpoint exists for a different {resolution} date range in "
                    f"{checkpoint_path.parent}. Resume with the same --start/--end, "
                    "or choose a new --output-dir."
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Could not read {resolution} download checkpoint: {exc}") from exc

    while cursor <= final_ts:
        request_end = min(final_ts, cursor + (MAX_CANDLES_PER_REQUEST - 1) * step)
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": cursor,
            "end": request_end,
        }
        response = None
        for attempt in range(8):
            try:
                response = client.get(api_url, params=params)
                if response.status_code == 429:
                    delay_ms = int(response.headers.get("X-RATE-LIMIT-RESET", "1000"))
                    time.sleep(min(max(delay_ms / 1000, 1.0), 60.0))
                    continue
                if response.status_code in (500, 502, 503, 504):
                    if attempt == 7:
                        response.raise_for_status()
                    delay = min(2 ** attempt, 30)
                    LOG.warning("%s: Delta returned HTTP %s; retrying in %ss",
                                resolution, response.status_code, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                break
            except httpx.TransportError as exc:
                if attempt == 7:
                    raise
                delay = min(2 ** attempt, 30)
                LOG.warning("%s: connection interrupted (%s); retrying in %ss",
                            resolution, exc, delay)
                time.sleep(delay)
        if response is None or response.status_code == 429:
            raise RuntimeError(f"Delta rate limit did not clear for {resolution}")
        payload = response.json()
        if payload.get("success") is False:
            raise RuntimeError(f"Delta rejected {resolution} request: {payload}")

        rows = payload.get("result", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected Delta candle response for {resolution}")
        page_candles = []
        for row in rows:
            ts = int(row.get("time", row.get("ts", 0)))
            if ts > 100_000_000_000:
                ts = ts // (1_000_000 if ts > 100_000_000_000_000 else 1_000)
            if start_ts <= ts <= end_ts:
                candle = {
                    "ts": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0)),
                }
                candles[ts] = candle
                page_candles.append(candle)
        if partial_path and checkpoint_path:
            with partial_path.open("a", encoding="utf-8", newline="\n") as partial:
                for candle in page_candles:
                    partial.write(json.dumps(candle, separators=(",", ":")) + "\n")
            checkpoint = {
                "symbol": symbol,
                "resolution": resolution,
                "start_ts": start_ts,
                "end_ts": final_ts,
                "next_ts": request_end + step,
            }
            temp_checkpoint = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
            temp_checkpoint.write_text(json.dumps(checkpoint), encoding="utf-8")
            temp_checkpoint.replace(checkpoint_path)
        LOG.info("%s: fetched through %s (%d unique candles)", resolution,
                 datetime.fromtimestamp(request_end, timezone.utc).isoformat(), len(candles))
        cursor = request_end + step
        time.sleep(0.1)  # Gentle pacing reduces transient disconnects on long 1m runs.

    return [candles[ts] for ts in sorted(candles)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSD", help="Delta symbol (default: BTCUSD)")
    parser.add_argument("--start", help="Start date/time UTC, e.g. 2025-01-01 (default: one year ago)")
    parser.add_argument("--end", help="End date/time UTC (default: now)")
    parser.add_argument(
        "--output-dir", default="backtest/data/delta_btc",
        help="Output folder, relative to project root unless absolute",
    )
    parser.add_argument("--base-url", default="https://api.india.delta.exchange",
                        help="Delta API base URL (default: India production public API)")
    parser.add_argument("--timeframes", nargs="+", choices=TIMEFRAMES,
                        default=list(TIMEFRAMES), help="Timeframes to download")
    args = parser.parse_args()

    end = _parse_utc(args.end) if args.end else datetime.now(timezone.utc)
    start = _parse_utc(args.start) if args.start else end - timedelta(days=365)
    if start >= end:
        parser.error("--start must be earlier than --end")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_url = args.base_url.rstrip("/") + "/v2/history/candles"

    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    LOG.info("Downloading %s candles from %s through %s", args.symbol,
             start.isoformat(), end.isoformat())
    try:
        with httpx.Client(timeout=30, headers={"Accept": "application/json"}) as client:
            for resolution in args.timeframes:
                path = output_dir / f"candles_{resolution}.json"
                checkpoint_path = output_dir / f"candles_{resolution}.checkpoint.json"
                partial_path = output_dir / f"candles_{resolution}.partial.jsonl"
                candles = _fetch_timeframe(
                    client, api_url, args.symbol, resolution, start_ts, end_ts,
                    checkpoint_path=checkpoint_path,
                    partial_path=partial_path,
                    resume_default_range=args.start is None,
                )
                if not candles:
                    raise RuntimeError(f"Delta returned no candles for {args.symbol} ({resolution})")
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(json.dumps(candles, separators=(",", ":")), encoding="utf-8")
                temp_path.replace(path)
                checkpoint_path.unlink(missing_ok=True)
                partial_path.unlink(missing_ok=True)
                LOG.info("Saved %s rows to %s", len(candles), path)
    except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
        LOG.error("Download failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
