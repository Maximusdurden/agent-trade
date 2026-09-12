#!/usr/bin/env python3
"""One-shot refresh of all agent-trade performance reports.

Chains: pull cloud DB -> deep-dive -> ticker performance -> learning report,
and optionally sends a Discord roster summary.

Usage:
  python tools/refresh_performance_reports.py
  python tools/refresh_performance_reports.py --skip-pull   # use existing cloud DB
  python tools/refresh_performance_reports.py --discord     # send Discord summary
"""
import argparse
import logging
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("RefreshReports")


def run(script: str, *args: str) -> int:
    cmd = [sys.executable, os.path.join(PROJECT_ROOT, script), *args]
    logger.info("Running: %s", " ".join(cmd))
    return subprocess.call(cmd)


def main():
    parser = argparse.ArgumentParser(description="Refresh all performance reports")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip pulling the cloud DB from GCS")
    parser.add_argument("--discord", action="store_true",
                        help="Send a Discord roster summary after refresh")
    args = parser.parse_args()

    if not args.skip_pull:
        rc = run("tools/pull_cloud_db.py")
        if rc != 0:
            logger.error("pull_cloud_db failed (rc=%s); aborting.", rc)
            return rc

    # 1. Deep-dive report (agent_trade_deep_dive.py reads cloud_downloaded_trading_agent.db)
    rc = run("agent_trade_deep_dive.py")
    if rc != 0:
        logger.warning("agent_trade_deep_dive.py returned rc=%s", rc)

    # 2. Ticker learning report (reads config.DATABASE_PATH; point it at the cloud snapshot)
    cloud_db = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
    rc = run("tools/ticker_learning_report.py", "--db", cloud_db)
    if rc != 0:
        logger.warning("ticker_learning_report.py returned rc=%s", rc)

    # 3. Promotion/relegation dry-run (recommendations only; never auto-applies here)
    rc = run("tools/ticker_promotion_relegation.py")
    if rc != 0:
        logger.warning("ticker_promotion_relegation.py returned rc=%s", rc)

    if args.discord:
        try:
            from tools.ticker_promotion_relegation import (
                load_screener_pool, compute_ticker_stats, get_open_positions,
                build_roster, format_summary,
            )
            from core.discord_notifier import send_discord_message
            pool = load_screener_pool()
            stats = compute_ticker_stats()
            open_pos = get_open_positions()
            roster = build_roster(stats, pool, open_pos)
            summary = format_summary(roster)
            send_discord_message(f"📊 **Ticker Roster Daily**\n{summary}")
        except Exception as e:
            logger.error(f"Discord summary failed: {e}")

    logger.info("Report refresh complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())