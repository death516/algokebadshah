"""Event-driven backtest engine (single source of truth for fills/PnL).

Design choices kept deliberately conservative so results are honest:
  - Signal fires on a CLOSED bar i, the entry fills at bar i+1 OPEN (no lookahead).
  - SL/TP are recomputed off the actual fill using the signal bar's ATR, so the
    reward:risk ratio holds after slippage.
  - Intrabar, if both stop and target are touched in the same bar, we assume the
    STOP filled first (worst case).
  - Fees charged per side on notional; slippage applied to every fill.
  - One position at a time (MAX_OPEN_POSITIONS = 1).

The risk layer here (1% sizing, leverage/notional caps, daily-loss halt,
drawdown kill switch) is a stand-in for Role C's RiskManager so the equity curve
is realistic. Swap it for `app.risk` when integrating.
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from backtest.sizing import risk_fraction


@dataclass
class BTConfig:
    start_equity: float = 10_000.0
    risk_pct: float = 1.0          # % of equity risked per trade
    max_leverage: float = 5.0
    max_notional: Optional[float] = None
    taker_fee_pct: float = 0.03    # per side, % of notional -> 10 bps round-trip with slippage
    slippage_bps: float = 2.0      # per fill, basis points (0.03%*2 + 2bps*2 = 10 bps round-trip)
    daily_loss_pct: float = 3.0    # halt new entries for the day past this loss
    max_dd_pct: float = 10.0       # kill switch: flatten + stop trading
    dd_cooldown_days: float = 0.0  # 0 = permanent halt (spec); >0 = resume after cooldown
    allow_short: bool = True
    atr_stop_mult: float = 1.5
    rr: float = 2.0
    use_tp: bool = True            # False = exit only via (trailing) stop / kill switch
    trail_after_r: float = 0.0     # 0 = off; else move stop to breakeven after this many R
    trail_atr_mult: float = 0.0    # 0 = off; else trail stop at close -/+ mult*ATR

    # --- position sizing (see backtest/sizing.py) ---
    sizing: str = "fixed"          # fixed|kelly|martingale|anti_martingale|kelly_martingale|kelly_anti
    risk_floor_pct: float = 0.25   # minimum risk fraction (Kelly fallback / floor)
    sizing_cap_pct: float = 10.0   # hard cap on risk fraction per trade
    kelly_fraction: float = 0.5    # fractional Kelly (0.5 = half Kelly)
    kelly_lookback: int = 40       # rolling window of trades for the edge estimate
    kelly_min_trades: int = 10
    kelly_cap_pct: float = 5.0
    kelly_use_r_multiples: bool = False
    mart_mult: float = 2.0         # risk multiplier per consecutive loss
    mart_max_steps: int = 3
    anti_mult: float = 1.5         # risk multiplier per consecutive win
    anti_max_steps: int = 3


@dataclass
class _Pos:
    side: str
    entry: float
    size: float
    stop: float
    tp: float
    risk_unit: float
    entry_i: int
    entry_ts: pd.Timestamp
    reason: str
    use_tp: bool = True
    trail_mult: float = 0.0
    be_moved: bool = False
    funding_pnl: float = 0.0


def run_backtest(sig: pd.DataFrame, cfg: BTConfig = BTConfig(),
                 mark_prices: Optional[pd.Series | pd.DataFrame] = None,
                 funding: Optional[pd.Series | pd.DataFrame] = None):
    """Run the simulation over an annotated signals frame.

    Returns (trades_df, equity_df, state) where state has final equity / halt info.
    """
    o = sig["open"].to_numpy(float)
    h = sig["high"].to_numpy(float)
    l = sig["low"].to_numpy(float)
    c = sig["close"].to_numpy(float)
    atr = sig["atr"].to_numpy(float)
    side = sig["side"].to_numpy(object)
    ts = sig.index
    n = len(sig)
    mark_open = None
    if isinstance(mark_prices, pd.DataFrame):
        mark_open = mark_prices["open"].reindex(ts).ffill().to_numpy(float) if "open" in mark_prices else None
        mark_prices = mark_prices["close"]
    mark_close = (mark_prices.reindex(ts).ffill().to_numpy(float)
                  if mark_prices is not None else c.copy())
    if mark_open is None:
        mark_open = mark_close
    if isinstance(funding, pd.DataFrame):
        funding = funding["rate"]
    funding_at = (funding.reindex(ts).fillna(0.0).to_numpy(float)
                  if funding is not None else np.zeros(n, dtype=float))
    has_exits = {"stop_mult", "tp_rr", "trail_mult"}.issubset(sig.columns)
    e_sm = sig["stop_mult"].to_numpy(float) if has_exits else None
    e_rr = sig["tp_rr"].to_numpy(float) if has_exits else None
    e_tm = sig["trail_mult"].to_numpy(float) if has_exits else None
    slip = cfg.slippage_bps / 1e4
    fee = cfg.taker_fee_pct / 100.0

    equity = cfg.start_equity
    peak = equity
    halted = False                 # drawdown kill switch active
    halt_until = None              # resume time when cooldown is enabled
    n_kills = 0
    pos: Optional[_Pos] = None
    pending: Optional[int] = None  # signal bar index awaiting fill at next open

    cur_day = None
    day_start_equity = equity
    daily_halt = False

    win_streak = 0
    loss_streak = 0
    recent_pnls = []               # rolling trade PnLs for Kelly

    trades, eq_ts, eq_val = [], [], []

    def _close(p: _Pos, price: float, i: int, why: str):
        nonlocal equity, peak, win_streak, loss_streak
        direction = 1 if p.side == "buy" else -1
        gross = p.size * (price - p.entry) * direction
        notional_in = p.size * p.entry
        notional_out = p.size * price
        fees = fee * (notional_in + notional_out)
        pnl = gross - fees
        equity += pnl
        peak = max(peak, equity)
        total_pnl = pnl + p.funding_pnl
        if total_pnl > 0:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak += 1
            win_streak = 0
        if cfg.kelly_use_r_multiples:
            risk_amount = p.size * p.risk_unit
            recent_pnls.append(total_pnl / risk_amount if risk_amount > 0 else 0.0)
        else:
            recent_pnls.append(total_pnl)
        del recent_pnls[:-cfg.kelly_lookback]  # keep only the rolling window
        trades.append({
            "entry_ts": p.entry_ts, "exit_ts": ts[i], "side": p.side,
            "entry": round(p.entry, 2), "exit": round(price, 2), "size": round(p.size, 6),
            # Funding is posted to equity at each settlement, so include it in
            # the reported trade P&L without adding it to equity a second time.
            "pnl": round(pnl + p.funding_pnl, 2),
            "r_multiple": round(gross / (p.size * p.risk_unit + 1e-9), 3),
            "fees": round(fees, 2), "funding_pnl": round(p.funding_pnl, 2),
            "exit_reason": why, "reason": p.reason,
        })

    for i in range(n):
        day = ts[i].date()
        if day != cur_day:
            cur_day = day
            day_start_equity = equity
            daily_halt = False

        # Funding is charged/credited at each hourly timestamp to positions
        # already open at that settlement. Positive rates mean longs pay shorts.
        if pos is not None and funding_at[i] != 0.0 and mark_open[i] > 0:
            direction = 1 if pos.side == "buy" else -1
            settlement = -direction * pos.size * mark_open[i] * funding_at[i]
            pos.funding_pnl += settlement
            equity += settlement
            peak = max(peak, equity)

        # 1) manage open position on this bar
        if pos is not None:
            direction = 1 if pos.side == "buy" else -1
            stop_hit = (l[i] <= pos.stop) if pos.side == "buy" else (h[i] >= pos.stop)
            tp_hit = pos.use_tp and ((h[i] >= pos.tp) if pos.side == "buy" else (l[i] <= pos.tp))
            if stop_hit:  # conservative: stop first if both touched
                # gap-through: fill at the worse of open/stop
                fill = min(o[i], pos.stop) if pos.side == "buy" else max(o[i], pos.stop)
                _close(pos, fill, i, "stop")
                pos = None
            elif tp_hit:
                fill = max(o[i], pos.tp) if pos.side == "buy" else min(o[i], pos.tp)
                _close(pos, fill, i, "tp")
                pos = None

        # 2) trailing stops using this bar's close (applies next bar)
        if pos is not None:
            if cfg.trail_after_r > 0 and not pos.be_moved:
                move = (c[i] - pos.entry) if pos.side == "buy" else (pos.entry - c[i])
                if move >= cfg.trail_after_r * pos.risk_unit:
                    pos.stop = pos.entry
                    pos.be_moved = True
            if pos.trail_mult > 0 and atr[i] > 0:
                if pos.side == "buy":
                    pos.stop = max(pos.stop, c[i] - pos.trail_mult * atr[i])
                else:
                    pos.stop = min(pos.stop, c[i] + pos.trail_mult * atr[i])

        # 3) drawdown kill switch (mark-to-market). Permanent by default (spec);
        #    if dd_cooldown_days > 0 it pauses then resumes (an always-on bot can
        #    never recover from a permanent halt, since flat equity never rebounds).
        mtm = equity
        if pos is not None:
            direction = 1 if pos.side == "buy" else -1
            mtm = equity + pos.size * (mark_close[i] - pos.entry) * direction
        if not halted and (peak - mtm) / peak >= cfg.max_dd_pct / 100.0:
            if pos is not None:
                _close(pos, c[i], i, "kill_switch")
                pos = None
            halted = True
            n_kills += 1
            if cfg.dd_cooldown_days > 0:
                halt_until = ts[i] + pd.Timedelta(days=cfg.dd_cooldown_days)
        elif halted and halt_until is not None and ts[i] >= halt_until:
            halted = False
            halt_until = None
            peak = equity  # reset the drawdown reference so it does not re-trip at once

        # 4) fill a pending entry at this bar's open
        if pos is None and pending is not None and not halted:
            si = pending
            pending = None
            s = side[si]
            if s and (cfg.allow_short or s == "buy"):
                direction = 1 if s == "buy" else -1
                fill = o[i] * (1 + slip * direction)
                # per-signal exit plan if present, else global cfg
                if has_exits and not np.isnan(e_sm[si]):
                    stop_mult = e_sm[si]
                    tp_rr = e_rr[si]
                    trail_mult = 0.0 if np.isnan(e_tm[si]) else e_tm[si]
                    use_tp = not np.isnan(tp_rr)
                else:
                    stop_mult, use_tp = cfg.atr_stop_mult, cfg.use_tp
                    tp_rr, trail_mult = cfg.rr, cfg.trail_atr_mult
                risk_unit = stop_mult * atr[si]
                if risk_unit > 0:
                    stop = fill - risk_unit if s == "buy" else fill + risk_unit
                    tp = (fill + tp_rr * risk_unit) if s == "buy" else (fill - tp_rr * risk_unit)
                    frac = risk_fraction(cfg, win_streak, loss_streak, recent_pnls)
                    size = (equity * frac) / risk_unit
                    notional = size * fill
                    cap = equity * cfg.max_leverage
                    if cfg.max_notional:
                        cap = min(cap, cfg.max_notional)
                    if notional > cap:
                        size = cap / fill
                    pos = _Pos(side=s, entry=fill, size=size, stop=stop, tp=tp,
                               risk_unit=risk_unit, entry_i=i, entry_ts=ts[i],
                               reason=str(sig["reason"].iloc[si]),
                               use_tp=use_tp, trail_mult=trail_mult)

        # 5) evaluate a fresh signal (fills next bar) if flat and allowed
        daily_pnl = equity - day_start_equity
        if daily_pnl <= -cfg.daily_loss_pct / 100.0 * day_start_equity:
            daily_halt = True
        if pos is None and pending is None and not halted and not daily_halt and side[i]:
            pending = i

        # record mark-to-market equity
        eq_ts.append(ts[i])
        eq_val.append(mtm)

    # close any open position at the final bar
    if pos is not None:
        _close(pos, c[-1], n - 1, "eod")

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame({"ts": eq_ts, "equity": eq_val}).set_index("ts")
    state = {"final_equity": equity, "peak": peak, "halted": halted,
             "kill_switch_hits": n_kills, "start_equity": cfg.start_equity}
    return trades_df, equity_df, state
