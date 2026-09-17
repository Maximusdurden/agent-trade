"""AMD-tuned configuration for the sideloaded trading lane.

This module loads the base ``core.config`` and then applies AMD-specific
overrides so the sideload lane trades AMD differently from the normal lane,
while still writing to the SAME database (so the blog + dashboard pick AMD up
automatically).

The lane is positioned as an **AMD expert/king/god**: it owns AMD exclusively,
only enters on high-conviction, backtest-validated setups, and skips
low-confidence days rather than forcing trades.

All knobs are env-driven (prefixed ``SL_``) so they can be tuned at runtime
without a redeploy. Defaults here reflect the plan decisions:
  - Paper trading, same Alpaca account, same DB.
  - Stocks only (no options until the agent is expert).
  - $10K paper account, $100/day net target to start.
  - Any interval >= 5m (prefer longer); must outperform indices (SPY/QQQ).
"""

from __future__ import annotations

import os

# Import the base config so all normal knobs exist; we override below.
from core import config as base_config  # noqa: F401  (ensures .env + base knobs load)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Lane identity
# ---------------------------------------------------------------------------
# The single symbol this lane owns. Reserved from the normal lane so the two
# lanes never fight over it.
SL_SYMBOL = os.getenv("SL_SYMBOL", "AMD").upper()

# cycle_id prefix so AMD decisions are attributable separately on the dashboard.
SL_CYCLE_PREFIX = os.getenv("SL_CYCLE_PREFIX", "sideload_amd")

# ---------------------------------------------------------------------------
# Risk / sizing (tuned for $100/day on a $10K paper account)
# ---------------------------------------------------------------------------
# Max % of equity per AMD position. 10% of $10K = $1K/position.
SL_MAX_ALLOCATION_PCT = _env_float("SL_MAX_ALLOCATION_PCT", 0.10)
# Max % of equity per ticker (AMD only here, so same as per-position).
SL_MAX_TICKER_ALLOCATION_PCT = _env_float("SL_MAX_TICKER_ALLOCATION_PCT", 0.10)
# Daily net PnL target (USD). The learning loop tunes toward this.
SL_DAILY_TARGET_USD = _env_float("SL_DAILY_TARGET_USD", 100.0)
# Minimum cash buffer % of equity (keep dry powder).
SL_MIN_CASH_BUFFER_PCT = _env_float("SL_MIN_CASH_BUFFER_PCT", 0.20)
# Daily loss limit % of equity before buys are blocked.
SL_DAILY_LOSS_LIMIT_PCT = _env_float("SL_DAILY_LOSS_LIMIT_PCT", 0.03)

# ---------------------------------------------------------------------------
# AMD-tuned entry/exit gates (the "trade AMD better" part)
# ---------------------------------------------------------------------------
# RSI pullback gate: only buy on pullback to support, never momentum-chase.
# Mirrors the equity desk's proven RSI 37-44 edge. 0 disables.
SL_RSI_ENTRY_MAX = _env_float("SL_RSI_ENTRY_MAX", 45.0)
# RSI overbought exit: take profit when a held position is this overbought.
SL_RSI_EXIT_OVERBOUGHT = _env_float("SL_RSI_EXIT_OVERBOUGHT", 65.0)
# VWAP dead-zone sigma: no BUY/SELL inside this band around VWAP.
SL_VWAP_DEAD_ZONE_SIGMA = _env_float("SL_VWAP_DEAD_ZONE_SIGMA", 1.0)
# Minimum normalized edge (|vwap_dist| / ATR) to trade. 0 disables.
SL_MIN_EDGE_SIGMA = _env_float("SL_MIN_EDGE_SIGMA", 0.5)
# ATR sizing baseline (%): positions scale down for higher-vol AMD.
SL_VOL_SIZING_BASELINE_ATR_PCT = _env_float("SL_VOL_SIZING_BASELINE_ATR_PCT", 2.0)
# Max hold hours before a forced exit (0 = disabled; AMD is high-beta so a cap
# helps avoid stale positions bleeding out).
SL_MAX_HOLD_HOURS = _env_float("SL_MAX_HOLD_HOURS", 72.0)
# Trailing-stop giveback % (0 = disabled).
SL_TRAIL_STOP_GIVEBACK_PCT = _env_float("SL_TRAIL_STOP_GIVEBACK_PCT", 0.03)
# Per-day round-trip budget (avoid churn).
SL_MAX_ROUND_TRIPS_PER_DAY = _env_int("SL_MAX_ROUND_TRIPS_PER_DAY", 2)

# ---------------------------------------------------------------------------
# Options (stocks first — become expert first)
# ---------------------------------------------------------------------------
# Hard-disabled until the agent is expert. The backtest confidence gate is what
# flips this later.
SL_OPTIONS_ENABLED = _env_bool("SL_OPTIONS_ENABLED", False)

# ---------------------------------------------------------------------------
# Intervals to consider in the backtest (>= 5m floor, prefer longer).
# ---------------------------------------------------------------------------
SL_INTERVALS = [
    iv.strip() for iv in os.getenv("SL_INTERVALS", "5min,15min,1h,1d").split(",") if iv.strip()
]

