"""Parameter/strategy sweep with in-sample -> out-of-sample validation.

Risk halts (daily loss, drawdown kill switch) are lifted here so we measure the
raw signal edge; fees and slippage stay realistic. The winner is re-run with the
real risk limits in run.py. Ranks by a robustness score that rewards consistent
IS *and* OOS performance and penalises tiny trade counts and deep drawdowns.
"""
import dataclasses as dc
import itertools
import logging

import pandas as pd

from app.strategy.trend import TrendParams, compute_signals
from backtest.data import load_candles
from backtest.engine import BTConfig, run_backtest
from backtest.metrics import compute_metrics

logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")
RESEARCH = dict(daily_loss_pct=1e9, max_dd_pct=1e9)  # risk off for edge measurement


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
    # fixed reward:risk targets
    for rr in (1.5, 2.0, 3.0):
        yield dict(use_tp=True, rr=rr, atr_stop_mult=1.5, trail_atr_mult=0.0)
    # ATR trailing (let winners run)
    for asm in (1.5, 2.0):
        for tm in (2.0, 3.0, 4.0):
            yield dict(use_tp=False, atr_stop_mult=asm, trail_atr_mult=tm)


def score(m_is: dict, m_oos: dict) -> float:
    if m_is.get("trades", 0) < 20 or m_oos.get("trades", 0) < 10:
        return -1e9
    # both periods must be profitable to score well
    r_is = m_is["total_return_pct"]
    r_oos = m_oos["total_return_pct"]
    pf_oos = min(m_oos["profit_factor"], 5.0)
    dd_pen = abs(m_oos["max_drawdown_pct"]) * 0.3
    consistency = 0 if (r_is > 0 and r_oos > 0) else -40
    return r_oos * 1.5 + r_is * 0.5 + pf_oos * 10 - dd_pen + consistency


def main():
    df_tf, df_htf = load_candles()
    split = df_tf.index[int(len(df_tf) * 0.7)]
    rows = []
    entries = list(entry_grid())
    exits = list(exit_grid())
    print(f"entry combos={len(entries)} x exit combos={len(exits)} = {len(entries)*len(exits)} runs")

    for ep in entries:
        sig = compute_signals(df_tf, df_htf, ep)
        sig_is, sig_oos = sig[sig.index < split], sig[sig.index >= split]
        for ex in exits:
            cfg = BTConfig(**RESEARCH, **ex)
            t_is, e_is, s_is = run_backtest(sig_is, cfg)
            t_oos, e_oos, s_oos = run_backtest(sig_oos, cfg)
            m_is = compute_metrics(t_is, e_is, s_is)
            m_oos = compute_metrics(t_oos, e_oos, s_oos)
            rows.append({
                "mode": ep.entry_mode, "ef": ep.ema_fast, "es": ep.ema_slow,
                "lb": ep.breakout_lookback, "adx": ep.adx_min, "htf": ep.use_htf_filter,
                "exit": "tp%.1f" % ex["rr"] if ex["use_tp"] else "trail%.1f/%.1f" % (ex["atr_stop_mult"], ex["trail_atr_mult"]),
                "ret_is": m_is.get("total_return_pct"), "ret_oos": m_oos.get("total_return_pct"),
                "pf_oos": m_oos.get("profit_factor"), "sharpe_oos": m_oos.get("sharpe"),
                "dd_oos": m_oos.get("max_drawdown_pct"), "wr_oos": m_oos.get("win_rate_pct"),
                "n_is": m_is.get("trades"), "n_oos": m_oos.get("trades"),
                "score": round(score(m_is, m_oos), 1),
            })
    res = pd.DataFrame(rows).sort_values("score", ascending=False)
    res.to_csv("backtest/results/sweep.csv", index=False)
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(res.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
