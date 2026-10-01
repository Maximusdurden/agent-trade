"""Flat Base Breakout sideload configuration (Phase 2 production).

This module defines the locked Stage 2 flat base breakout strategy parameters
for the sideloaded trading lane. It mirrors the AMD/GLD sideload pattern: a
dedicated lane that owns a reserved universe, reuses agent-trade's Alpaca
client / indicator engine, and writes to the SAME database so the blog +
dashboard pick the lane up automatically.

Locked specs (Phase 1d verdict):
  - Universe: 27 momentum leaders (AMD explicitly excluded and deconflicted to
    the active RSI(2)<10 tech swing lane).
  - Sizing: $350 fixed dollar risk per trade (1R = $350 based on 1.5*ATR14 stop).
  - Concurrency: max 3 open flat-base positions (max heat $1,050).
  - Setup: +30% prior trend / 60 bars, base 12-35 bars <=20% depth, lows > SMA50.
  - Breakout: Close[t-1] > Base_High AND Vol[t-1] >= 1.5*SMA(Vol,20)[t-1].
  - Macro: QQQ Close > QQQ SMA(20).
  - Exit: breakeven at +2R, 33% @ +3R, 33% @ +5R, 34% trailed SMA20, 45d time stop.
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
# cycle_id prefix so flat-base decisions are attributable separately.
FLATBASE_CYCLE_PREFIX = os.getenv("FLATBASE_CYCLE_PREFIX", "sideload_flatbase")

# ---------------------------------------------------------------------------
# Universe (29 momentum leaders; AMD explicitly excluded for deconfliction)
# ---------------------------------------------------------------------------
FLATBASE_UNIVERSE = [
    "NVDA", "AVGO", "DELL", "HPE", "MU", "META", "TSLA", "SMH", "QQQ",
    "AMZN", "GOOGL", "MSFT", "NFLX", "APP", "ANET", "CRWD", "NOW", "PANW",
    "COIN", "MSTR", "HOOD", "SHOP", "UBER", "VRT", "CAT", "GLD", "GE",
]
# Assert AMD is NOT in the flat-base universe (deconfliction with tech swing lane).
assert "AMD" not in FLATBASE_UNIVERSE, "AMD must be excluded from flat-base universe"

# ---------------------------------------------------------------------------
# Locked strategy parameters
# ---------------------------------------------------------------------------
# Fixed dollar risk per trade (1R = $350 based on 1.5*ATR14 stop distance).
RISK_PER_TRADE_USD = _env_float("FLATBASE_RISK_USD", 350.0)
# Max concurrent open flat-base positions (max cumulative heat = 3 * $350 = $1,050).
MAX_CONCURRENT_POSITIONS = _env_int("FLATBASE_MAX_CONCURRENT", 3)
# Lookback bars for data fetch.
LOOKBACK_BARS = _env_int("FLATBASE_LOOKBACK_BARS", 120)

# Setup phase
UPTREND_GAIN_PCT = _env_float("FLATBASE_UPTREND_GAIN_PCT", 30.0)
UPTREND_LOOKBACK = _env_int("FLATBASE_UPTREND_LOOKBACK", 60)
BASE_MIN_BARS = _env_int("FLATBASE_BASE_MIN_BARS", 12)
BASE_MAX_BARS = _env_int("FLATBASE_BASE_MAX_BARS", 35)
BASE_TIGHTNESS = _env_float("FLATBASE_BASE_TIGHTNESS", 0.20)
BASE_SMA = _env_int("FLATBASE_BASE_SMA", 50)

# Execution trigger
# RVOL threshold for breakout volume confirmation. Default 1.5 (validated).
# Lowered to 1.2 via FLATBASE_BREAKOUT_RVOL to generate ~2x more signals in
# trending regimes (analysis 2026-09-30: rvol1.5 fires 0-14x/yr per symbol,
# rvol1.2 fires 2-20x with similar setup quality).
BREAKOUT_RVOL = _env_float("FLATBASE_BREAKOUT_RVOL", 1.2)
MACRO_SMA = _env_int("FLATBASE_MACRO_SMA", 20)
MACRO_SYMBOL = os.getenv("FLATBASE_MACRO_SYMBOL", "QQQ")

# Risk & exits
ATR_STOP_MULT = _env_float("FLATBASE_ATR_STOP_MULT", 1.5)
BREAKEVEN_R = _env_float("FLATBASE_BREAKEVEN_R", 2.0)
TP1_R = _env_float("FLATBASE_TP1_R", 3.0)
TP2_R = _env_float("FLATBASE_TP2_R", 5.0)
TRAIL_SMA = _env_int("FLATBASE_TRAIL_SMA", 20)
MAX_HOLD_DAYS = _env_int("FLATBASE_MAX_HOLD_DAYS", 45)

# Order execution: MOC proxy at 3:45 PM ET, TIF=day.
MOC_HOUR = _env_int("FLATBASE_MOC_HOUR", 15)
MOC_MINUTE = _env_int("FLATBASE_MOC_MINUTE", 45)

# State file (local JSON + GCS sync).
STATE_FILE = os.getenv("FLATBASE_STATE_FILE", "flatbase_positions.json")
GCS_STATE_BLOB = os.getenv("FLATBASE_GCS_BLOB", "flatbase_positions.json")


def apply_sideload_overrides() -> None:
    """Apply flat-base overrides onto the base ``core.config`` module.

    Call this AFTER importing ``core.config`` and BEFORE constructing the
    Alpaca client so the lane reads the flat-base values. Also registers the
    flat-base universe in the global sideload-reserved symbols so the normal
    lane never trades these names (cross-lane deconfliction).
    """
    cfg = base_config
    cfg.SL_SYMBOL = "FLATBASE"
    cfg.SL_CYCLE_PREFIX = FLATBASE_CYCLE_PREFIX
    cfg.RISK_PER_TRADE_USD = RISK_PER_TRADE_USD
    cfg.MAX_CONCURRENT_POSITIONS = MAX_CONCURRENT_POSITIONS

    # Register the flat-base universe in the global sideload-reserved set so
    # the normal lane excludes them (cross-lane awareness).
    reserved = set(getattr(cfg, "SIDELOAD_RESERVED_SYMBOLS", set()))
    reserved.update(FLATBASE_UNIVERSE)
    cfg.SIDELOAD_RESERVED_SYMBOLS = reserved

    # Ensure the universe is tradable by this lane.
    for sym in FLATBASE_UNIVERSE:
        if sym not in cfg.TRADING_UNIVERSE:
            cfg.TRADING_UNIVERSE = list(cfg.TRADING_UNIVERSE) + [sym]