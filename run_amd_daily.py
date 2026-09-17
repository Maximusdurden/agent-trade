#!/usr/bin/env python3
"""AMD sideload daily job entrypoint (Cloud Run).

Runs the daily learning pipeline for the AMD sideload lane:
  1. Pull the fresh DB from GCS (so learning sees the latest trades).
  2. Run the grid-search backtest (coarse -> fine -> walk-forward).
  3. Run the learning agent (writes the tuned strategy rule).
  4. Publish "Dexter's AMD Fact of the Day" (Dexter-voiced blog post).

This is the entrypoint referenced by the ``sideload-daily`` Cloud Run job
(deploy/deploy_sideload.ps1).
"""

from __future__ import annotations

import logging
import sys

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("RunAMDDaily")


def _pull_db_from_gcs() -> None:
    """Pull the fresh DB from GCS so learning sees the latest trades."""
    try:
        from core import gcs_sync
        gcs_sync.download_from_gcs()
        logger.info("Pulled DB from GCS.")
    except Exception as e:
        logger.warning(f"GCS DB pull failed (continuing with local DB): {e}")
        log_exception_to_jira(e, "AMD Daily GCS DB Pull Failure")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-daily")

    _pull_db_from_gcs()

    # 1. Backtest (coarse -> fine -> walk-forward).
    from sideload import backtest_amd as bt
    from sideload import config_sideload as sl_cfg
    from core.alpaca_client import AlpacaClient

    client = AlpacaClient()
    df_by_interval = bt._load_all_intervals(client, sl_cfg.SL_INTERVALS)
    coarse = bt.run_grid(df_by_interval, bt.COARSE_GRID, top_n=sl_cfg.SL_TOP_N_CONFIGS)
    logger.info(f"Coarse winners: {len(coarse)}")
    fine = bt.run_grid(df_by_interval, bt.FINE_GRID, top_n=sl_cfg.SL_TOP_N_CONFIGS)
    logger.info(f"Fine winners: {len(fine)}")
    validated = bt.walk_forward_validate(
        df_by_interval, fine or coarse, train_frac=sl_cfg.SL_BACKTEST_TRAIN_FRACTION
    )
    logger.info(f"Walk-forward shippable configs: {len(validated)}")
    import json
    with open("sideload/backtest_validated.json", "w") as f:
        json.dump(validated, f, indent=2, default=str)

    # 2. Learning agent (writes the tuned strategy rule).
    from sideload import learn_amd as la
    summary = la.learn(dry=False)
    logger.info("Learning summary: %s", json.dumps(summary, indent=2, default=str))

    # 3. Publish Dexter's AMD Fact of the Day.
    from sideload import publish_fact_of_day as fod
    result = fod.publish(dry=False)
    logger.info("Fact-of-day result: %s", json.dumps(result, indent=2, default=str))

    logger.info("AMD daily pipeline complete.")


if __name__ == "__main__":
    main()