#!/usr/bin/env python3
"""Sideload lane performance monitor — daily health check vs backtest expectation.

Runs daily (off-hours) to answer: "is the live sideload lane performing as the
backtest predicted?" It:

  1. Reads the locked strategy rule (sideload_<symbol>_strategy table) for the
     backtest expectation (interval, RSI, expectancy, win rate).
  2. Computes LIVE realized PnL from closed round-trips in the DB.
  3. Tracks rolling win rate, expectancy, and trade count over a lookback
     window (default 30 days).
  4. Sends a Discord notification with a health verdict:
       - ON TRACK: live expectancy/win rate within tolerance of backtest.
       - DEVIATING: live is materially worse than backtest (warn).
       - KILL-WORTHY: live is badly negative or win rate collapsed (alert).

Usage:
    python -m sideload.monitor_amd --symbol AMD            # AMD lane (default)
    python -m sideload.monitor_amd --symbol SOL/USD        # SOL lane
    python -m sideload.monitor_amd --dry                   # print, don't notify
    python -m sideload.monitor_amd --days 30               # lookback window
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core import config, database

logger = logging.getLogger("MonitorSideload")

# Health thresholds (relative to backtest expectation).
# Live expectancy within +/- EXPECTANCY_TOLERANCE_PCT of backtest = on track.
EXPECTANCY_TOLERANCE_PCT = 0.50  # 50% of backtest expectancy
# Live win rate within +/- WIN_RATE_TOLERANCE of backtest = on track.
WIN_RATE_TOLERANCE = 0.10
# Minimum trades in the window before we can judge health (avoid noise).
MIN_TRADES_TO_JUDGE = 5
# If live expectancy is negative AND win rate < 40%, flag kill-worthy.
KILL_WORTHY_EXPECTANCY = 0.0
KILL_WORTHY_WIN_RATE = 0.40


def _strategy_table(symbol: str) -> str:
    """Strategy table name for a symbol (e.g. SOL/USD -> sideload_sol_strategy)."""
    # Lowercase, strip non-alphanumerics (SOL/USD -> sol_usd -> sol).
    sym = symbol.replace("/", "_").replace("-", "_").lower()
    # For crypto pairs, keep the base (SOL/USD -> sol).
    if "_usd" in sym:
        sym = sym.replace("_usd", "")
    return f"sideload_{sym}_strategy"


def _load_strategy_rule(symbol: str) -> dict | None:
    """Read the locked strategy rule (backtest expectation)."""
    table = _strategy_table(symbol)
    conn = database.get_db_connection()
    try:
        cur = conn.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except Exception as e:
        logger.warning(f"Could not read strategy rule from {table}: {e}")
        return None
    finally:
        conn.close()


def _symbol_round_trips(symbol: str, lookback_days: int) -> list[dict]:
    """Fetch closed round-trips for a symbol in the lookback window."""
    try:
        from core import feedback
        trips = feedback.compute_closed_round_trips(lookback_days=lookback_days)
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        sym_trips = []
        for t in trips:
            if t.get("symbol", "").upper() != symbol.upper():
                continue
            # Only count round-trips closed within the window.
            close_ts = t.get("close_ts")
            if close_ts:
                try:
                    if isinstance(close_ts, str):
                        close_ts = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
                    if close_ts < cutoff:
                        continue
                except Exception:
                    pass
            sym_trips.append(t)
        return sym_trips
    except Exception as e:
        logger.warning(f"Could not compute {symbol} round-trips: {e}")
        log_exception_to_jira(e, f"{symbol} Monitor Round-Trip Failure")
        return []


def _assess_health(symbol: str, rule: dict | None, trips: list[dict]) -> dict:
    """Compare live performance against the backtest expectation."""
    n = len(trips)
    if n == 0:
        return {
            "verdict": "NO_TRADES",
            "trades": 0, "pnl": 0.0, "win_rate": 0.0, "expectancy": 0.0,
            "message": f"No {symbol} round-trips closed in the window yet.",
        }
    pnl = float(sum(t.get("pnl", 0.0) or 0.0 for t in trips))
    wins = sum(1 for t in trips if t.get("win"))
    win_rate = wins / n
    expectancy = pnl / n

    if not rule:
        return {
            "verdict": "NO_BASELINE",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "message": f"No locked strategy rule; {n} {symbol} trades, ${pnl:,.2f} PnL, "
                       f"{win_rate:.0%} win, ${expectancy:.2f}/trade.",
        }

    exp_expected = float(rule.get("test_expectancy", 0.0) or 0.0)
    win_expected = float(rule.get("test_win_rate", 0.0) or 0.0)

    if n < MIN_TRADES_TO_JUDGE:
        return {
            "verdict": "INSUFFICIENT",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "exp_expected": exp_expected, "win_expected": win_expected,
            "message": f"Only {n} {symbol} trades (need {MIN_TRADES_TO_JUDGE}+ to judge). "
                       f"${pnl:,.2f} PnL, {win_rate:.0%} win.",
        }

    # Kill-worthy: badly negative with collapsed win rate.
    if expectancy <= KILL_WORTHY_EXPECTANCY and win_rate < KILL_WORTHY_WIN_RATE:
        return {
            "verdict": "KILL_WORTHY",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "exp_expected": exp_expected, "win_expected": win_expected,
            "message": (f"KILL-WORTHY: {n} {symbol} trades, ${pnl:,.2f} PnL, "
                        f"{win_rate:.0%} win, ${expectancy:.2f}/trade — well below "
                        f"backtest ${exp_expected:.2f}/trade @ {win_expected:.0%}."),
        }

    # On track if within tolerance of backtest.
    exp_ok = abs(expectancy - exp_expected) <= abs(exp_expected) * EXPECTANCY_TOLERANCE_PCT
    win_ok = abs(win_rate - win_expected) <= WIN_RATE_TOLERANCE
    if exp_ok and win_ok:
        return {
            "verdict": "ON_TRACK",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "exp_expected": exp_expected, "win_expected": win_expected,
            "message": (f"ON TRACK: {n} {symbol} trades, ${pnl:,.2f} PnL, "
                        f"{win_rate:.0%} win, ${expectancy:.2f}/trade vs backtest "
                        f"${exp_expected:.2f}/trade @ {win_expected:.0%}."),
        }

    return {
        "verdict": "DEVIATING",
        "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
        "exp_expected": exp_expected, "win_expected": win_expected,
        "message": (f"DEVIATING: {n} {symbol} trades, ${pnl:,.2f} PnL, "
                    f"{win_rate:.0%} win, ${expectancy:.2f}/trade vs backtest "
                    f"${exp_expected:.2f}/trade @ {win_expected:.0%}."),
    }


def _notify(symbol: str, health: dict, lookback_days: int) -> None:
    """Send a Discord notification with the health verdict."""
    try:
        from core.discord_notifier import send_discord_message
        verdict = health["verdict"]
        emoji = {
            "ON_TRACK": "✅", "DEVIATING": "⚠️", "KILL_WORTHY": "🚨",
            "INSUFFICIENT": "⏳", "NO_TRADES": "🔇", "NO_BASELINE": "ℹ️",
        }.get(verdict, "ℹ️")
        lines = [
            f"{emoji} **{symbol} Monitor ({lookback_days}d)** — {verdict}",
            health["message"],
        ]
        if health.get("exp_expected") is not None:
            lines.append(f"Backtest baseline: ${health['exp_expected']:.2f}/trade "
                         f"@ {health['win_expected']:.0%} win.")
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def monitor(symbol: str = "AMD", lookback_days: int = 30, dry: bool = False) -> dict:
    """Run the daily sideload performance monitor. Returns the health dict."""
    rule = _load_strategy_rule(symbol)
    trips = _symbol_round_trips(symbol, lookback_days)
    health = _assess_health(symbol, rule, trips)
    health["symbol"] = symbol
    health["lookback_days"] = lookback_days
    health["date"] = datetime.utcnow().strftime("%Y-%m-%d")

    logger.info(f"{symbol} monitor ({lookback_days}d): {health['verdict']} — {health['message']}")
    if not dry:
        _notify(symbol, health, lookback_days)
    return health


def main() -> None:
    parser = argparse.ArgumentParser(description="Sideload lane performance monitor")
    parser.add_argument("--symbol", default="AMD", help="Symbol to monitor (e.g. AMD, SOL/USD)")
    parser.add_argument("--dry", action="store_true", help="Print, don't notify")
    parser.add_argument("--days", type=int, default=30, help="Lookback window (days)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-monitor")
    try:
        health = monitor(symbol=args.symbol, lookback_days=args.days, dry=args.dry)
        print(json.dumps(health, indent=2, default=str))
    except Exception as e:
        logger.critical(f"{args.symbol} monitor failed: {e}")
        log_exception_to_jira(e, f"{args.symbol} Monitor Failure")
        raise


if __name__ == "__main__":
    main()