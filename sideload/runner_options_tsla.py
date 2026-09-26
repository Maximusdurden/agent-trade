#!/usr/bin/env python3
"""TSLA Options Model A production runner (Phase 2).

Autonomous, self-contained production engine for TSLA front-week options,
implementing the validated Phase 1i parameters:

  - Entry premium:   $3.50 base, Delta 0.45.
  - PM volatility:   (PMH - PML) / PML >= 0.0035.
  - Setup trigger:   Sweep of PMH/PML with 1-min reversal candle crossing VWAP.
  - Profit target:   Single limit target at +45.0% premium gain.
  - Stop loss:       Hard cap at -22.0% (intrabar adverse extreme).
  - Time exit:       30 minutes maximum hold.

Flow:
  09:29:50  Pre-market volatility gate (04:00-09:29 ET bars -> PMH/PML).
  09:30-10:15  Model A liquidity sweep trigger (1-min bar close).
            On trigger: resolve front-week expiry, select strike, size, BUY.
  Post-fill  Active polling monitor loop (every 2s):
            - Take-profit (+45%): limit sell at bid.
            - Stop-loss (-22%): IOC limit sell at bid - $0.05, escalate to market.
            - Time stop (30m): market sell.
  State persisted to sideload/data/options_tsla_positions.json + GCS sync.
  Audit trail to sideload/data/options_tsla_fills.csv.

Usage:
    python -m sideload.runner_options_tsla --dry-run
    python -m sideload.runner_options_tsla --live
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from sideload.options_strike_sizer import (
    select_strike, resolve_front_week_expiry, BASE_ALLOCATION,
)
from sideload.options_execution_guards import (
    check_spread, check_can_trade, record_trade, check_macro_blackout,
)
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("RunnerOptionsTSLA")

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(PROJECT_ROOT, "sideload", "data")
STATE_PATH = os.path.join(DATA_DIR, "options_tsla_positions.json")
FILLS_PATH = os.path.join(DATA_DIR, "options_tsla_fills.csv")
GCS_STATE_BLOB = "options_tsla_positions.json"
GCS_FILLS_BLOB = "options_tsla_fills.csv"

# ---------------------------------------------------------------------------
# Locked Phase 1i parameters
# ---------------------------------------------------------------------------
TICKER = "TSLA"
ENTRY_PREMIUM = 3.50
DELTA = 0.45
TP_PCT = 0.45          # +45% take-profit.
STOP_PCT = 0.22        # -22% stop-loss.
MAX_HOLD_MINUTES = 30  # Time stop.

# Pre-market volatility gate.
PM_VOL_MIN = 0.0035

# Model A setup window.
MODEL_A_START = dtime(9, 30, 0)
MODEL_A_END = dtime(10, 15, 0)

# PM gate evaluation time.
PM_GATE_TIME = dtime(9, 29, 50)

# Polling loop.
POLL_INTERVAL_SECONDS = 2
STOP_ESCALATE_SECONDS = 2
STOP_IOC_SLIP = 0.05  # IOC limit sell at bid - $0.05.

INTRADAY_INTERVAL = "1min"


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _load_intraday(client: AlpacaClient, symbol: str, days_back: int = 5) -> pd.DataFrame:
    df = client.get_historical_bars_paginated(
        symbol, timeframe_str=INTRADAY_INTERVAL, days_back=days_back)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.sort_index()
    return df


def _load_daily(client: AlpacaClient, symbol: str, limit: int = 5) -> pd.DataFrame:
    df = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is not None:
        df.index = df.index.tz_convert(ET).tz_localize(None)
    df = df.sort_index()
    return df


def _vwap_series(day_bars: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
    vwap_start = day.replace(hour=9, minute=30)
    vwap_bars = day_bars[day_bars.index >= vwap_start]
    if vwap_bars.empty or not {"close", "volume"}.issubset(vwap_bars.columns):
        return pd.Series(dtype=float)
    tp = (vwap_bars["high"] + vwap_bars["low"] + vwap_bars["close"]) / 3.0
    cum_pv = (tp * vwap_bars["volume"]).cumsum()
    cum_v = vwap_bars["volume"].cumsum()
    return cum_pv / cum_v.replace(0, pd.NA)


def _vwap_at(vwap_series: pd.Series, ts) -> float | None:
    mask = vwap_series.index <= ts
    if not mask.any():
        return None
    vals = vwap_series[mask]
    if vals.empty:
        return None
    return float(vals.iloc[-1])


# ---------------------------------------------------------------------------
# GCS sync
# ---------------------------------------------------------------------------
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
        logger.warning(f"GCS bucket unavailable for options_tsla state: {e}")
        return None


def sync_down_from_gcs() -> None:
    """Download options_tsla state from GCS at startup."""
    bucket = _gcs_bucket()
    if bucket is None:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        blob = bucket.blob(GCS_STATE_BLOB)
        if blob.exists():
            blob.download_to_filename(STATE_PATH)
            logger.info(f"[GCS] Downloaded {GCS_STATE_BLOB} -> {STATE_PATH}")
    except Exception as e:
        logger.warning(f"[GCS] Could not download {GCS_STATE_BLOB}: {e}")


def sync_up_to_gcs() -> None:
    """Upload options_tsla state to GCS."""
    bucket = _gcs_bucket()
    if bucket is None:
        return
    if not os.path.exists(STATE_PATH):
        return
    try:
        bucket.blob(GCS_STATE_BLOB).upload_from_filename(STATE_PATH)
        logger.info(f"[GCS] Uploaded {STATE_PATH} -> {GCS_STATE_BLOB}")
    except Exception as e:
        logger.warning(f"[GCS] Could not upload {GCS_STATE_BLOB}: {e}")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _empty_state() -> dict:
    return {"active_position": None, "last_session": None}


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return _empty_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("active_position", None)
        state.setdefault("last_session", None)
        return state
    except Exception as e:
        logger.warning(f"Could not load options_tsla state: {e}")
        return _empty_state()


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def _append_fill(record: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fieldnames = ["timestamp", "session_date", "contract_symbol", "side",
                  "qty", "fill_price", "entry_premium", "reason", "pnl_pct",
                  "slippage", "note"]
    file_exists = os.path.exists(FILLS_PATH)
    with open(FILLS_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: record.get(k, "") for k in fieldnames})


# ---------------------------------------------------------------------------
# Pre-market volatility gate
# ---------------------------------------------------------------------------
def _pm_volatility_ok(client: AlpacaClient, session_date: str) -> dict:
    """Compute PMH/PML from 04:00-09:29 ET bars and check the 0.35% gate."""
    intraday = _load_intraday(client, TICKER, days_back=5)
    if intraday.empty:
        return {"pass": False, "reason": "No intraday data", "pmh": None, "pml": None}

    day = pd.Timestamp(session_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return {"pass": False, "reason": "No bars on session date", "pmh": None, "pml": None}

    pm_start = day_start.replace(hour=4, minute=0)
    pm_end = day_start.replace(hour=9, minute=29)
    pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
    if pm_bars.empty:
        return {"pass": False, "reason": "No pre-market bars", "pmh": None, "pml": None}

    pmh = float(pm_bars["high"].max())
    pml = float(pm_bars["low"].min())
    if pml <= 0:
        return {"pass": False, "reason": "Invalid PML", "pmh": pmh, "pml": pml}

    range_pct = (pmh - pml) / pml
    ok = range_pct >= PM_VOL_MIN
    return {
        "pass": ok,
        "reason": "OK" if ok else f"PM range {range_pct:.4%} < {PM_VOL_MIN:.4%}",
        "pmh": pmh,
        "pml": pml,
        "range_pct": round(range_pct * 100.0, 3),
    }


# ---------------------------------------------------------------------------
# Model A setup trigger
# ---------------------------------------------------------------------------
def _model_a_setup(client: AlpacaClient, session_date: str,
                   pmh: float, pml: float) -> dict | None:
    """Detect a liquidity sweep fade setup (Model A) on 1-min bar close."""
    intraday = _load_intraday(client, TICKER, days_back=5)
    if intraday.empty:
        return None

    day = pd.Timestamp(session_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return None

    vwap_series = _vwap_series(day_bars, day)
    a_start = day.replace(hour=MODEL_A_START.hour, minute=MODEL_A_START.minute)
    a_end = day.replace(hour=MODEL_A_END.hour, minute=MODEL_A_END.minute)
    window = day_bars[(day_bars.index >= a_start) & (day_bars.index <= a_end)]
    if window.empty:
        return None

    for ts, bar in window.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        vwap = _vwap_at(vwap_series, ts)

        # Bearish sweep: High > PMH, Close < PMH, Close < VWAP.
        if high > pmh and close < pmh and vwap is not None and close < vwap:
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close}

        # Bullish sweep: Low < PML, Close > PML, Close > VWAP.
        if low < pml and close > pml and vwap is not None and close > vwap:
            return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close}

    return None


# ---------------------------------------------------------------------------
# Quote polling
# ---------------------------------------------------------------------------
def _get_quote(client: AlpacaClient, occ_symbol: str) -> tuple[float | None, float | None]:
    """Return (bid, ask) for an OCC symbol, or (None, None) on failure."""
    try:
        info = client.get_latest_option_data([occ_symbol])
        if not info:
            return None, None
        q = info.get(occ_symbol)
        if q is None:
            return None, None
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        return (bid or None, ask or None)
    except Exception as e:
        logger.warning(f"Quote poll failed for {occ_symbol}: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Exit engine
# ---------------------------------------------------------------------------
def _dispatch_exit(client: AlpacaClient, occ_symbol: str, qty: int,
                   reason: str, limit_price: float | None = None,
                   dry_run: bool = False) -> dict:
    """Dispatch a SELL-to-close order for the held position."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would SELL {qty}x {occ_symbol} reason={reason} "
                    f"limit={limit_price}")
        return {"status": "dry_run", "reason": reason, "limit_price": limit_price}

    if limit_price is not None:
        return client.place_option_order(
            symbol=occ_symbol, qty=qty, side="sell", limit_price=limit_price)
    return client.close_option_position(occ_symbol)


