"""Metric sanity: Sortino/Calmar present, CAPM alpha/beta behave (Role B)."""
import numpy as np
import pandas as pd

from backtest.metrics import compute_metrics


def _fixture(strat_rets, bench_rets):
    # equity and benchmark built identically so their per-bar returns align 1:1
    n = len(strat_rets)
    idx = pd.date_range("2026-01-01", periods=n + 1, freq="15min", tz="UTC")
    eq = pd.DataFrame({"equity": 10_000 * np.cumprod(np.r_[1.0, 1 + np.asarray(strat_rets)])},
                      index=idx)
    bench = pd.Series(100 * np.cumprod(np.r_[1.0, 1 + np.asarray(bench_rets)]), index=idx)
    trades = pd.DataFrame({"pnl": [100, -50, 80], "r_multiple": [2, -1, 1.5],
                           "fees": [1, 1, 1], "exit_reason": ["tp", "stop", "tp"]})
    state = {"start_equity": 10_000, "final_equity": float(eq["equity"].iloc[-1]),
             "halted": False, "kill_switch_hits": 0}
    return compute_metrics(trades, eq, state, benchmark=bench)


def test_beta_one_alpha_zero_when_strategy_equals_benchmark():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0003, 0.01, 500)
    m = _fixture(r, r)                         # equity tracks benchmark exactly
    assert abs(m["beta"] - 1.0) < 0.05
    assert abs(m["alpha_annual_pct"]) < 1.0


def test_beta_near_zero_when_uncorrelated():
    rng = np.random.default_rng(1)
    strat = rng.normal(0.0003, 0.01, 500)
    bench = rng.normal(0.0002, 0.01, 500)      # independent
    m = _fixture(strat, bench)
    assert abs(m["beta"]) < 0.2


def test_sortino_and_calmar_present_and_positive_on_uptrend():
    rng = np.random.default_rng(2)
    r = rng.normal(0.002, 0.004, 500)          # strong positive drift -> reliably up
    m = _fixture(r, r)
    assert m["sortino"] > 0 and m["calmar"] > 0
