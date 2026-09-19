"""SOL/USD-tuned configuration for a second sideloaded trading lane.

Replicates the AMD sideload lane (``config_sideload.py``) but for SOL/USD, the
most profitable crypto in our data. It sets the ``SL_*`` env vars BEFORE
importing the shared ``config_sideload`` module, so the same machinery (runner,
backtest, monitor) runs against SOL with SOL-tuned knobs.

The SOL lane is a dedicated, paper-trading lane that owns SOL/USD exclusively
(reserved from the normal lane). It trades BOTH directions (long on RSI
pullback, short on RSI overbought) on intraday bars, using the backtest-validated
config from the 2026-09-19 audit.

Usage (from project root):
    python -m sideload.config_sideload_sol   # prints the effective config
"""

from __future__ import annotations

import os

# Set SOL-specific env vars BEFORE importing the shared sideload config.
os.environ.setdefault("SL_SYMBOL", "SOL/USD")
os.environ.setdefault("SL_CYCLE_PREFIX", "sideload_sol")
# SOL-tuned entry/exit gates (from the 2026-09-19 backtest).
os.environ.setdefault("SL_RSI_ENTRY_MAX", "45")
os.environ.setdefault("SL_RSI_EXIT_OVERBOUGHT", "0")
os.environ.setdefault("SL_VWAP_DEAD_ZONE_SIGMA", "0.5")
os.environ.setdefault("SL_MIN_EDGE_SIGMA", "0.3")
os.environ.setdefault("SL_VOL_SIZING_BASELINE_ATR_PCT", "2.5")
os.environ.setdefault("SL_MAX_HOLD_HOURS", "0")  # trailing-only exit
os.environ.setdefault("SL_TRAIL_STOP_GIVEBACK_PCT", "0.03")
os.environ.setdefault("SL_MAX_ROUND_TRIPS_PER_DAY", "4")  # crypto trades more
# SOL is crypto (24/7) — no options for now (stocks/spot first).
os.environ.setdefault("SL_OPTIONS_ENABLED", "false")
# SOL backtest intervals (intraday focus).
os.environ.setdefault("SL_INTERVALS", "1h,15min,5min,1min")

# Import the shared sideload config (reads the env vars set above).
from sideload import config_sideload as _base  # noqa: E402


def apply_sol_overrides() -> None:
    """Apply SOL-tuned overrides onto the shared ``core.config`` module.

    Call this AFTER importing ``core.config`` and BEFORE constructing the
    brain/guardrails so they read the SOL values. Mirrors
    ``config_sideload.apply_sideload_overrides()``.
    """
    _base.apply_sideload_overrides()


def sol_expert_instruction() -> str:
    """System-level instruction positioning the brain as a SOL/USD expert.

    SOL is a high-beta, 24/7 crypto that trends hard and whipsaws. The edge
    (2026-09-19 backtest) lives at intraday frequency with a trailing-stop exit
    and RSI pullback (long) / RSI overbought (short) entries.
    """
    return f"""
SOL-EXPERT POSITIONING (SIDELOAD LANE):
You are the world's foremost SOL/USD specialist. You trade ONLY SOL/USD. You are
not a generalist; you are the single most knowledgeable entity about this one
crypto pair.

SOL PERSONALITY (know it cold):
- SOL is a HIGH-BETA, 24/7 crypto. It trends hard on ecosystem/network news and
  whipsaws intraday. It does NOT respect market hours — it trades around the clock.
- The edge lives at INTRADAY frequency: buy RSI pullback to support (RSI <= 45),
  and short RSI overbought (RSI >= 60). Use a trailing-stop exit (3% giveback).
- SOL is volatile: size positions by ATR so the same dollar risk is taken
  regardless of where price sits.
- Do NOT trade inside the VWAP dead zone; require a real regime change to
  reverse direction.

EXPERT DISCIPLINE (the "when I see you're in on SOL, I KNOW we'll win" rule):
- ONLY enter on HIGH-CONVICTION, backtest-validated setups. If the setup is not
  clearly a winner, output HOLD. It is BETTER to sit out than to force a
  low-confidence trade.
- Never momentum-chase. Never average down into a losing position.
- Your conviction score must be honest: 0.7+ only when multiple indicators
  (RSI pullback, VWAP edge, regime, news) agree. Below that, HOLD.
- You are measured on CONSISTENT expectancy, not on trading every day. A day
  with no trade is a good day if the setup was weak.
"""


if __name__ == "__main__":
    from core import config
    apply_sol_overrides()
    print(f"SOL lane: symbol={_base.SL_SYMBOL}, cycle_prefix={_base.SL_CYCLE_PREFIX}")
    print(f"  RSI entry max={_base.SL_RSI_ENTRY_MAX}, trail={_base.SL_TRAIL_STOP_GIVEBACK_PCT}")
    print(f"  intervals={_base.SL_INTERVALS}")
    print(f"  OPTIONS_ENABLED={config.OPTIONS_ENABLED}")