def _monitor_position(client: AlpacaClient, pos: dict, dry_run: bool = False) -> dict:
    """Poll the held position and execute exits until flat or time stop.

    Returns a dict describing the exit (fill price, reason, pnl_pct).
    """
    occ = pos["contract_symbol"]
    qty = int(pos["contracts"])
    entry_premium = float(pos["entry_premium"])
    target_premium = float(pos["target_premium"])
    stop_premium = float(pos["stop_premium"])
    entry_time = pd.Timestamp(pos["entry_time"])
    max_exit_time = entry_time + timedelta(minutes=MAX_HOLD_MINUTES)

    logger.info(f"Monitoring {occ} {qty}x entry={entry_premium:.2f} "
                f"target={target_premium:.2f} stop={stop_premium:.2f}")

    while True:
        now = datetime.now(ET)
        if now >= max_exit_time:
            logger.warning(f"[TIME STOP] {occ} held >= {MAX_HOLD_MINUTES}m; market sell.")
            result = _dispatch_exit(client, occ, qty, "time_stop", dry_run=dry_run)
            return _finalize_exit(pos, result, "time_stop", dry_run)

        bid, ask = _get_quote(client, occ)
        if bid is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Take-profit (+45%).
        if bid >= target_premium:
            logger.info(f"[TAKE PROFIT] {occ} bid {bid:.2f} >= target {target_premium:.2f}; "
                        f"limit sell at {bid:.2f}")
            result = _dispatch_exit(client, occ, qty, "take_profit",
                                    limit_price=bid, dry_run=dry_run)
            return _finalize_exit(pos, result, "take_profit", dry_run)

        # Stop-loss (-22%).
        if bid <= stop_premium:
            logger.warning(f"[STOP] {occ} bid {bid:.2f} <= stop {stop_premium:.2f}; "
                           f"IOC limit sell at {bid - STOP_IOC_SLIP:.2f}")
            if dry_run:
                result = _dispatch_exit(client, occ, qty, "stop_loss",
                                        limit_price=bid - STOP_IOC_SLIP, dry_run=True)
                return _finalize_exit(pos, result, "stop_loss", dry_run)
            # IOC limit sell at bid - $0.05.
            try:
                result = client.place_option_order(
                    symbol=occ, qty=qty, side="sell",
                    limit_price=round(bid - STOP_IOC_SLIP, 2))
            except Exception as e:
                logger.error(f"[STOP] IOC limit sell failed for {occ}: {e}")
                result = {"status": "failed", "error": str(e)}
            # If not filled after 2s, escalate to market sell.
            status = str(result.get("status", "")).lower()
            filled_qty = float(result.get("filled_qty", 0) or 0)
            if status in ("filled", "accepted") and filled_qty >= qty:
                return _finalize_exit(pos, result, "stop_loss", dry_run)
            time.sleep(STOP_ESCALATE_SECONDS)
            logger.warning(f"[STOP] IOC partial/no fill for {occ}; escalating to market sell.")
            try:
                market_result = client.close_option_position(occ)
                return _finalize_exit(pos, market_result, "stop_loss_escalated", dry_run)
            except Exception as e:
                logger.error(f"[STOP] Emergency market sell failed for {occ}: {e}")
                return _finalize_exit(pos, {"status": "escalation_failed", "error": str(e)},
                                      "stop_loss_escalated", dry_run)

        time.sleep(POLL_INTERVAL_SECONDS)


