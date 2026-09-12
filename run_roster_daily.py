#!/usr/bin/env python3
"""Cloud Run job entrypoint for the DAILY ticker roster update.

Runs the promotion/relegation *recommendations* + learning report + Discord
summary. It does NOT edit the pool (that's the weekly job). It pulls the fresh
DB from GCS first so it sees the latest closed round-trips.

Usage (in Cloud Run job command):
    python run_roster_daily.py
    python run_roster_daily.py --discord
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily ticker roster update")
    parser.add_argument("--discord", action="store_true",
                        help="Send a Discord roster summary")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip pulling the cloud DB from GCS")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("RosterDaily")

    # 1. Pull the authoritative DB from GCS (unless told to skip).
    if not args.skip_pull:
        from tools.pull_cloud_db import main as pull_main
        rc = pull_main()
        if rc != 0:
            logger.error("pull_cloud_db failed (rc=%s); aborting.", rc)
            return rc

    # 2. Generate the learning report (reads the cloud snapshot).
    from tools.ticker_learning_report import main as learning_main
    cloud_db = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
    sys.argv = ["ticker_learning_report", "--db", cloud_db]
    rc = learning_main()
    if rc != 0:
        logger.warning("ticker_learning_report returned rc=%s", rc)

    # 3. Promotion/relegation dry-run (recommendations only).
    from tools.ticker_promotion_relegation import (
        load_screener_pool, compute_ticker_stats, get_open_positions,
        build_roster, format_summary,
    )
    pool = load_screener_pool()
    stats = compute_ticker_stats()
    open_pos = get_open_positions()
    roster = build_roster(stats, pool, open_pos)

    logger.info("Roster summary:\n%s", format_summary(roster))

    # 4. Optional Discord notification.
    if args.discord:
        from core.discord_notifier import send_ticker_roster_summary
        send_ticker_roster_summary(roster)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())