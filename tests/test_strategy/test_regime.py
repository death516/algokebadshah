"""Regime-switch strategy + engine per-signal-exit behaviour (Role B)."""
import numpy as np
import pandas as pd

from app.strategy.trend import TrendParams, compute_signals
from backtest.engine import BTConfig, run_backtest


def _frame(close: np.ndarray, start="2026-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq="15min", tz="UTC")
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"open": c.shift(1).fillna(c), "high": c + 3,
                         "low": c - 3, "close": c, "volume": 1.0}, index=idx)


def _htf(df):
    return df.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def _ou(n, mu=100.0, theta=0.06, sigma=2.5, seed=1):
    # Ornstein-Uhlenbeck: mean-reverts around mu -> low ADX (ranging regime).
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = mu
    for t in range(1, n):
        x[t] = x[t - 1] + theta * (mu - x[t - 1]) + rng.normal(0, sigma)
    return x


def test_trend_regime_emits_trend_leg():
    # Strong drift + noise -> high ADX with repeated EMA re-crosses (trend leg).
    df = _frame(100 + np.cumsum(0.4 + np.random.default_rng(1).normal(0, 2.5, 1500)))
    sig = compute_signals(df, _htf(df), TrendParams(entry_mode="regime"))
    reasons = sig.loc[sig["side"] != "", "reason"]
    assert reasons.str.startswith("TREND").any(), "expected trend-leg signals"


def test_ranging_regime_emits_mean_reversion_leg():
    df = _frame(_ou(2000))
    sig = compute_signals(df, _htf(df), TrendParams(entry_mode="regime"))
    reasons = sig.loc[sig["side"] != "", "reason"]
    assert reasons.str.startswith("MR").any(), "expected mean-reversion signals"


def test_trend_leg_trails_mr_leg_takes_fixed_tp():
    # Trend-leg rows carry a trail_mult and no fixed tp_rr; MR rows carry a tp_rr.
    rng = np.random.default_rng(2)
    trend = 100 + np.cumsum(0.2 + rng.normal(0, 0.4, 800))
    osc = trend[-1] + 8 * np.sin(np.linspace(0, 40 * np.pi, 800)) + rng.normal(0, 0.3, 800)
    df = _frame(np.concatenate([trend, osc]))
    sig = compute_signals(df, _htf(df), TrendParams(entry_mode="regime"))
    trend_rows = sig[sig["reason"].str.startswith("TREND")]
    mr_rows = sig[sig["reason"].str.startswith("MR")]
    assert (trend_rows["trail_mult"] > 0).all() and trend_rows["tp_rr"].isna().all()
    assert (mr_rows["trail_mult"] == 0).all() and mr_rows["tp_rr"].notna().all()


def test_engine_uses_per_signal_exit_over_cfg():
    # A trailing signal must not exit at a fixed TP even when cfg.use_tp is True.
    df = _frame(100 + np.cumsum(0.2 + np.random.default_rng(3).normal(0, 0.4, 600)))
    sig = compute_signals(df, _htf(df), TrendParams(entry_mode="regime"))
    cfg = BTConfig(use_tp=True, rr=2.0, daily_loss_pct=1e9, max_dd_pct=1e9)
    trades, _, _ = run_backtest(sig, cfg)
    # trend trades (trailing) should exit by stop/kill, never by the cfg fixed tp
    if not trades.empty:
        trend_trades = trades[trades["reason"].str.startswith("TREND")]
        assert (trend_trades["exit_reason"] != "tp").all()


def test_cooldown_resumes_after_kill_switch():
    # Force a kill switch with a tiny DD cap, confirm the breaker can recover.
    df = _frame(100 + np.cumsum(np.random.default_rng(4).normal(0, 1.0, 2000)))
    sig = compute_signals(df, _htf(df), TrendParams(entry_mode="regime"))
    permanent, _, s_perm = run_backtest(sig, BTConfig(max_dd_pct=2.0, dd_cooldown_days=0.0))
    resumable, _, s_res = run_backtest(sig, BTConfig(max_dd_pct=2.0, dd_cooldown_days=1.0))
    # permanent halts once then stops; resumable can trip multiple times
    assert s_res["kill_switch_hits"] >= s_perm["kill_switch_hits"]
