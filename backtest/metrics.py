"""Performance metrics + equity-curve plot from engine output."""
import json
from pathlib import Path

import numpy as np
import pandas as pd


def compute_metrics(trades: pd.DataFrame, equity: pd.DataFrame, state: dict,
                    periods_per_year: float = 365 * 24 * 4,
                    benchmark: pd.Series = None, rf: float = 0.0) -> dict:
    """periods_per_year defaults to 15m bars in a year (for Sharpe/Sortino).

    benchmark: a price series (e.g. BTC close) for CAPM alpha/beta/Treynor —
    the strategy is measured against buy-and-hold of it. rf: annual risk-free rate.
    """
    start, final = state["start_equity"], state["final_equity"]
    total_ret = final / start - 1.0

    if trades.empty:
        return {"trades": 0, "total_return_pct": round(total_ret * 100, 2),
                "note": "no trades"}

    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] <= 0]["pnl"]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = gross_win / gross_loss if gross_loss > 0 else np.inf

    eq = equity["equity"]
    rets = eq.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * np.sqrt(periods_per_year)) if rets.std() > 0 else 0.0
    # Sortino: penalise only downside volatility (target 0)
    downside = rets[rets < 0]
    dstd = np.sqrt((downside ** 2).mean()) if len(downside) else 0.0
    sortino = (rets.mean() / dstd * np.sqrt(periods_per_year)) if dstd > 0 else 0.0
    roll_max = eq.cummax()
    max_dd = ((eq - roll_max) / roll_max).min()

    span_days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
    cagr = (final / start) ** (365.0 / span_days) - 1.0 if span_days > 0 else 0.0
    # Calmar: annual return per unit of worst drawdown
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0.0

    out = {
        "trades": int(len(trades)),
        "wins": int((trades["pnl"] > 0).sum()),
        "losses": int((trades["pnl"] <= 0).sum()),
        "win_rate_pct": round((trades["pnl"] > 0).mean() * 100, 2),
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "profit_factor": round(float(profit_factor), 3),
        "avg_r_multiple": round(float(trades["r_multiple"].mean()), 3),
        "expectancy_usd": round(float(trades["pnl"].mean()), 2),
        "sharpe": round(float(sharpe), 2),
        "sortino": round(float(sortino), 2),
        "calmar": round(float(calmar), 2),
        "max_drawdown_pct": round(float(max_dd) * 100, 2),
        "total_fees_usd": round(float(trades["fees"].sum()), 2),
        "final_equity": round(final, 2),
        "halted_by_kill_switch": bool(state["halted"]),
        "kill_switch_hits": int(state.get("kill_switch_hits", 0)),
        "exit_breakdown": trades["exit_reason"].value_counts().to_dict(),
        "span_days": round(span_days, 1),
    }

    if benchmark is not None and len(benchmark) > 2:
        b = benchmark.reindex(eq.index).pct_change()
        pair = pd.concat([rets.rename("s"), b.rename("b")], axis=1, sort=False).dropna()
        var_b = pair["b"].var()
        beta = pair["s"].cov(pair["b"]) / var_b if var_b > 0 else 0.0
        ann_s = pair["s"].mean() * periods_per_year
        ann_b = pair["b"].mean() * periods_per_year
        alpha = ann_s - (rf + beta * (ann_b - rf))                    # CAPM, annualised
        treynor = (ann_s - rf) / beta if beta != 0 else float("nan")  # return per unit market risk
        out.update({
            "alpha_annual_pct": round(float(alpha) * 100, 2),
            "beta": round(float(beta), 3),
            "treynor_annual": round(float(treynor), 3),
        })
    return out


def save_results(trades: pd.DataFrame, equity: pd.DataFrame, metrics: dict, outdir: Path,
                 label: str = "") -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(outdir / "trades.csv", index=False)
    equity.to_csv(outdir / "equity.csv")
    (outdir / "results.json").write_text(json.dumps(metrics, indent=2, default=str))
    _plot(equity, trades, metrics, outdir / "equity_curve.png", label)


def _plot(equity: pd.DataFrame, trades: pd.DataFrame, metrics: dict, path: Path, label: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(equity.index, equity["equity"], lw=1.1, color="#2563eb")
    ax.set_title(f"Equity curve {label}  |  ret {metrics.get('total_return_pct')}%  "
                 f"PF {metrics.get('profit_factor')}  Sharpe {metrics.get('sharpe')}  "
                 f"maxDD {metrics.get('max_drawdown_pct')}%  trades {metrics.get('trades')}")
    ax.set_ylabel("equity (USD)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
