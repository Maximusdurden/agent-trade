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


def load_daily(client, symbol: str, days_back: int):
    """Fetch daily bars + indicators for a symbol (used by reconcile)."""
    try:
        from sideload.runner_flat_base import load_daily as _ld
        return _ld(client, symbol, days_back)
    except Exception:
        try:
            df = client.get_historical_bars(symbol, limit=days_back, timeframe_str="day")
            if df is None or df.empty:
                return None
            if hasattr(df, "index") and isinstance(df.index, type(df.index)):
                df = df.copy()
            # Add ATR14 if missing.
            if "atr" not in df.columns:
                from sideload.backtest_swing_mean_reversion import add_indicators
                from sideload.backtest_swing_mean_reversion import BASELINE_CFG
                df = add_indicators(df, dict(BASELINE_CFG))
            return df
        except Exception as e:
            logger.warning(f"[Reconcile] load_daily failed for {symbol}: {e}")
            return None

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
    """Load flat-base state from local JSON (or empty if missing).

    Includes corruption self-heal: if the state file is unparseable (torn
    write / crashed container), quarantine the bad bytes and write a fresh
    empty state so the lane never runs blind and the next GCS upload
    overwrites the corrupted cloud blob.
    """
    if not os.path.exists(STATE_PATH):
        return _empty_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("active_positions", {})
        state.setdefault("realized_pnl_30d", 0.0)
        return state
    except Exception as e:
        ts = datetime.now(ET).strftime("%Y%m%d-%H%M%S")
        quarantine = f"{STATE_PATH}.corrupt-{ts}"
        try:
            os.replace(STATE_PATH, quarantine)
            logger.warning(
                f"Could not load flat-base state ({e}); quarantined to "
                f"{quarantine} and starting fresh. GCS will be overwritten "
                f"with a valid empty state on the next upload."
            )
        except Exception as qe:
            logger.warning(
                f"Could not load flat-base state ({e}) and quarantine failed "
                f"({qe}); starting fresh."
            )
        try:
            save_state(_empty_state())
        except Exception as se:
            logger.warning(f"Could not write fresh flat-base state: {se}")
        return _empty_state()


