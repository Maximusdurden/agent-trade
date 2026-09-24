"""GLD-tuned configuration for the sideloaded bounded mean-reversion ladder lane.

This module loads the base ``core.config`` and then applies GLD-specific
overrides so the sideload lane trades GLD differently from the normal lane,
while still writing to the SAME database (so the blog + dashboard pick GLD up
automatically).

The lane is positioned as a **GLD gold expert**: it owns GLD exclusively, only
enters on acute oversold pullbacks during macro uptrends (Close > SMA200), and
scales into a bounded 3-tranche ladder before taking profit on SMA20
mean-reversion or exiting via a hard -7.5% stop-loss circuit breaker.

All knobs are env-driven (prefixed ``SLG_``) so they can be tuned at runtime
without a redeploy. Defaults here reflect the spec decisions:
  - Paper trading, same Alpaca account, same DB.
  - Daily bars only (TimeFrame.Day).
  - Macro regime gate: Close > SMA(200).
  - Entry (Tranche 1): RSI(14) < 42 AND Close < Lower_BB(20, 2.0).
  - Ladder: T1 = 1.0B, T2 = 1.25B at <= t1*(1-0.025), T3 = 1.50B at <= t1*(1-0.050).
  - Take-profit: Close >= SMA(20) OR price >= blended_basis * 1.03.
  - Catastrophic stop: price <= t1_price * (1 - 0.075).
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
SLG_SYMBOL = os.getenv("SLG_SYMBOL", "GLD").upper()

# cycle_id prefix so GLD decisions are attributable separately on the dashboard.
SLG_CYCLE_PREFIX = os.getenv("SLG_CYCLE_PREFIX", "sideload_gld")

# Data timeframe: daily bars only.
SLG_TIMEFRAME = os.getenv("SLG_TIMEFRAME", "day")

# ---------------------------------------------------------------------------
# Strategy rules (Phase 1b: Connors mean-reversion variant)
# ---------------------------------------------------------------------------
# Macro regime gate: only deploy capital when Close > SMA(200).
SLG_TREND_SMA = _env_int("SLG_TREND_SMA", 200)
# Short-term stretch: RSI(2) < threshold (Connors-style, far more trades than
# RSI(14) < 42). Grid values [5, 10, 15].
SLG_RSI_PERIOD = _env_int("SLG_RSI_PERIOD", 2)
SLG_RSI_ENTRY_MAX = _env_float("SLG_RSI_ENTRY_MAX", 10.0)
# Band decoupling mode: how to confirm the oversold stretch.
#   "none"        : pure RSI-2, no band requirement (Option 1).
#   "stretch_atr" : Close < SMA(20) - (k * ATR(14))  (Option 2).
#   "boll"        : Close < Lower_BB(20, mult).
SLG_BAND_MODE = os.getenv("SLG_BAND_MODE", "stretch_atr").lower()
SLG_BOLL_PERIOD = _env_int("SLG_BOLL_PERIOD", 20)
SLG_BOLL_MULT = _env_float("SLG_BOLL_MULT", 1.5)
# Stretch ATR multiplier k for "stretch_atr" mode (grid [1.0, 1.5]).
SLG_STRETCH_ATR_MULT = _env_float("SLG_STRETCH_ATR_MULT", 1.5)
SLG_ATR_PERIOD = _env_int("SLG_ATR_PERIOD", 14)

# Ladder sizing (base notional $B).
SLG_BASE_NOTIONAL = _env_float("SLG_BASE_NOTIONAL", 1000.0)
# Tranche 1 notional multiplier.
SLG_T1_MULT = _env_float("SLG_T1_MULT", 1.0)
# Tranche 2: trigger when price <= t1_price * (1 - step2); notional mult.
# Grid values [-1.5%, -2.0%, -2.5%].
SLG_T2_STEP_PCT = _env_float("SLG_T2_STEP_PCT", 0.02)
SLG_T2_MULT = _env_float("SLG_T2_MULT", 1.25)
# Tranche 3: trigger when price <= t1_price * (1 - step3); notional mult.
SLG_T3_STEP_PCT = _env_float("SLG_T3_STEP_PCT", 0.04)
SLG_T3_MULT = _env_float("SLG_T3_MULT", 1.50)
# Maximum tranches (strictly no further allocation).
SLG_MAX_TRANCHES = _env_int("SLG_MAX_TRANCHES", 3)

# Exit parameters (Phase 1b: faster turns + time stop).
# Take-profit: liquidate 100% if Close >= SMA(exit_sma) OR price >= blended_basis * (1 + tp_pct).
SLG_TP_SMA = _env_int("SLG_TP_SMA", 5)
SLG_TP_BLENDED_PCT = _env_float("SLG_TP_BLENDED_PCT", 0.02)
# Max hold window (time stop): liquidate at close if held > max_hold_days.
SLG_MAX_HOLD_DAYS = _env_int("SLG_MAX_HOLD_DAYS", 10)
# Catastrophic stop: liquidate 100% if price <= t1_price * (1 - stop_pct).
SLG_STOP_PCT = _env_float("SLG_STOP_PCT", 0.06)

# ---------------------------------------------------------------------------
# Risk / sizing
# ---------------------------------------------------------------------------
# Max % of equity per GLD position (3 tranches of base notional can stack).
SLG_MAX_ALLOCATION_PCT = _env_float("SLG_MAX_ALLOCATION_PCT", 0.10)
# Max % of equity per ticker (GLD only here, so same as per-position).
SLG_MAX_TICKER_ALLOCATION_PCT = _env_float("SLG_MAX_TICKER_ALLOCATION_PCT", 0.10)
# Minimum cash buffer % of equity (keep dry powder).
SLG_MIN_CASH_BUFFER_PCT = _env_float("SLG_MIN_CASH_BUFFER_PCT", 0.20)
# Daily loss limit % of equity before buys are blocked.
SLG_DAILY_LOSS_LIMIT_PCT = _env_float("SLG_DAILY_LOSS_LIMIT_PCT", 0.03)

# ---------------------------------------------------------------------------
# Backtest / learning
# ---------------------------------------------------------------------------
# Walk-forward: fraction of history used for training (rest is out-of-sample).
SLG_BACKTEST_TRAIN_FRACTION = _env_float("SLG_BACKTEST_TRAIN_FRACTION", 0.7)
# Minimum out-of-sample win rate for a config to be considered shippable.
SLG_MIN_WIN_RATE = _env_float("SLG_MIN_WIN_RATE", 0.55)
# Minimum out-of-sample expectancy (net PnL per round-trip, USD) to ship.
SLG_MIN_EXPECTANCY_USD = _env_float("SLG_MIN_EXPECTANCY_USD", 5.0)
# Minimum AGGREGATED out-of-sample trades across walk-forward folds.
# NOTE: GLD is regime-dependent (only trades when Close>SMA200), so gold bear
# markets (e.g. 2021-2023) produce 0 trades. A per-fold minimum of 20 is
# structurally impossible; we use an aggregate OOS minimum instead.
SLG_MIN_OOS_TRADES = _env_int("SLG_MIN_OOS_TRADES", 15)
# How many top configs to carry into the fine grid / walk-forward.
SLG_TOP_N_CONFIGS = _env_int("SLG_TOP_N_CONFIGS", 20)

# ---------------------------------------------------------------------------
# Options (stocks first — become expert first)
# ---------------------------------------------------------------------------
SLG_OPTIONS_ENABLED = _env_bool("SLG_OPTIONS_ENABLED", False)


def apply_sideload_overrides() -> None:
    """Apply GLD-tuned overrides onto the base ``core.config`` module.

    Call this AFTER importing ``core.config`` and BEFORE constructing the
    brain/guardrails so they read the sideload values. Mutates the shared
    ``core.config`` module in place (the same module the rest of the app uses).
    """
    cfg = base_config
    cfg.SL_SYMBOL = SLG_SYMBOL
    cfg.SL_CYCLE_PREFIX = SLG_CYCLE_PREFIX

    # Risk / sizing
    cfg.MAX_TRADE_ALLOCATION_PCT = SLG_MAX_ALLOCATION_PCT
    cfg.MAX_TICKER_ALLOCATION_PCT = SLG_MAX_TICKER_ALLOCATION_PCT
    cfg.MIN_CASH_BUFFER_PCT = SLG_MIN_CASH_BUFFER_PCT
    cfg.DAILY_LOSS_LIMIT_PCT = SLG_DAILY_LOSS_LIMIT_PCT

    # Options: stocks only until expert.
    cfg.OPTIONS_ENABLED = SLG_OPTIONS_ENABLED

    # Ensure GLD is tradable by this lane (it is reserved from the normal lane,
    # so we add it to the trading universe for the sideload cycle).
    if SLG_SYMBOL not in cfg.TRADING_UNIVERSE:
        cfg.TRADING_UNIVERSE = list(cfg.TRADING_UNIVERSE) + [SLG_SYMBOL]


# ---------------------------------------------------------------------------
# GLD-expert positioning (injected into the brain prompt)
# ---------------------------------------------------------------------------
def gld_expert_instruction() -> str:
    """Return the system-level instruction that positions the brain as a GLD
    gold expert for the sideload lane.

    This is prepended to the brain's system prompt so the model treats GLD as
    a deeply-understood, high-conviction instrument: it only enters on acute
    oversold pullbacks during macro uptrends, scales into a bounded 3-tranche
    ladder, and skips low-confidence days rather than forcing trades.
    """
    return f"""
