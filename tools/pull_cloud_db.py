#!/usr/bin/env python3
"""Pull the authoritative trading DB from GCS to `cloud_downloaded_trading_agent.db`.

This is the offline analysis equivalent of `core.gcs_sync.download_from_gcs()`.
The live runner overwrites `trading_agent.db` during execution; analysis scripts
read from a separate snapshot file (`cloud_downloaded_trading_agent.db`) so they
never race with the running agent or mutate live state.

Read-only with respect to trading: writes ONLY the local analysis snapshot file.
"""
import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

from core.gcs_sync import get_gcs_client  # noqa: E402

CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pull_cloud_db")


def main():
    bucket_name = os.getenv("GCS_BUCKET_NAME")
    if not bucket_name:
        logger.error("GCS_BUCKET_NAME not set in .env")
        return 1

    client = get_gcs_client()
    if client is None:
        logger.error("GCS client unavailable (no ADC credentials)")
        return 1

    bucket = client.bucket(bucket_name)
    blob = bucket.blob("trading_agent.db")
    if not blob.exists():
        logger.error("trading_agent.db not found on GCS bucket %s", bucket_name)
        return 1

    tmp = CLOUD_DB + ".tmp"
    blob.download_to_filename(tmp)
    os.replace(tmp, CLOUD_DB)
    sz = os.path.getsize(CLOUD_DB) / (1024 * 1024)
    logger.info("Downloaded %s (%.1f MB)", CLOUD_DB, sz)
    return 0


if __name__ == "__main__":
    sys.exit(main())