#!/usr/bin/env python3
"""AMD performance monitor — daily health check vs backtest expectation.

Runs daily (off-hours) to answer: "is the live AMD lane performing as the
backtest predicted?" It:

  1. Reads the locked strategy rule (sideload_amd_strategy table) for the
     backtest expectation (interval, RSI, expectancy, win rate).
  2. Computes LIVE realized AMD PnL from closed round-trips in the DB.
  3. Tracks rolling win rate, expectancy, and trade count over a lookback
     window (default 30 days).
  4. Sends a Discord notification with a health verdict:
       - ON TRACK: live expectancy/win rate within tolerance of backtest.
       - DEVIATING: live is materially worse than backtest (warn).
       - KILL-WORTHY: live is badly negative or win rate collapsed (alert).

Usage:
    python -m sideload.monitor_amd            # run the daily monitor
    python -m sideload.monitor_amd --dry      # print, don't notify
    python -m sideload.monitor_amd --days 30  # lookback window
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

logger = logging.getLogger("MonitorAMD")

STRATEGY_TABLE = "sideload_amd_strategy"

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


def _load_strategy_rule() -> dict | None:
    """Read the locked strategy rule (backtest expectation)."""
    conn = database.get_db_connection()
    try:
        cur = conn.execute(
            f"SELECT * FROM {STRATEGY_TABLE} ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except Exception as e:
        logger.warning(f"Could not read strategy rule: {e}")
        return None
    finally:
        conn.close()


def _amd_round_trips(lookback_days: int) -> list[dict]:
    """Fetch closed AMD round-trips in the lookback window."""
    try:
        from core import feedback
        trips = feedback.compute_closed_round_trips(lookback_days=lookback_days)
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        amd = []
        for t in trips:
            if t.get("symbol", "").upper() != sl_cfg.SL_SYMBOL:
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
            amd.append(t)
        return amd
    except Exception as e:
        logger.warning(f"Could not compute AMD round-trips: {e}")
        log_exception_to_jira(e, "AMD Monitor Round-Trip Failure")
        return []


def _assess_health(rule: dict | None, trips: list[dict]) -> dict:
    """Compare live AMD performance against the backtest expectation."""
    n = len(trips)
    if n == 0:
        return {
            "verdict": "NO_TRADES",
            "trades": 0, "pnl": 0.0, "win_rate": 0.0, "expectancy": 0.0,
            "message": "No AMD round-trips closed in the window yet.",
        }
    pnl = float(sum(t.get("pnl", 0.0) or 0.0 for t in trips))
    wins = sum(1 for t in trips if t.get("win"))
    win_rate = wins / n
    expectancy = pnl / n

    if not rule:
        return {
            "verdict": "NO_BASELINE",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "message": f"No locked strategy rule; {n} AMD trades, ${pnl:,.2f} PnL, "
                       f"{win_rate:.0%} win, ${expectancy:.2f}/trade.",
        }

    exp_expected = float(rule.get("test_expectancy", 0.0) or 0.0)
    win_expected = float(rule.get("test_win_rate", 0.0) or 0.0)

    if n < MIN_TRADES_TO_JUDGE:
        return {
            "verdict": "INSUFFICIENT",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "exp_expected": exp_expected, "win_expected": win_expected,
            "message": f"Only {n} AMD trades (need {MIN_TRADES_TO_JUDGE}+ to judge). "
                       f"${pnl:,.2f} PnL, {win_rate:.0%} win.",
        }

    # Kill-worthy: badly negative with collapsed win rate.
    if expectancy <= KILL_WORTHY_EXPECTANCY and win_rate < KILL_WORTHY_WIN_RATE:
        return {
            "verdict": "KILL_WORTHY",
            "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
            "exp_expected": exp_expected, "win_expected": win_expected,
            "message": (f"KILL-WORTHY: {n} AMD trades, ${pnl:,.2f} PnL, "
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
            "message": (f"ON TRACK: {n} AMD trades, ${pnl:,.2f} PnL, "
                        f"{win_rate:.0%} win, ${expectancy:.2f}/trade vs backtest "
                        f"${exp_expected:.2f}/trade @ {win_expected:.0%}."),
        }

    return {
        "verdict": "DEVIATING",
        "trades": n, "pnl": pnl, "win_rate": win_rate, "expectancy": expectancy,
        "exp_expected": exp_expected, "win_expected": win_expected,
        "message": (f"DEVIATING: {n} AMD trades, ${pnl:,.2f} PnL, "
                    f"{win_rate:.0%} win, ${expectancy:.2f}/trade vs backtest "
                    f"${exp_expected:.2f}/trade @ {win_expected:.0%}."),
    }


def _notify(health: dict, lookback_days: int) -> None:
    """Send a Discord notification with the health verdict."""
    try:
        from core.discord_notifier import send_discord_message
        verdict = health["verdict"]
        emoji = {
            "ON_TRACK": "✅", "DEVIATING": "⚠️", "KILL_WORTHY": "🚨",
            "INSUFFICIENT": "⏳", "NO_TRADES": "🔇", "NO_BASELINE": "ℹ️",
        }.get(verdict, "ℹ️")
        lines = [
            f"{emoji} **AMD Monitor ({lookback_days}d)** — {verdict}",
            health["message"],
        ]
        if health.get("exp_expected") is not None:
            lines.append(f"Backtest baseline: ${health['exp_expected']:.2f}/trade "
                         f"@ {health['win_expected']:.0%} win.")
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def monitor(lookback_days: int = 30, dry: bool = False) -> dict:
    """Run the daily AMD performance monitor. Returns the health dict."""
    rule = _load_strategy_rule()
    trips = _amd_round_trips(lookback_days)
    health = _assess_health(rule, trips)
    health["lookback_days"] = lookback_days
    health["date"] = datetime.utcnow().strftime("%Y-%m-%d")

    logger.info(f"AMD monitor ({lookback_days}d): {health['verdict']} — {health['message']}")
    if not dry:
        _notify(health, lookback_days)
    return health


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD performance monitor")
    parser.add_argument("--dry", action="store_true", help="Print, don't notify")
    parser.add_argument("--days", type=int, default=30, help="Lookback window (days)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-monitor")
    try:
        health = monitor(lookback_days=args.days, dry=args.dry)
        print(json.dumps(health, indent=2, default=str))
    except Exception as e:
        logger.critical(f"AMD monitor failed: {e}")
        log_exception_to_jira(e, "AMD Monitor Failure")
        raise


if __name__ == "__main__":
    main()