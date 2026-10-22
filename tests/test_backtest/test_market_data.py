import pandas as pd
import pytest

from backtest.data import load_backtest_data
from backtest.engine import BTConfig, run_backtest
from app.api.routes import get_backtest_candles, run_backtest_endpoint


def test_bundled_full_year_data_loads_with_aligned_mark_prices():
    candles_15m, candles_1h, funding, mark = load_backtest_data()

    assert len(candles_15m) > 30_000
    assert len(candles_1h) > 8_000
    assert len(funding) > 8_000
    assert len(mark) == len(candles_15m)
    assert candles_15m.index.tz is not None
    assert candles_15m.index.equals(mark.index)
    assert "rate" in funding.columns


def test_backtest_api_uses_bundle_and_exposes_historical_chart_candles():
    candles = get_backtest_candles(resolution="15m", limit=3)
    result = run_backtest_endpoint()

    assert len(candles) == 3
    assert candles[-1]["ts"] > candles[0]["ts"]
    assert result["status"] == "ok"
    assert result["bars_analyzed"] > 30_000
    assert len(result["trades"]) == result["metrics"]["total_trades"]
    assert "total_funding_pnl" in result["metrics"]
    if result["trades"]:
        trade = result["trades"][-1]
        assert trade["entry_ts"] < trade["exit_ts"]


@pytest.mark.parametrize(("side", "funding_sign"), [("buy", -1), ("sell", 1)])
def test_funding_is_charged_to_longs_and_credited_to_shorts(side, funding_sign):
    index = pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC")
    sig = pd.DataFrame({
        "open": [100.0] * 4,
        "high": [101.0] * 4,
        "low": [99.0] * 4,
        "close": [100.0] * 4,
        "atr": [1.0] * 4,
        "side": [side, "", "", ""],
        "reason": ["test", "", "", ""],
    }, index=index)
    mark = pd.DataFrame({"open": [100.0, 100.0, 110.0, 100.0],
                         "close": [100.0, 100.0, 110.0, 100.0]}, index=index)
    funding = pd.Series([0.0, 0.0, 0.001, 0.0], index=index)
    cfg = BTConfig(taker_fee_pct=0.0, slippage_bps=0.0,
                   daily_loss_pct=1e9, max_dd_pct=1e9)

    trades, _, state = run_backtest(sig, cfg, mark_prices=mark, funding=funding)

    expected_payment = funding_sign * ((10_000 * 0.01) / 1.5) * 110.0 * 0.001
    assert len(trades) == 1
    assert trades.iloc[0]["funding_pnl"] == round(expected_payment, 2)
    assert trades.iloc[0]["pnl"] == round(expected_payment, 2)
    assert state["final_equity"] == pytest.approx(10_000 + expected_payment)
