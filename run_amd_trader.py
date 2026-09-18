#!/usr/bin/env python3
"""AMD sideload intraday trading job entrypoint (Cloud Run).

Runs a BOUNDED intraday trading loop for the AMD sideload lane. A single Cloud
Scheduler trigger (every 15 min Mon-Fri) starts this job, which runs a bounded
number of cycles (default 1) so it doesn't run forever. For a true continuous
loop, run ``python -m sideload.runner_sideload --loop`` locally.

This is the entrypoint referenced by the ``sideload-trader`` Cloud Run job
(deploy/deploy_sideload.ps1).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("RunAMDTrader")


def _is_amd_market_hours() -> tuple[bool, str]:
    """AMD is an equity (not crypto), so it only trades during US equity market
    hours: Mon-Fri 09:30-16:00 America/New_York. Returns (is_open, reason).

    This is a hard gate at the top of the trader job so the scheduler can keep
    firing every 15 min but the job no-ops outside the window (handles DST
    automatically via zoneinfo, unlike a fixed-UTC cron).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz)
    except Exception:
        try:
            import pytz
            tz = pytz.timezone("America/New_York")
            now = datetime.now(tz)
        except Exception:
            return True, "Could not determine NY time; running anyway."
    weekday = now.weekday()
    if weekday >= 5:
        return False, f"AMD market closed: Weekend ({now.strftime('%A')})."
    market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < market_start:
        return False, f"AMD market closed: Pre-market (NY {now.strftime('%H:%M')})."
    if now > market_end:
        return False, f"AMD market closed: Post-market (NY {now.strftime('%H:%M')})."
    return True, "AMD market open."


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD sideload intraday trader")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Number of trading cycles to run (default 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log decisions but place no orders")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the 09:30-16:00 NY market-hours gate (testing)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-trader")

    # AMD is an equity — only trade during US market hours (09:30-16:00 NY).
    # Skip (no-op) outside the window unless --force is passed.
    if not args.force:
        is_open, reason = _is_amd_market_hours()
        if not is_open:
            logger.info(f"Skipping AMD trader cycle: {reason}")
            return

    from sideload import config_sideload as sl_cfg
    sl_cfg.apply_sideload_overrides()
    from core import config
    from core.alpaca_client import AlpacaClient
    from core.data_provider import DataProvider
    from core.guardrails import RiskGuardrails
    from core.trading_brain import TradingBrain
    from sideload import runner_sideload as rs

    # CRITICAL: download the latest GCS DB BEFORE running so the sideload lane
    # MERGES its AMD decision into the existing data instead of starting from an
    # empty /tmp DB and overwriting the normal lane's decisions on upload. This
    # was the root cause of the dashboard "flipping" between the AMD post and the
    # normal-lane posts (each job's upload clobbered the other's).
    try:
        from core.gcs_sync import download_from_gcs
        download_from_gcs()
    except Exception as dl_err:
        logger.warning(f"Could not download GCS DB before sideload cycle: {dl_err}")

    alpaca_client = AlpacaClient()
    data_provider = DataProvider(alpaca_client)
    brain = TradingBrain()
    guardrails = RiskGuardrails()

    interval_min = float(getattr(config, "TRADING_INTERVAL_MINUTES", 15))
    for i in range(args.cycles):
        logger.info(f"AMD trader cycle {i + 1}/{args.cycles}")
        try:
            rs.run_single_cycle(alpaca_client, data_provider, brain, guardrails,
                                dry_run=args.dry_run)
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            log_exception_to_jira(e, "AMD Sideload Trader Cycle Failure")
        if i < args.cycles - 1:
            time.sleep(interval_min * 60)

    logger.info("AMD trader job complete.")


if __name__ == "__main__":
    main()