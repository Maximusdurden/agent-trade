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

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("RunAMDTrader")


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD sideload intraday trader")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Number of trading cycles to run (default 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log decisions but place no orders")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-trader")

    from sideload import config_sideload as sl_cfg
    sl_cfg.apply_sideload_overrides()
    from core import config
    from core.alpaca_client import AlpacaClient
    from core.data_provider import DataProvider
    from core.guardrails import RiskGuardrails
    from core.trading_brain import TradingBrain
    from sideload import runner_sideload as rs

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