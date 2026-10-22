"""Parameter sensitivity: vary ONE production parameter at a time and check the
metrics stay in a sane band. A strategy that only works at one exact setting is
overfit; graceful degradation across neighbours is the robustness we want.
Reproduce: python -m backtest.sensitivity
"""
import dataclasses as dc
import logging
from pathlib import Path

import pandas as pd

from app.strategy.trend import compute_signals
from backtest.data import load_candles
from backtest.engine import run_backtest
from backtest.metrics import compute_metrics
from backtest.run import PROD, RISK_CFG

logging.basicConfig(level=logging.ERROR)
OUT = Path(__file__).resolve().parent.parent / "backtest" / "results" / "sensitivity"

# param name -> values to try (production value included, marked * in output)
GRID = {
    "adx_trend": [20, 22, 25, 28],
    "adx_range": [15, 18, 20],
    "rsi_os": [20, 25, 30, 35],
    "trend_trail_mult": [3.0, 3.5, 4.0, 5.0],
    "trend_stop_mult": [1.5, 2.0, 2.5],
    "mr_tp_rr": [0.75, 1.0, 1.5],
    "ema_fast": [10, 20, 21],
}


def main():
    df_tf, df_htf = load_candles()
    bench = compute_signals(df_tf, df_htf, PROD)["close"]
    rows = []
    for name, values in GRID.items():
        for v in values:
            over = {name: v}
            # keep ob mirrored with os for symmetry
            if name == "rsi_os":
                over["rsi_ob"] = 100 - v
            if name == "ema_fast" and v >= PROD.ema_slow:
                continue
            p = dc.replace(PROD, **over)
            sig = compute_signals(df_tf, df_htf, p)
            t, e, s = run_backtest(sig, RISK_CFG)
            m = compute_metrics(t, e, s, benchmark=bench)
            rows.append({
                "param": name, "value": v,
                "is_prod": getattr(PROD, name) == v,
                "return_%": m.get("total_return_pct"), "PF": m.get("profit_factor"),
                "sharpe": m.get("sharpe"), "calmar": m.get("calmar"),
                "maxDD_%": m.get("max_drawdown_pct"), "trades": m.get("trades"),
            })
    res = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT / "sensitivity.csv", index=False)
    pd.set_option("display.width", 200, "display.max_rows", 100)
    print("\n=== Parameter sensitivity (one-at-a-time, risk-managed; * = production) ===")
    for name in GRID:
        sub = res[res["param"] == name].copy()
        sub["value"] = sub.apply(lambda r: f"{r['value']}*" if r["is_prod"] else str(r["value"]), axis=1)
        print(f"\n[{name}]")
        print(sub.drop(columns=["param", "is_prod"]).to_string(index=False))


if __name__ == "__main__":
    main()
