"""Walk-forward robustness check: split the whole period into K contiguous folds
and measure each config's return per fold. Ranks by consistency (how many folds
are positive) rather than one lucky window, which is what a single IS/OOS split
rewards by accident. Only ATR-trailing exits are tested (fixed TP was dominated).
"""
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


def entry_grid():
    for mode in ("ema_cross", "pullback"):
        for ef, es in ((20, 50), (10, 30), (21, 55)):
            for adx in (0.0, 20.0, 25.0):
                for htf in (True, False):
                    yield TrendParams(entry_mode=mode, ema_fast=ef, ema_slow=es,
                                      adx_min=adx, use_htf_filter=htf)
    for lb in (20, 40, 55, 96):
        for adx in (0.0, 20.0, 25.0):
            for htf in (True, False):
                yield TrendParams(entry_mode="breakout", breakout_lookback=lb,
                                  adx_min=adx, use_htf_filter=htf)


def exit_grid():
    for asm in (1.5, 2.0):
        for tm in (3.0, 4.0):
            yield dict(use_tp=False, atr_stop_mult=asm, trail_atr_mult=tm)


def main():
    df_tf, df_htf = load_candles()
    bounds = [df_tf.index[int(len(df_tf) * i / K)] for i in range(K)] + [df_tf.index[-1]]
    rows = []
    for ep in entry_grid():
        sig = compute_signals(df_tf, df_htf, ep)
        for ex in exit_grid():
            cfg = BTConfig(**RESEARCH, **ex)
            fold_rets, fold_n = [], []
            for i in range(K):
                s = sig[(sig.index >= bounds[i]) & (sig.index < bounds[i + 1])]
                t, e, st = run_backtest(s, cfg)
                m = compute_metrics(t, e, st)
                fold_rets.append(m.get("total_return_pct", 0.0))
                fold_n.append(m.get("trades", 0))
            t, e, st = run_backtest(sig, cfg)
            full = compute_metrics(t, e, st)
            fr = np.array(fold_rets)
            rows.append({
                "mode": ep.entry_mode, "ef": ep.ema_fast, "es": ep.ema_slow,
                "lb": ep.breakout_lookback, "adx": ep.adx_min, "htf": ep.use_htf_filter,
                "exit": "trail%.1f/%.1f" % (ex["atr_stop_mult"], ex["trail_atr_mult"]),
                "pos_folds": int((fr > 0).sum()), "median_fold": round(float(np.median(fr)), 2),
                "worst_fold": round(float(fr.min()), 2), "full_ret": full.get("total_return_pct"),
                "full_pf": full.get("profit_factor"), "full_dd": full.get("max_drawdown_pct"),
                "full_sharpe": full.get("sharpe"), "n": full.get("trades"),
                "folds": [round(x, 1) for x in fold_rets],
            })
    res = pd.DataFrame(rows)
    res = res[res["n"] >= 40]  # enough trades to trust
    res = res.sort_values(["pos_folds", "median_fold", "full_ret"], ascending=False)
    res.to_csv("backtest/results/walkforward.csv", index=False)
    pd.set_option("display.width", 240, "display.max_columns", 40)
    print(f"K={K} folds, {len(rows)} configs tested, {len(res)} with >=40 trades")
    print(res.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