def save_state(state: dict) -> None:
    """Save flat-base state to local JSON (atomically)."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE_PATH)


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


def should_notify_cycle(symbols: list[str]) -> bool:
    """Return True when a cycle-complete notification is worth sending.

    The monitor scheduler runs hourly from 10:00-15:00 ET (6x/day) and used to
    send an identical "Runner cycle complete (N open positions)" message every
    single time. That is pure noise: the count rarely changes intraday.

    Notify only when the position SET changed, or on the first cycle of the
    day (a once-daily heartbeat proving the lane is alive).

    The marker is persisted into the state file, so it must be written BEFORE
    ``sync_up_to_gcs()`` — otherwise the next container downloads the older
    cloud state, loses the marker, and re-notifies.
    """
    state = load_state()
    today = datetime.now(ET).date().isoformat()
    current = sorted(symbols)
    last = state.get("last_cycle_notify") or {}
    if last.get("date") == today and last.get("symbols") == current:
        return False
    state["last_cycle_notify"] = {"date": today, "symbols": current}
    save_state(state)
    return True


def increment_bars_held() -> None:
    """Increment bars_held for all open positions (once per trading day).

    The monitor scheduler runs multiple times per day (hourly 10:00-15:00 ET),
    so guard on the last-increment date to avoid counting the same day multiple
    times (which would fire the 45-day time stop ~7.5 calendar days early).
    """
    from datetime import datetime
    state = load_state()
    today = datetime.now(ET).date().isoformat()
    if state.get("bars_held_last_date") == today:
        return
    for pos in state["active_positions"].values():
        pos["bars_held"] = pos.get("bars_held", 0) + 1
    state["bars_held_last_date"] = today
    save_state(state)


def prune_30d_pnl() -> None:
    """Reset the 30d realized PnL window (called on a rolling basis)."""
    state = load_state()
    state["realized_pnl_30d"] = 0.0
    save_state(state)


def reconcile_with_broker(client, entry_ref_price: dict | None = None) -> list[str]:
    """Adopt broker positions that are missing from local flat-base state.

    Cloud Run containers are ephemeral and the state file is only persisted to
    GCS at shutdown. If a container dies between the order fill and the state
    write (or the state write silently fails), the broker holds a position that
    the monitor does not know about — an ORPHAN. This function finds those
    orphans and adopts them into the state machine so the monitor can manage
    their exits (stops / scaling / time stop).

    For each broker position NOT already in ``active_positions``:
      - entry_price  = broker avg_entry_price (authoritative)
      - stop_loss    = entry - 1.5 * ATR14 (computed from daily bars)
      - target_3r/5r = entry + 3R / +5R where R = entry - stop
      - initial/remaining shares = broker qty

    Returns the list of adopted symbols.
    """
    try:
        positions = client.get_positions()
    except Exception as e:
        logger.warning(f"[Reconcile] Could not fetch broker positions: {e}")
        return []

    state = load_state()
    adopted = []

    # PRUNE: drop state entries the broker no longer holds.
    #
    # Without this the state only ever GROWS. A position closed by another lane
    # (or manually) stays in active_positions forever, so the reported count
    # drifts above reality. Observed 2026-11-02: state said 6 open, the broker
    # held 5 — AMD had been closed but was never removed, and every cycle
    # notification reported the phantom.
    #
    # Only prune when the broker call SUCCEEDED and returned a snapshot, so a
    # transient API failure (handled above by the early return) can never wipe
    # live state.
    broker_equity = set()
    for symbol, pos in positions.items():
        if "/" in symbol or getattr(pos, "is_option", False) or pos.get("is_option"):
            continue
        broker_equity.add(symbol.upper())

    stale = [s for s in state["active_positions"]
             if s.upper() not in broker_equity]
    for symbol in stale:
        logger.warning(
            f"[Reconcile] PRUNED stale position {symbol}: not held at broker "
            f"(state had {len(state['active_positions'])} open, "
            f"broker has {len(broker_equity)})."
        )
        del state["active_positions"][symbol]

    for symbol, pos in positions.items():
        # Only adopt equity positions (skip options / crypto).
        if "/" in symbol or getattr(pos, "is_option", False) or pos.get("is_option"):
            continue
        if symbol in state["active_positions"]:
            continue
        qty = float(pos.get("qty", 0.0) or 0.0)
        if qty <= 0:
            continue
        entry = float(pos.get("avg_entry_price", 0.0) or 0.0)
        if entry <= 0:
            continue

        # Compute ATR14 for the stop distance. NOTE: load_daily() returns raw
        # OHLCV only, so the indicator is computed here explicitly. Previously
        # this looked for an "atr" column that never existed, so EVERY adopted
        # position silently took the 6%-of-entry fallback (a 9% stop distance
        # after ATR_STOP_MULT). That inflated per-share risk, making positions
        # oversized, and parked the stop far below any real support.
        # 2026-10-14: NVDA adopted at a 9.00% stop instead of its true 3.77%
        # ATR stop, carrying ~$4.4k of risk on a $350-risk strategy.
        atr = None
        try:
            df = load_daily(client, symbol, fb_cfg.LOOKBACK_BARS)
            if df is not None and not df.empty:
                if "atr14" not in df.columns:
                    from sideload.runner_flat_base import _build_cfg
                    from sideload.backtest_flat_base import add_indicators
                    df = add_indicators(df, _build_cfg())
                if "atr14" in df.columns:
                    atr = float(df["atr14"].iloc[-1])
        except Exception as e:
            logger.warning(f"[Reconcile] ATR fetch failed for {symbol}: {e}")

        if atr is None or atr <= 0:
            # Fall back to ATR = 6% of entry when ATR is genuinely unavailable.
            # Note the stop distance this produces is ATR_STOP_MULT * 6% = 9%,
            # not 6%.
            logger.warning(f"[Reconcile] {symbol}: ATR14 unavailable; "
                           f"falling back to a 6% ATR assumption "
                           f"({fb_cfg.ATR_STOP_MULT * 6:.1f}% stop distance).")
            atr = entry * 0.06
        stop = entry - fb_cfg.ATR_STOP_MULT * atr
        r = entry - stop
        if r <= 0:
            logger.warning(f"[Reconcile] {symbol}: invalid R ({r:.2f}); skipping adoption.")
            continue

        pos_rec = {
            "cluster_id": f"FB-{symbol}-{datetime.now(ET).strftime('%Y%m%d')}-ADOPT",
            "entry_date": datetime.now(ET).strftime("%Y-%m-%d"),
            "bars_held": 0,
            "entry_price": entry,
            "initial_shares": qty,
            "remaining_shares": qty,
            "stop_loss": stop,
            "target_3r": entry + fb_cfg.TP1_R * r,
            "target_5r": entry + fb_cfg.TP2_R * r,
            "breakeven_active": False,
            "scale_1_filled": False,
            "scale_2_filled": False,
            "adopted": True,
        }
        state["active_positions"][symbol] = pos_rec
        adopted.append(symbol)
        logger.warning(f"[Reconcile] ADOPTED orphaned broker position {symbol}: "
                       f"qty={qty:.2f} entry={entry:.2f} stop={stop:.2f}")

    if adopted or stale:
        save_state(state)
    return adopted