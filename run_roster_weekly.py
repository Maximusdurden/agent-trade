#!/usr/bin/env python3
"""Cloud Run job entrypoint for the WEEKLY ticker roster update.

Applies the promotion/relegation changes to ``screener_pool.json`` and uploads
the edited pool to GCS (so the runtime pool updates without an image rebuild).
Also sends a Discord summary and (optionally) creates a Jira ticket for the
audit trail.

Usage (in Cloud Run job command):
    python run_roster_weekly.py
    python run_roster_weekly.py --discord --jira
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

# Wire error -> Jira ticket creation (same as runner.py). Any logger.error() /
# logger.critical() in the roster pipeline files a Jira bug ticket.
try:
    from core import logger_setup
    logger_setup.setup_logging(app_name="agent-trade-roster", env="production")
except Exception as _e:
    print(f"[run_roster_weekly] Jira logging setup failed: {_e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly ticker roster update")
    parser.add_argument("--discord", action="store_true",
                        help="Send a Discord roster summary")
    parser.add_argument("--jira", action="store_true",
                        help="Create a Jira ticket for pool changes")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip pulling the cloud DB from GCS")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("RosterWeekly")

    # 1. Pull the authoritative DB from GCS.
    if not args.skip_pull:
        from tools.pull_cloud_db import main as pull_main
        rc = pull_main()
        if rc != 0:
            logger.error("pull_cloud_db failed (rc=%s); aborting.", rc)
            return rc

    # 2. Compute the roster and apply pool changes.
    from tools.ticker_promotion_relegation import (
        load_screener_pool, compute_ticker_stats, get_open_positions,
        build_roster, apply_roster, write_pool, format_summary,
    )
    pool = load_screener_pool()
    stats = compute_ticker_stats()
    open_pos = get_open_positions()
    roster = build_roster(stats, pool, open_pos)

    summary = format_summary(roster)
    logger.info("Roster summary:\n%s", summary)

    if roster["to_add"] or roster["to_remove"]:
        new_pool = apply_roster(pool, roster["to_add"], roster["to_remove"])
        write_pool(new_pool)
        logger.info("Applied pool changes: %d -> %d tickers.", len(pool), len(new_pool))

        # Upload the edited pool to GCS so the runtime pool updates without a
        # full image rebuild.
        from core.gcs_sync import upload_screener_pool
        if upload_screener_pool(new_pool):
            logger.info("Uploaded updated pool to GCS.")
        else:
            logger.warning("GCS upload failed; local pool updated only.")
    else:
        logger.info("No pool changes this week.")

    # 3. Optional Discord notification.
    if args.discord:
        from core.discord_notifier import send_ticker_roster_summary
        send_ticker_roster_summary(roster)

    # 4. Optional Jira audit ticket.
    if args.jira and (roster["to_add"] or roster["to_remove"]):
        try:
            _create_jira_ticket(roster)
        except Exception as e:
            logger.error(f"Jira ticket creation failed: {e}")

    return 0


def _create_jira_ticket(roster: dict) -> None:
    """Create a Jira ticket documenting the weekly pool changes."""
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "..", "agent-jira-client"))
    from agent_jira import IssueManager  # type: ignore
    project_key = os.getenv("JIRA_PROJECT_KEY", "TMCL")
    manager = IssueManager()
    summary = f"Weekly ticker roster: +{len(roster['to_add'])} / -{len(roster['to_remove'])}"
    body = (
        "Weekly promotion/relegation applied to screener_pool.json.\n\n"
        f"**Promoted:** {', '.join(roster['to_add']) or 'none'}\n"
        f"**Relegated:** {', '.join(roster['to_remove']) or 'none'}\n"
        f"**Watch:** {', '.join(roster['tiers'].get('WATCH', [])) or 'none'}\n"
    )
    manager.create_issue(project_key=project_key, summary=summary, description=body)
    logger.info("Created Jira ticket: %s", summary)


if __name__ == "__main__":
    raise SystemExit(main())