GLD-EXPERT POSITIONING (SIDELOAD LANE):
You are the world's foremost GLD specialist — a gold expert, king, and god. You
have studied GLD's price action, volatility, and macro drivers inside and out.
You trade ONLY {SLG_SYMBOL}. You are not a generalist; you are the single most
knowledgeable entity about this one instrument.

GLD PERSONALITY (know it cold):
- GLD tracks the price of gold. It is a MACRO instrument: it trends with real
  yields, the USD, and geopolitical risk. It is far less volatile than equities
  and does NOT gap on single-company earnings.
- GLD rewards buying ACUTE OVERSOLD PULLBACKS during MACRO UPTRENDS (Close >
  SMA200). The edge lives at RSI(14) < 42 with price below the lower Bollinger
  band — NOT at momentum chases.
- GLD mean-reverts: after an oversold flush, price tends to snap back toward
  the 20-day SMA. Take profit there rather than riding for more.
- GLD is bounded: scale into a MAXIMUM of 3 tranches (1.0x, 1.25x, 1.50x base
  notional) as price steps down 2.5% / 5.0% from the first fill. NEVER add a
  4th tranche. Respect the hard -7.5% stop-loss circuit breaker.

EXPERT DISCIPLINE (the "when I see you're in on GLD, I KNOW we'll win" rule):
- ONLY enter on HIGH-CONVICTION, backtest-validated setups. If the setup is not
  clearly a winner, output HOLD. It is BETTER to sit out a day than to force a
  low-confidence trade.
- Never momentum-chase. Never average down beyond the 3-tranche ladder.
- Respect macro event risk: do not hold through a high-impact event (FOMC,
  CPI, NFP, geopolitical shock) unless the setup is exceptional.
- Your conviction score must be honest: 0.7+ only when multiple indicators
  (RSI pullback, Bollinger lower band, SMA200 regime, macro news) agree. Below
  that, HOLD.
- You are measured on CONSISTENT expectancy, not on trading every day. A day
  with no trade is a good day if the setup was weak.
"""