#!/usr/bin/env python3
"""Swing fill verification — checks realized slippage vs the $0.05 assumption.

Reads swing_fills.csv (written by run_swing_trader.py) and reports the realized
fill-price delta vs the official day-t open. Alerts (Discord) if realized
slippage breaches the modeled $0.05/share threshold.

Usage:
    python -m sideload.verify_swing_fills --no-discord
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.discord_notifier import send_discord_message

logger = logging.getLogger("VerifySwingFills")

FILL_LOG = os.path.join(PROJECT_ROOT, "sideload", "data", "swing_fills.csv")
# Modeled slippage threshold ($/share).
SLIPPAGE_THRESHOLD = 0.05


def _sync_fills_from_gcs() -> None:
    """Download swing_fills.csv from GCS before auditing.

    The weekly audit runs in its own ephemeral Cloud Run container, so it must
    pull the persisted fills from GCS (they are not local to this container).
    """
    try:
        from core.gcs_sync import get_gcs_client
        client = get_gcs_client()
        bucket_name = os.getenv("GCS_BUCKET_NAME")
        if client is None or not bucket_name:
            logger.info("GCS unavailable; using local fill log if present.")
            return
        bucket = client.bucket(bucket_name)
        blob = bucket.blob("swing_fills.csv")
        if blob.exists():
            os.makedirs(os.path.dirname(FILL_LOG), exist_ok=True)
            blob.download_to_filename(FILL_LOG)
            logger.info(f"[GCS] Downloaded swing_fills.csv for audit.")
    except Exception as e:
        logger.warning(f"[GCS] Could not download fills for audit: {e}")


def verify_fills(fill_log: str = FILL_LOG) -> dict:
    if not os.path.exists(fill_log):
        return {"error": f"no fill log at {fill_log}", "fills": 0}
    rows = []
    with open(fill_log, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)
    if not rows:
        return {"fills": 0, "message": "fill log empty (no fills yet)"}

    # Compute realized slippage vs the reference price.
    # New schema: side, fill_price, ref_price, slip. Old schema: fill_price,
    # prev_close, open_print, slip_vs_close, slip_vs_open.
    deltas = []
    breaches = []
    for r in rows:
        try:
            fill = float(r.get("fill_price", 0))
            if r.get("ref_price"):
                ref = float(r["ref_price"])
            else:
                ref = float(r.get("open_print", 0))
            delta = fill - ref
            deltas.append(delta)
            if abs(delta) > SLIPPAGE_THRESHOLD:
                breaches.append({"symbol": r.get("symbol"), "ts": r.get("ts"),
                                 "delta": delta})
        except (TypeError, ValueError):
            continue

    if not deltas:
        return {"fills": len(rows), "message": "no numeric fill deltas"}

    import numpy as np
    deltas = np.array(deltas)
    return {
        "fills": len(rows),
        "mean_delta": float(deltas.mean()),
        "median_delta": float(np.median(deltas)),
        "max_delta": float(deltas.max()),
        "min_delta": float(deltas.min()),
        "breach_count": len(breaches),
        "breaches": breaches,
        "threshold": SLIPPAGE_THRESHOLD,
        "within_threshold": len(breaches) == 0,
    }


def _format_weekly_block(r: dict) -> str:
    """Format a Discord status block for the weekly fill audit."""
    status = "✅ WITHIN THRESHOLD" if r["within_threshold"] else "⚠️ BREACH"
    lines = [
        "**Swing Fill Audit (weekly)**",
        f"Status: {status}",
        f"Fills logged: {r['fills']}",
        f"Mean fill delta (Fill-Ref): ${r['mean_delta']:.4f}",
        f"Median: ${r['median_delta']:.4f} | Max: ${r['max_delta']:.4f} | Min: ${r['min_delta']:.4f}",
        f"Threshold: ${r['threshold']:.2f}/share",
        f"Breaches: {r['breach_count']}",
    ]
    for b in r.get("breaches", [])[:5]:
        lines.append(f"  • {b['symbol']} {b['ts']}: ${b['delta']:.4f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify swing fill slippage")
    parser.add_argument("--weekly", action="store_true",
                        help="Weekly audit: sync fills from GCS, post status block to Discord")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-swing-fill-verify")

    try:
        # Weekly audit runs in its own ephemeral container — pull fills from GCS.
        if args.weekly:
            _sync_fills_from_gcs()
        r = verify_fills()
        if "error" in r:
            logger.info(f"Fill verify: {r['error']}")
            return
        print(f"\n=== Swing Fill Verification ===")
        print(f"  fills logged: {r['fills']}")
        if r.get("fills", 0) == 0:
            print(f"  {r.get('message', 'no fills')}")
            return
        print(f"  mean fill delta (Fill-Ref): ${r['mean_delta']:.4f}")
        print(f"  median: ${r['median_delta']:.4f} | max: ${r['max_delta']:.4f} | min: ${r['min_delta']:.4f}")
        print(f"  threshold: ${r['threshold']:.2f}")
        print(f"  breaches: {r['breach_count']}")
        for b in r.get("breaches", []):
            print(f"    {b['symbol']} {b['ts']}: delta=${b['delta']:.4f}")
        print(f"  WITHIN THRESHOLD: {r['within_threshold']}")

        if not args.no_discord:
            try:
                if args.weekly:
                    send_discord_message(_format_weekly_block(r))
                else:
                    status = "OK" if r["within_threshold"] else "BREACH"
                    send_discord_message(
                        f"[SWING FILLS] {status}: {r['fills']} fills, "
                        f"mean delta=${r['mean_delta']:.4f}, breaches={r['breach_count']}")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Fill verify failed: {e}")
        log_exception_to_jira(e, "Swing Fill Verify Failure")
        raise


if __name__ == "__main__":
    main()