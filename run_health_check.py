#!/usr/bin/env python3
"""Agent-Trade Cloud Health Check (P4).

Runs as a Cloud Run job (agent-trade-health) on a schedule (e.g. daily 6:00 PM
ET) and alerts via Discord if any of these are true:

  1. GCS trading_agent.db is stale (> 48h old).
  2. Alpaca broker positions exist that are NOT tracked in any lane state file
     (flatbase_positions.json / swing_positions.json / options_tsla_positions.json)
     — i.e. orphaned positions.
  3. The blog published 0-trade posts for > 3 consecutive trading days.
  4. Any enabled scheduler has not fired in > 48h (dead scheduler).

Read-only: does not modify any trading state. Sends a Discord message ONLY when
an issue is found (or on --always).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger("AgentTradeHealth")

ET = None
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _gcs_bucket():
    try:
        from core.gcs_sync import get_gcs_client
        client = get_gcs_client()
        if client is None:
            return None
        bucket_name = os.getenv("GCS_BUCKET_NAME")
        if not bucket_name:
            return None
        return client.bucket(bucket_name)
    except Exception as e:
        logger.warning(f"GCS bucket unavailable: {e}")
        return None


def check_db_staleness() -> list[str]:
    """Check GCS trading_agent.db age."""
    issues = []
    bucket = _gcs_bucket()
    if bucket is None:
        issues.append("GCS bucket unavailable — cannot verify DB freshness.")
        return issues
    try:
        blob = bucket.blob("trading_agent.db")
        if not blob.exists():
            issues.append("GCS trading_agent.db MISSING.")
            return issues
        blob.reload()
        updated = blob.updated
        age_h = (_now_utc() - updated).total_seconds() / 3600.0
        if age_h > 48:
            issues.append(f"GCS trading_agent.db STALE: last updated {updated.isoformat()} "
                          f"({age_h:.1f}h ago, > 48h).")
        else:
            logger.info(f"DB fresh: {age_h:.1f}h old.")
    except Exception as e:
        issues.append(f"Could not check DB staleness: {e}")
    return issues


def check_orphaned_positions() -> list[str]:
    """Check Alpaca positions not tracked in any lane state file."""
    issues = []
    try:
        from core.alpaca_client import AlpacaClient
        client = AlpacaClient()
        positions = client.get_positions()
        if not positions:
            logger.info("No broker positions.")
            return issues

        # Load all lane state files from GCS.
        bucket = _gcs_bucket()
        tracked = set()
        if bucket is not None:
            for blob_name in ("flatbase_positions.json", "swing_positions.json",
                              "options_tsla_positions.json"):
                try:
                    blob = bucket.blob(blob_name)
                    if blob.exists():
                        data = json.loads(blob.download_as_text() or "{}")
                        if blob_name == "flatbase_positions.json":
                            tracked.update(data.get("active_positions", {}).keys())
                        elif blob_name == "swing_positions.json":
                            tracked.update(data.get("positions", {}).keys())
                        elif blob_name == "options_tsla_positions.json":
                            ap = data.get("active_position")
                            if ap and ap.get("symbol"):
                                tracked.add(ap["symbol"])
                except Exception as e:
                    logger.warning(f"Could not read {blob_name}: {e}")

        for symbol, pos in positions.items():
            # Skip crypto (not managed by equity lanes) and options (managed by
            # the options lane state which we already checked).
            if "/" in symbol:
                continue
            if symbol not in tracked:
                qty = float(pos.get("qty", 0) or 0)
                if qty > 0:
                    issues.append(f"ORPHANED position: {symbol} qty={qty} "
                                  f"not tracked in any lane state file.")
    except Exception as e:
        issues.append(f"Could not check orphaned positions: {e}")
    return issues


def check_blog_quiet_streak() -> list[str]:
    """Check if the blog published 0-trade posts for > 3 consecutive days."""
    issues = []
    try:
        import sqlite3
        db_path = os.getenv("DATABASE_FILENAME", "trading_agent.db")
        if not os.path.exists(db_path):
            # Try to pull from GCS.
            from core import gcs_sync
            gcs_sync.download_from_gcs()
        if not os.path.exists(db_path):
            issues.append("No local DB to check blog quiet streak.")
            return issues
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # Count trades per day for the last 10 days.
        rows = cur.execute("""
            SELECT substr(timestamp,1,10) as day, COUNT(*) as n
            FROM trades WHERE timestamp >= ?
            GROUP BY day ORDER BY day DESC
        """, ((_now_utc() - timedelta(days=10)).strftime("%Y-%m-%d"),)).fetchall()
        conn.close()
        trade_days = {r[0] for r in rows if r[1] > 0}
        # Count trading days (Mon-Fri) in the last 10 days.
        quiet_streak = 0
        d = _now_utc().date()
        for _ in range(10):
            if d.weekday() < 5:
                if d.isoformat() in trade_days:
                    quiet_streak = 0
                else:
                    quiet_streak += 1
            d -= timedelta(days=1)
        if quiet_streak >= 3:
            issues.append(f"Blog quiet streak: {quiet_streak} consecutive trading days "
                          f"with 0 trades.")
    except Exception as e:
        issues.append(f"Could not check blog quiet streak: {e}")
    return issues


def check_schedulers() -> list[str]:
    """Check enabled schedulers have fired recently (via Cloud Scheduler API)."""
    issues = []
    try:
        from google.cloud import scheduler_v1
    except ImportError:
        logger.warning("google-cloud-scheduler not installed; scheduler check skipped.")
        return issues
    try:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "agenttrade-us")
        location = os.getenv("CLOUD_RUN_REGION", "us-central1")
        client = scheduler_v1.CloudSchedulerClient()
        parent = f"projects/{project}/locations/{location}"
        jobs = client.list_jobs(request={"parent": parent})
        now = _now_utc()
        for job in jobs:
            if job.state != scheduler_v1.Job.State.ENABLED:
                continue
            # last_attempt_time may be None if never fired.
            if job.last_attempt_time is None:
                continue
            age_h = (now - job.last_attempt_time).total_seconds() / 3600.0
            if age_h > 48:
                issues.append(f"Scheduler {job.name.split('/')[-1]} has not fired in "
                              f"{age_h:.1f}h (> 48h).")
    except Exception as e:
        issues.append(f"Could not check schedulers: {e}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-trade cloud health check")
    parser.add_argument("--always", action="store_true",
                        help="Always send a Discord summary (even if healthy)")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    issues = []
    issues += check_db_staleness()
    issues += check_orphaned_positions()
    issues += check_blog_quiet_streak()
    issues += check_schedulers()

    if issues:
        msg = "🚨 **Agent-Trade Health Check**\n" + "\n".join(f"- {i}" for i in issues)
        logger.warning("Health issues found:\n%s", "\n".join(issues))
    else:
        msg = "✅ **Agent-Trade Health Check** — all clear."
        logger.info("All health checks passed.")

    if not args.no_discord and (issues or args.always):
        try:
            from core.discord_notifier import send_discord_message
            send_discord_message(msg)
        except Exception as e:
            logger.warning(f"Discord send failed: {e}")

    # Always exit 0: this is a monitoring job — the Discord alert IS the
    # notification. Exit 1 would trigger Cloud Run retries (maxRetries) and
    # spam duplicate alerts.
    return 0


if __name__ == "__main__":
    sys.exit(main())