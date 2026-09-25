"""Flat Base Breakout state tracking engine (Phase 2 production).

Persists open flat-base positions to a local JSON file and synchronizes with
Google Cloud Storage (GCS) so state survives ephemeral Cloud Run containers,
matching the `swing_positions.json` pattern.

State schema:
    {
      "active_positions": {
        "NVDA": {
          "cluster_id": "FB-NVDA-20260925-001",
          "entry_date": "2026-09-25",
          "bars_held": 0,
          "entry_price": 125.50,
          "initial_shares": 18.66,
          "remaining_shares": 18.66,
          "stop_loss": 106.75,
          "target_3r": 181.75,
          "target_5r": 219.25,
          "breakeven_active": false,
          "scale_1_filled": false,
          "scale_2_filled": false
        }
      },
      "realized_pnl_30d": 0.0
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sideload import config_sideload_flatbase as fb_cfg

logger = logging.getLogger("FlatBaseState")

ET = ZoneInfo("America/New_York")

# Local state file path (sideload/data/flatbase_positions.json).
STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sideload", "data", fb_cfg.STATE_FILE,
)


def _empty_state() -> dict:
    return {"active_positions": {}, "realized_pnl_30d": 0.0}


def _gcs_bucket():
    """Return the GCS bucket for flat-base state, or None if unavailable."""
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
        logger.warning(f"GCS bucket unavailable for flat-base state: {e}")
        return None


def sync_down_from_gcs() -> None:
    """Download flat-base state from GCS at container startup."""
    bucket = _gcs_bucket()
    if bucket is None:
        return
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        blob = bucket.blob(fb_cfg.GCS_STATE_BLOB)
        if blob.exists():
            blob.download_to_filename(STATE_PATH)
            logger.info(f"[GCS] Downloaded {fb_cfg.GCS_STATE_BLOB} -> {STATE_PATH}")
    except Exception as e:
        logger.warning(f"[GCS] Could not download {fb_cfg.GCS_STATE_BLOB}: {e}")


def sync_up_to_gcs() -> None:
    """Upload flat-base state to GCS at container shutdown."""
    bucket = _gcs_bucket()
    if bucket is None:
        return
    if not os.path.exists(STATE_PATH):
        return
    try:
        bucket.blob(fb_cfg.GCS_STATE_BLOB).upload_from_filename(STATE_PATH)
        logger.info(f"[GCS] Uploaded {STATE_PATH} -> {fb_cfg.GCS_STATE_BLOB}")
    except Exception as e:
        logger.warning(f"[GCS] Could not upload {fb_cfg.GCS_STATE_BLOB}: {e}")


def load_state() -> dict:
    """Load flat-base state from local JSON (or empty if missing)."""
    if not os.path.exists(STATE_PATH):
        return _empty_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("active_positions", {})
        state.setdefault("realized_pnl_30d", 0.0)
        return state
    except Exception as e:
        logger.warning(f"Could not load flat-base state: {e}")
        return _empty_state()


def save_state(state: dict) -> None:
    """Save flat-base state to local JSON."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def open_position(symbol: str, entry_price: float, initial_shares: float,
                  stop_loss: float, target_3r: float, target_5r: float) -> dict:
    """Create a new position record and persist it."""
    state = load_state()
    now = datetime.now(ET)
    cluster_id = f"FB-{symbol}-{now.strftime('%Y%m%d')}-001"
    pos = {
        "cluster_id": cluster_id,
        "entry_date": now.strftime("%Y-%m-%d"),
        "bars_held": 0,
        "entry_price": entry_price,
        "initial_shares": initial_shares,
        "remaining_shares": initial_shares,
        "stop_loss": stop_loss,
        "target_3r": target_3r,
        "target_5r": target_5r,
        "breakeven_active": False,
        "scale_1_filled": False,
        "scale_2_filled": False,
    }
    state["active_positions"][symbol] = pos
    save_state(state)
    logger.info(f"[FlatBase] Opened {symbol}: {cluster_id} entry={entry_price:.2f} "
                f"shares={initial_shares:.2f} stop={stop_loss:.2f}")
    return pos


def close_position(symbol: str, realized_pnl: float) -> None:
    """Close a position, add realized PnL to the 30d window, and persist."""
    state = load_state()
    if symbol in state["active_positions"]:
        del state["active_positions"][symbol]
    state["realized_pnl_30d"] = state.get("realized_pnl_30d", 0.0) + realized_pnl
    save_state(state)
    logger.info(f"[FlatBase] Closed {symbol}: realized PnL ${realized_pnl:.2f}")


def update_position(symbol: str, **updates) -> None:
    """Apply field updates to an open position and persist."""
    state = load_state()
    if symbol in state["active_positions"]:
        state["active_positions"][symbol].update(updates)
        save_state(state)


def get_open_positions() -> dict:
    """Return the dict of open positions keyed by symbol."""
    return load_state()["active_positions"]


def count_open_positions() -> int:
    """Number of currently open flat-base positions."""
    return len(get_open_positions())


def increment_bars_held() -> None:
    """Increment bars_held for all open positions (called once per trading day)."""
    state = load_state()
    for pos in state["active_positions"].values():
        pos["bars_held"] = pos.get("bars_held", 0) + 1
    save_state(state)


def prune_30d_pnl() -> None:
    """Reset the 30d realized PnL window (called on a rolling basis)."""
    state = load_state()
    state["realized_pnl_30d"] = 0.0
    save_state(state)