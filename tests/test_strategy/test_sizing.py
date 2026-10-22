"""Position-sizing rule checks (Role B)."""
import math

from backtest.engine import BTConfig
from backtest.sizing import kelly_fraction, risk_fraction


def test_kelly_formula_known_values():
    # 12 trades: 6 wins of +2, 6 losses of -1 -> W=0.5, b=2 -> f = 0.5 - 0.5/2 = 0.25
    pnls = [2, 2, 2, 2, 2, 2, -1, -1, -1, -1, -1, -1]
    assert math.isclose(kelly_fraction(pnls), 0.25, abs_tol=1e-9)


def test_kelly_none_when_no_edge_or_too_few():
    assert kelly_fraction([1, -1, 1]) is None               # too few
    assert kelly_fraction([-1] * 20) is None                # no wins
    # losing edge -> f clamps to 0
    pnls = [1] * 4 + [-3] * 8
    assert kelly_fraction(pnls) == 0.0


def test_fixed_is_constant():
    cfg = BTConfig(sizing="fixed", risk_pct=1.0)
    assert math.isclose(risk_fraction(cfg, 5, 0, []), 0.01)
    assert math.isclose(risk_fraction(cfg, 0, 9, []), 0.01)


def test_martingale_grows_on_losses_capped():
    cfg = BTConfig(sizing="martingale", risk_pct=1.0, mart_mult=2.0, mart_max_steps=3)
    assert math.isclose(risk_fraction(cfg, 0, 0, []), 0.01)       # base
    assert math.isclose(risk_fraction(cfg, 0, 2, []), 0.04)       # x2^2
    assert math.isclose(risk_fraction(cfg, 0, 9, []), 0.08)       # capped at 2^3
    assert math.isclose(risk_fraction(cfg, 5, 0, []), 0.01)       # wins don't matter


def test_anti_martingale_grows_on_wins_capped():
    cfg = BTConfig(sizing="anti_martingale", risk_pct=1.0, anti_mult=1.5, anti_max_steps=3)
    assert math.isclose(risk_fraction(cfg, 0, 0, []), 0.01)
    assert math.isclose(risk_fraction(cfg, 2, 0, []), 0.01 * 1.5 ** 2)
    assert math.isclose(risk_fraction(cfg, 9, 0, []), 0.01 * 1.5 ** 3)  # capped
    assert math.isclose(risk_fraction(cfg, 0, 5, []), 0.01)              # losses reset


def test_hard_cap_applies():
    cfg = BTConfig(sizing="martingale", risk_pct=4.0, mart_mult=2.0,
                   mart_max_steps=5, sizing_cap_pct=10.0)
    # 4% * 2^5 = 128% but capped at 10%
    assert math.isclose(risk_fraction(cfg, 0, 5, []), 0.10)


def test_kelly_combo_uses_kelly_base_times_streak():
    pnls = [2, 2, 2, 2, 2, 2, -1, -1, -1, -1, -1, -1]  # kelly f = 0.25, half-kelly = 0.125
    cfg = BTConfig(sizing="kelly_anti", kelly_fraction=0.5, kelly_cap_pct=50.0,
                   anti_mult=1.5, anti_max_steps=3, sizing_cap_pct=100.0)
    base = 0.5 * 0.25  # 0.125
    assert math.isclose(risk_fraction(cfg, 2, 0, pnls), base * 1.5 ** 2)
