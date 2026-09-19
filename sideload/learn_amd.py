#!/usr/bin/env python3
"""AMD daily learning agent — edge discovery -> tuned strategy rule.

Runs daily (off-hours). It:
  1. Reads the latest backtest winners (sideload/backtest_validated.json).
  2. Computes realized AMD PnL vs the $100/day target from the DB.
  3. Writes a tuned strategy rule to the sideload_amd_strategy table that the
     sideload brain reads each cycle.
  4. Emits a "what the agent learned today" summary (consumed by the
     fact-of-the-day publisher).

Usage:
    python -m sideload.learn_amd            # run the daily learning pass
    python -m sideload.learn_amd --dry      # print what would be written
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core import config, database

logger = logging.getLogger("LearnAMD")

STRATEGY_TABLE = "sideload_amd_strategy"
VALIDATED_PATH = os.path.join(PROJECT_ROOT, "sideload", "backtest_validated.json")


def _ensure_strategy_table() -> None:
    conn = database.get_db_connection()
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {STRATEGY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts TEXT,
                interval TEXT,
                rsi_entry_max REAL,
                rsi_exit_overbought REAL,
                macd_filter TEXT,
                vwap_dead_zone_sigma REAL,
                min_edge_sigma REAL,
                atr_sizing_baseline_pct REAL,
                max_hold_hours REAL,
                trail_stop_giveback_pct REAL,
                time_of_day TEXT,
                regime_filter TEXT,
                direction TEXT,
                rsi_short_entry_min REAL,
                test_expectancy REAL,
                test_win_rate REAL,
                test_trades INTEGER,
                rule_text TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _load_validated() -> list[dict]:
    if not os.path.exists(VALIDATED_PATH):
        logger.warning(f"No validated configs at {VALIDATED_PATH}. Run backtest first.")
        return []
    with open(VALIDATED_PATH) as f:
        return json.load(f)


def _realized_amd_pnl() -> float:
    """Sum realized AMD PnL from closed round-trips in the DB."""
    try:
        from core import feedback
        trips = feedback.compute_closed_round_trips()
        amd = [t for t in trips if t.get("symbol", "").upper() == sl_cfg.SL_SYMBOL]
        return float(sum(t.get("pnl", 0.0) or 0.0 for t in amd))
    except Exception as e:
        logger.warning(f"Could not compute realized AMD PnL: {e}")
        log_exception_to_jira(e, "AMD Learning PnL Computation Failure")
        return 0.0


def _build_rule_text(cfg: dict) -> str:
    """Turn a validated config into a strategy rule the brain can read."""
    rsi_entry = cfg.get("rsi_entry_max", 45.0)
    rsi_exit = cfg.get("rsi_exit_overbought", 0.0)
    interval = cfg.get("interval", "1h")
    vwap_sigma = cfg.get("vwap_dead_zone_sigma", 1.0)
    min_edge = cfg.get("min_edge_sigma", 0.5)
    max_hold = cfg.get("max_hold_hours", 0.0)
    trail = cfg.get("trail_stop_giveback_pct", 0.0)
    macd = cfg.get("macd_filter", "off")
    time_of_day = cfg.get("time_of_day", "all")
    regime = cfg.get("regime_filter", "all")

    lines = [
        f"For {sl_cfg.SL_SYMBOL} (AMD expert lane, interval={interval}):",
        f"BUY only on RSI pullback to support (RSI <= {rsi_entry}).",
    ]
    if macd == "hist_gt_0":
        lines.append("Require MACD histogram > 0 to confirm momentum.")
    if vwap_sigma > 0:
        lines.append(f"Do NOT trade inside the VWAP dead zone (±{vwap_sigma}σ).")
    if min_edge > 0:
        lines.append(f"Require normalized edge >= {min_edge} ATRs from VWAP.")
    if rsi_exit > 0:
        lines.append(f"Take profit when RSI >= {rsi_exit} (overbought exit).")
    if max_hold > 0:
        lines.append(f"Force-exit after {max_hold} hours max hold.")
    if trail > 0:
        lines.append(f"Trailing-stop: exit if {trail:.0%} of peak gain is given back.")
    if time_of_day != "all":
        lines.append(f"Only trade during {time_of_day.upper()} window.")
    if regime != "all":
        lines.append(f"Only trade in {regime} regime.")
    direction = cfg.get("direction", "long")
    if direction == "both":
        lines.append("Trade BOTH directions: long call on RSI pullback, long put on "
                     f"RSI overbought (>= {cfg.get('rsi_short_entry_min', 60.0)}).")
    elif direction == "short":
        lines.append(f"Trade SHORT only: long put on RSI overbought "
                     f"(>= {cfg.get('rsi_short_entry_min', 60.0)}).")
    lines.append("Skip low-confidence days: HOLD unless the setup is a clear winner.")
    return " ".join(lines)


def learn(dry: bool = False) -> dict:
    """Run the daily learning pass. Returns a summary dict for the fact-of-day."""
    _ensure_strategy_table()
    validated = _load_validated()
    realized_pnl = _realized_amd_pnl()
    target = sl_cfg.SL_DAILY_TARGET_USD

    summary = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "symbol": sl_cfg.SL_SYMBOL,
        "realized_pnl": realized_pnl,
        "daily_target": target,
        "validated_configs": len(validated),
        "best_config": None,
        "rule_text": None,
        "learned": "No validated configs yet — run the backtest first.",
    }

    if not validated:
        logger.warning("No validated configs; nothing to write.")
        return summary

    best = validated[0]
    rule_text = _build_rule_text(best)
    summary["best_config"] = best
    summary["rule_text"] = rule_text
    summary["learned"] = (
        f"Best AMD config: interval={best.get('interval')}, RSI entry<={best.get('rsi_entry_max')}, "
        f"out-of-sample expectancy=${best.get('test_expectancy', 0.0):.2f}/trade, "
        f"win rate={best.get('test_win_rate', 0.0):.0%} over {best.get('test_trades', 0)} test trades. "
        f"Realized PnL=${realized_pnl:,.2f} vs ${target:,.0f}/day target."
    )

    if dry:
        logger.info(f"[DRY] Would write rule: {rule_text}")
        return summary

    # Write the tuned rule (replace any prior rule for this lane).
    conn = database.get_db_connection()
    try:
        conn.execute(f"DELETE FROM {STRATEGY_TABLE}")
        conn.execute(f"""
            INSERT INTO {STRATEGY_TABLE} (
                created_ts, interval, rsi_entry_max, rsi_exit_overbought, macd_filter,
                vwap_dead_zone_sigma, min_edge_sigma, atr_sizing_baseline_pct,
                max_hold_hours, trail_stop_giveback_pct, time_of_day, regime_filter,
                direction, rsi_short_entry_min,
                test_expectancy, test_win_rate, test_trades, rule_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            best.get("interval"), best.get("rsi_entry_max"), best.get("rsi_exit_overbought"),
            best.get("macd_filter"), best.get("vwap_dead_zone_sigma"),
            best.get("min_edge_sigma"), best.get("atr_sizing_baseline_pct"),
            best.get("max_hold_hours"), best.get("trail_stop_giveback_pct"),
            best.get("time_of_day"), best.get("regime_filter"),
            best.get("direction", "long"), best.get("rsi_short_entry_min"),
            best.get("test_expectancy"), best.get("test_win_rate"), best.get("test_trades"),
            rule_text,
        ))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Wrote tuned AMD strategy rule: {rule_text}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD daily learning agent")
    parser.add_argument("--dry", action="store_true", help="Print without writing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-learn")
    try:
        summary = learn(dry=args.dry)
        logger.info("Learning summary: %s", json.dumps(summary, indent=2, default=str))
    except Exception as e:
        logger.critical(f"Learning run failed: {e}")
        log_exception_to_jira(e, "AMD Learning Run Failure")
        raise


if __name__ == "__main__":
    main()