# ---------------------------------------------------------------------------
# Backtest / learning
# ---------------------------------------------------------------------------
# Walk-forward: fraction of history used for training (rest is out-of-sample).
SL_BACKTEST_TRAIN_FRACTION = _env_float("SL_BACKTEST_TRAIN_FRACTION", 0.7)
# Minimum out-of-sample win rate for a config to be considered shippable.
SL_MIN_WIN_RATE = _env_float("SL_MIN_WIN_RATE", 0.55)
# Minimum out-of-sample expectancy (net PnL per round-trip, USD) to ship.
SL_MIN_EXPECTANCY_USD = _env_float("SL_MIN_EXPECTANCY_USD", 5.0)
# How many top configs to carry into the fine grid / walk-forward.
SL_TOP_N_CONFIGS = _env_int("SL_TOP_N_CONFIGS", 20)


def apply_sideload_overrides() -> None:
    """Apply AMD-tuned overrides onto the base ``core.config`` module.

    Call this AFTER importing ``core.config`` and BEFORE constructing the
    brain/guardrails so they read the sideload values. Mutates the shared
    ``core.config`` module in place (the same module the rest of the app uses),
    which is exactly what we want: the sideload lane and the normal lane share
    the same config object, but the sideload lane sets AMD-specific values for
    the duration of its cycle.
    """
    cfg = base_config
    cfg.SL_SYMBOL = SL_SYMBOL
    cfg.SL_CYCLE_PREFIX = SL_CYCLE_PREFIX
    cfg.SL_DAILY_TARGET_USD = SL_DAILY_TARGET_USD

    # Risk / sizing
    cfg.MAX_TRADE_ALLOCATION_PCT = SL_MAX_ALLOCATION_PCT
    cfg.MAX_TICKER_ALLOCATION_PCT = SL_MAX_TICKER_ALLOCATION_PCT
    cfg.MIN_CASH_BUFFER_PCT = SL_MIN_CASH_BUFFER_PCT
    cfg.DAILY_LOSS_LIMIT_PCT = SL_DAILY_LOSS_LIMIT_PCT

    # Entry/exit gates
    cfg.EQUITY_RSI_ENTRY_MAX = SL_RSI_ENTRY_MAX
    cfg.RSI_EXIT_OVERBOUGHT = SL_RSI_EXIT_OVERBOUGHT
    cfg.VWAP_DEAD_ZONE_SIGMA = SL_VWAP_DEAD_ZONE_SIGMA
    cfg.MIN_EDGE_SIGMA = SL_MIN_EDGE_SIGMA
    cfg.VOL_SIZING_BASELINE_ATR_PCT = SL_VOL_SIZING_BASELINE_ATR_PCT
    cfg.MAX_HOLD_HOURS = SL_MAX_HOLD_HOURS
    cfg.TRAIL_STOP_GIVEBACK_PCT = SL_TRAIL_STOP_GIVEBACK_PCT
    cfg.MAX_ROUND_TRIPS_PER_DAY = SL_MAX_ROUND_TRIPS_PER_DAY

    # Options: stocks only until expert.
    cfg.OPTIONS_ENABLED = SL_OPTIONS_ENABLED

    # Ensure AMD is tradable by this lane (it is reserved from the normal lane,
    # so we add it to the trading universe for the sideload cycle).
    if SL_SYMBOL not in cfg.TRADING_UNIVERSE:
        cfg.TRADING_UNIVERSE = list(cfg.TRADING_UNIVERSE) + [SL_SYMBOL]


# ---------------------------------------------------------------------------
# AMD-expert positioning (injected into the brain prompt)
# ---------------------------------------------------------------------------
def amd_expert_instruction() -> str:
    """Return the system-level instruction that positions the brain as an
    AMD expert/king/god for the sideload lane.

    This is prepended to the brain's system prompt so the model treats AMD as
    a deeply-understood, high-conviction instrument: it only enters on
    backtest-validated setups, respects AMD's earnings/volatility personality,
    and skips low-confidence days rather than forcing trades.
    """
    return f"""
AMD-EXPERT POSITIONING (SIDELOAD LANE):
You are the world's foremost AMD specialist — an AMD expert, king, and god. You
have studied AMD's price action, volatility, and earnings behavior inside and
out. You trade ONLY {SL_SYMBOL}. You are not a generalist; you are the single
most knowledgeable entity about this one ticker.

AMD PERSONALITY (know it cold):
- AMD is a HIGH-BETA semiconductor. It moves hard on earnings and AI/data-center
  guidance. A single earnings/guidance event can gap it several percent.
- AMD rewards buying PULLBACKS TO SUPPORT, not momentum chases. The edge lives
  at RSI ~37-44 (pullback to support), NOT at RSI >= 50 (momentum chase).
- AMD is volatile: size positions by ATR so the same dollar risk is taken
  regardless of where price sits.
- AMD whipsaws intraday in tight ranges around VWAP. Do NOT trade inside the
  VWAP dead zone; require a real regime change to reverse direction.

EXPERT DISCIPLINE (the "when I see you're in on AMD, I KNOW we'll win" rule):
- ONLY enter on HIGH-CONVICTION, backtest-validated setups. If the setup is not
  clearly a winner, output HOLD. It is BETTER to sit out a day than to force a
  low-confidence trade.
- Never momentum-chase. Never average down into a losing position.
- Respect AMD's earnings/event calendar: do not hold through a high-impact
  event (earnings, AI guidance, FOMC) unless the setup is exceptional.
- Your conviction score must be honest: 0.7+ only when multiple indicators
  (RSI pullback, VWAP edge, regime, anchors, news) agree. Below that, HOLD.
- You are measured on CONSISTENT expectancy, not on trading every day. A day
  with no trade is a good day if the setup was weak.
"""