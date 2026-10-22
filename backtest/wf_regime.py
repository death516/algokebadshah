"""Walk-forward the regime-switch strategy (trend-follow + mean-reversion).

Same K-fold consistency ranking as wf.py, but over the regime params. Risk halts
lifted to measure raw edge; fees/slippage realistic.
"""
import itertools
import logging

import numpy as np
import pandas as pd

from app.strategy.trend import TrendParams, compute_signals
from backtest.data import load_candles
from backtest.engine import BTConfig, run_backtest
from backtest.metrics import compute_metrics

logging.basicConfig(level=logging.ERROR)
RESEARCH = dict(daily_loss_pct=1e9, max_dd_pct=1e9)
K = 6


def grid():
    for adx_tr, adx_rg in ((22, 18), (25, 20), (25, 18), (28, 20)):
        for os_, ob in ((30, 70), (25, 75), (35, 65)):
            for trail in (3.0, 4.0):
                for mr_tp in (1.0, 1.5):
                    yield TrendParams(entry_mode="regime", ema_fast=20, ema_slow=50,
                                      adx_trend=adx_tr, adx_range=adx_rg,
                                      rsi_os=os_, rsi_ob=ob,
                                      trend_trail_mult=trail, mr_tp_rr=mr_tp)


def main():
    df_tf, df_htf = load_candles()
    bounds = [df_tf.index[int(len(df_tf) * i / K)] for i in range(K)] + [df_tf.index[-1]]
    cfg = BTConfig(**RESEARCH)
    rows = []
    for p in grid():
        sig = compute_signals(df_tf, df_htf, p)
        fold_rets = []
        for i in range(K):
            s = sig[(sig.index >= bounds[i]) & (sig.index < bounds[i + 1])]
            t, e, st = run_backtest(s, cfg)
            fold_rets.append(compute_metrics(t, e, st).get("total_return_pct", 0.0))
        t, e, st = run_backtest(sig, cfg)
        full = compute_metrics(t, e, st)
        fr = np.array(fold_rets)
        rows.append({
            "adx_tr": p.adx_trend, "adx_rg": p.adx_range, "rsi": f"{int(p.rsi_os)}/{int(p.rsi_ob)}",
            "trail": p.trend_trail_mult, "mr_tp": p.mr_tp_rr,
            "pos_folds": int((fr > 0).sum()), "median_fold": round(float(np.median(fr)), 2),
            "worst_fold": round(float(fr.min()), 2), "full_ret": full.get("total_return_pct"),
            "full_pf": full.get("profit_factor"), "full_dd": full.get("max_drawdown_pct"),
            "full_sharpe": full.get("sharpe"), "n": full.get("trades"),
            "folds": [round(x, 1) for x in fold_rets],
        })
    res = pd.DataFrame(rows).sort_values(
        ["pos_folds", "median_fold", "full_ret"], ascending=False)
    res.to_csv("backtest/results/walkforward_regime.csv", index=False)
    pd.set_option("display.width", 240, "display.max_columns", 40)
    print(f"K={K} folds, {len(rows)} regime configs")
    print(res.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