def _finalize_exit(pos: dict, result: dict, reason: str, dry_run: bool) -> dict:
    """Build the exit record, append to audit trail, and return it."""
    entry_premium = float(pos["entry_premium"])
    fill_price = result.get("filled_avg_price")
    if fill_price is None and dry_run:
        # In dry-run, estimate fill at the target/stop premium.
        if reason == "take_profit":
            fill_price = float(pos["target_premium"])
        elif reason in ("stop_loss", "stop_loss_escalated"):
            fill_price = float(pos["stop_premium"])
        else:
            fill_price = entry_premium

    pnl_pct = None
    slippage = None
    if fill_price is not None:
        pnl_pct = round((float(fill_price) - entry_premium) / entry_premium * 100.0, 2)
        if reason == "take_profit":
            slippage = round(float(fill_price) - float(pos["target_premium"]), 2)
        elif reason in ("stop_loss", "stop_loss_escalated"):
            slippage = round(float(fill_price) - float(pos["stop_premium"]), 2)

    record = {
        "timestamp": datetime.now(ET).isoformat(),
        "session_date": pos.get("session_date", ""),
        "contract_symbol": pos["contract_symbol"],
        "side": "sell",
        "qty": pos["contracts"],
        "fill_price": fill_price,
        "entry_premium": entry_premium,
        "reason": reason,
        "pnl_pct": pnl_pct,
        "slippage": slippage,
        "note": "dry_run" if dry_run else "live",
    }
    _append_fill(record)
    logger.info(f"[EXIT] {pos['contract_symbol']} reason={reason} "
                f"fill={fill_price} pnl={pnl_pct}%")
    return {"reason": reason, "fill_price": fill_price, "pnl_pct": pnl_pct,
            "result": result}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def _enter_position(client: AlpacaClient, session_date: str, setup: dict,
                    dry_run: bool = False) -> dict | None:
    """Resolve expiry, select strike, size, and BUY the option."""
    direction = setup["direction"]
    opt_type = "call" if direction == "BULLISH" else "put"

    # 1. Resolve front-week expiry.
    expiry = resolve_front_week_expiry()
    logger.info(f"Resolved front-week expiry: {expiry}")

    # 2. Select strike + size.
    strike_info = select_strike(client, TICKER, direction, expiry,
                                current_price=setup["entry_price"])
    if not strike_info or not strike_info.get("selected_occ"):
        logger.warning(f"No tradeable {opt_type} contract for {TICKER} on {expiry}.")
        return None

    occ = strike_info["selected_occ"]
    contracts = int(strike_info.get("contracts", 0))
    if contracts < 1:
        logger.warning(f"Contract sizing rejected {occ} (contracts=0).")
        return None

    ask = float(strike_info["ask"])
    # Spread gate.
    spread = check_spread(float(strike_info["bid"]), ask)
    if not spread["pass"]:
        logger.warning(f"Spread gate failed for {occ}: {spread['reason']}")
        return None

    entry_premium = ask
    target_premium = round(entry_premium * (1.0 + TP_PCT), 2)
    stop_premium = round(entry_premium * (1.0 - STOP_PCT), 2)

    logger.info(f"[ENTRY] {direction} {occ} {contracts}x ask={ask:.2f} "
                f"target={target_premium:.2f} stop={stop_premium:.2f}")

    if dry_run:
        fill_price = ask
        logger.info(f"[DRY-RUN] Would BUY {contracts}x {occ} at {fill_price:.2f}")
        result = {"status": "dry_run", "filled_avg_price": fill_price}
    else:
        result = client.place_option_order(
            symbol=occ, qty=contracts, side="buy", limit_price=ask)
        fill_price = result.get("filled_avg_price")

    pos = {
        "contract_symbol": occ,
        "entry_time": datetime.now(ET).isoformat(),
        "entry_premium": entry_premium,
        "contracts": contracts,
        "target_premium": target_premium,
        "stop_premium": stop_premium,
        "direction": direction,
        "session_date": session_date,
        "entry_price": setup["entry_price"],
    }
    return pos


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run_session(session_date: str, dry_run: bool = True) -> dict:
    """Run one TSLA Model A session."""
    client = AlpacaClient()
    sync_down_from_gcs()
    state = load_state()

    # Circuit breaker: max 1 trade/day.
    can_trade = check_can_trade(session_date)
    if not can_trade["can_trade"]:
        logger.info(f"Circuit breaker: {can_trade['reason']}")
        return {"status": "skipped", "reason": can_trade["reason"]}

    # Macro blackout (no scheduled releases by default).
    blackout = check_macro_blackout(session_date)
    if blackout["blocked"]:
        logger.info(f"Macro blackout: {blackout['reason']}")
        return {"status": "skipped", "reason": blackout["reason"]}

    # 1. Pre-market volatility gate.
    pm = _pm_volatility_ok(client, session_date)
    if not pm["pass"]:
        logger.info(f"PM volatility gate: {pm['reason']}")
        return {"status": "disarmed", "reason": pm["reason"], "pm": pm}

    # 2. Model A setup trigger.
    setup = _model_a_setup(client, session_date, pm["pmh"], pm["pml"])
    if setup is None:
        logger.info("No Model A setup triggered in window.")
        return {"status": "no_setup", "pm": pm}

    # 3. Enter position.
    pos = _enter_position(client, session_date, setup, dry_run=dry_run)
    if pos is None:
        return {"status": "no_entry", "pm": pm, "setup": setup}

    # 4. Persist state + record trade.
    state["active_position"] = pos
    state["last_session"] = session_date
    save_state(state)
    sync_up_to_gcs()
    record_trade(session_date, stopped_out=False)

    # 5. Monitor position until exit.
    exit_info = _monitor_position(client, pos, dry_run=dry_run)

    # 6. Clear state.
    state["active_position"] = None
    save_state(state)
    sync_up_to_gcs()

    return {"status": "completed", "pm": pm, "setup": setup, "position": pos,
            "exit": exit_info}


def main() -> None:
    parser = argparse.ArgumentParser(description="TSLA Options Model A runner (Phase 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate entry/exit without placing real orders.")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (default is dry-run).")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    dry_run = not args.live
    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        result = run_session(args.date, dry_run=dry_run)
        print(json.dumps(result, indent=2, default=str))

        if not args.no_discord:
            try:
                status = result.get("status", "unknown")
                lines = [f"**TSLA Options Model A — {args.date}**",
                         f"Mode: {'DRY-RUN' if dry_run else 'LIVE'} | Status: {status}"]
                if result.get("exit"):
                    ex = result["exit"]
                    lines.append(f"Exit: {ex.get('reason')} | PnL: {ex.get('pnl_pct')}%")
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "runner_options_tsla",
                              {"date": args.date, "dry_run": dry_run})
        logger.exception("runner_options_tsla failed")
        raise


if __name__ == "__main__":
    main()
