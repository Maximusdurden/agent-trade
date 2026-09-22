#!/usr/bin/env python3
"""Swing RSI-2 mean-reversion scheduler entrypoint (Cloud Run / local).

Wires the validated swing strategy into the operational pipeline. A single
Cloud Scheduler trigger (every 15 min Mon-Fri) starts this job, which:

  - 4:05 PM ET (EOD): runs the daily signal scan across the refined universe,
    computes available cash slots (3 - open positions), and stages
    Market-On-Open orders for the top open slots.
  - 9:35 AM - 3:55 PM ET (intraday): monitors open positions for exits
    (Close > SMA5, Catastrophic Stop at Entry - 2.0*ATR14, or Day-5 Time Exit).
  - Logs every fill price vs prior-day close and open print to validate the
    $0.05 slippage assumption (slippage & fill logger).

Usage:
    python run_swing_trader.py --scan          # EOD 4:05 PM signal scan
    python run_swing_trader.py --monitor       # intraday exit monitor
    python run_swing_trader.py --dry-run       # compute, no orders/writes
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("RunSwingTrader")

ET = ZoneInfo("America/New_York")

# State file for open positions (paper-trading staging).
POSITIONS_FILE = os.path.join(PROJECT_ROOT, "sideload", "data", "swing_positions.json")
# Slippage/fill log (validates the $0.05 assumption).
FILL_LOG = os.path.join(PROJECT_ROOT, "sideload", "data", "swing_fills.csv")
# GCS blob names for the swing state files (persisted across ephemeral Cloud Run
# containers).
GCS_POSITIONS_BLOB = "swing_positions.json"
GCS_FILLS_BLOB = "swing_fills.csv"

MAX_SLOTS = 3


def _now_et() -> datetime:
    return datetime.now(ET)


def _gcs_bucket():
    """Return the GCS bucket for swing state, or None if unavailable."""
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
        logger.warning(f"GCS bucket unavailable for swing state: {e}")
        return None


def _sync_down_from_gcs() -> None:
    """Download swing state files from GCS at container startup.

    Cloud Run containers are ephemeral — local files are destroyed when the job
    finishes. Pull the persisted positions/fills from GCS so each run starts
    from the last-known state instead of a blank slate.
    """
    bucket = _gcs_bucket()
    if bucket is None:
        return
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    for local, blob in ((POSITIONS_FILE, GCS_POSITIONS_BLOB),
                        (FILL_LOG, GCS_FILLS_BLOB)):
        try:
            gblob = bucket.blob(blob)
            if gblob.exists():
                gblob.download_to_filename(local)
                logger.info(f"[GCS] Downloaded {blob} -> {local}")
        except Exception as e:
            logger.warning(f"[GCS] Could not download {blob}: {e}")


def _sync_up_to_gcs() -> None:
    """Upload swing state files to GCS at container shutdown.

    Persists positions/fills so the next ephemeral run can pick up where this
    one left off.
    """
    bucket = _gcs_bucket()
    if bucket is None:
        return
    for local, blob in ((POSITIONS_FILE, GCS_POSITIONS_BLOB),
                        (FILL_LOG, GCS_FILLS_BLOB)):
        if not os.path.exists(local):
            continue
        try:
            bucket.blob(blob).upload_from_filename(local)
            logger.info(f"[GCS] Uploaded {local} -> {blob}")
        except Exception as e:
            logger.warning(f"[GCS] Could not upload {blob}: {e}")


def _is_equity_market_hours() -> tuple[bool, str]:
    """Mon-Fri 09:30-16:00 ET gate (equity strategy)."""
    now = _now_et()
    if now.weekday() >= 5:
        return False, f"Weekend ({now.strftime('%A')})."
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < start:
        return False, f"Pre-market (ET {now.strftime('%H:%M')})."
    if now > end:
        return False, f"Post-market (ET {now.strftime('%H:%M')})."
    return True, "Market open."


def _load_positions() -> dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            logger.warning(f"Could not load positions file: {e}")
    return {}


def _save_positions(positions: dict) -> None:
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    with open(POSITIONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(positions, fh, indent=2)


def _log_fill(symbol: str, fill_price: float, ref_price: float, side: str = "buy") -> None:
    """Record a fill for slippage validation.

    fill_price = actual broker fill (from the Alpaca order).
    ref_price  = the theoretical reference price:
                 - buy:  the day-t open print (fill-at-open assumption)
                 - sell: the exit trigger price (SMA5 level / cat stop / close)
    slip       = fill_price - ref_price (realized execution slippage vs model).
    """
    os.makedirs(os.path.dirname(FILL_LOG), exist_ok=True)
    import csv
    new = not os.path.exists(FILL_LOG)
    with open(FILL_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "symbol", "side", "fill_price", "ref_price", "slip"])
        w.writerow([
            _now_et().isoformat(), symbol, side,
            round(fill_price, 2), round(ref_price, 2),
            round(fill_price - ref_price, 2),
        ])


def place_market_on_open_order(client, symbol: str, qty: float, side: str = "buy") -> dict:
    """Submit a Market-On-Open (OPG) order via Alpaca.

    Uses TimeInForce.OPG so the fill executes in the 9:30 AM NYSE/Nasdaq opening
    auction cross, NOT pre-market extended hours. This matches the backtested
    'fill at open + $0.05 slippage' assumption.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side_enum,
        time_in_force=TimeInForce.OPG,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        logger.info(f"[OPG] Submitted {side} {qty} {symbol} (time_in_force=OPG)")
        return {"symbol": symbol, "qty": qty, "side": side, "status": "submitted",
                "order_id": getattr(order, "id", None)}
    except Exception as e:
        logger.error(f"[OPG] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": side, "status": "failed", "error": str(e)}


def place_market_sell_order(client, symbol: str, qty: float) -> dict:
    """Submit an immediate market SELL order via Alpaca and poll for the fill.

    Returns the order dict including the actual ``filled_avg_price`` so the
    exit fill is logged empirically (not the theoretical trigger price).
    """
    import time
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        order_id = str(getattr(order, "id", ""))
        logger.info(f"[SELL] Submitted {qty} {symbol} (market, DAY) order_id={order_id}")
        # Poll for the fill to capture the empirical execution price.
        filled_price = None
        status_str = str(getattr(order, "status", ""))
        for _ in range(10):
            try:
                updated = client.trading_client.get_order_by_id(order_id=order_id)
                status_str = str(getattr(updated, "status", ""))
                if getattr(updated, "filled_avg_price", None) is not None:
                    filled_price = float(updated.filled_avg_price)
                if status_str.lower() in ("filled", "partially_filled"):
                    break
            except Exception as poll_err:
                logger.warning(f"[SELL] Poll error for {symbol}: {poll_err}")
            time.sleep(0.5)
        return {"symbol": symbol, "qty": qty, "side": "sell",
                "status": status_str, "order_id": order_id,
                "filled_avg_price": filled_price}
    except Exception as e:
        logger.error(f"[SELL] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": "sell", "status": "failed",
                "order_id": None, "filled_avg_price": None, "error": str(e)}


def _get_position_qty(client, symbol: str, fallback: float | None = None) -> float:
    """Get the actual filled qty for a symbol from Alpaca (source of truth).

    Falls back to the staged qty if the position can't be fetched.
    """
    try:
        positions = client.trading_client.get_all_positions()
        for p in positions:
            if str(getattr(p, "symbol", "")).upper() == symbol.upper():
                return float(getattr(p, "qty", 0) or 0)
    except Exception as e:
        logger.warning(f"[SELL] Could not fetch Alpaca qty for {symbol}: {e}")
    return fallback or 0.0


def _latest_open_print(client, symbol: str) -> float:
    """Latest daily bar's open print (reference for buy-fill slippage)."""
    try:
        from core.strategies.swing_rsi2_mean_reversion import load_daily
        df = load_daily(client, symbol, 5)
        if not df.empty:
            return float(df["open"].iloc[-1])
    except Exception as e:
        logger.warning(f"[FILL] Could not fetch open print for {symbol}: {e}")
    return 0.0


def _ensure_cat_stop(client, pos: dict) -> float | None:
    """Compute the catastrophic stop for a position if not already set.

    cat_stop = actual_entry_fill - 2.0 * ATR14, matching the backtest's
    structural stop (entry - catastrophic_atr_mult * ATR14). The entry fill
    price is read from the buy order; ATR14 comes from the daily data.
    """
    if pos.get("cat_stop") is not None:
        return pos["cat_stop"]
    entry_fill = None
    order_id = pos.get("order_id")
    if order_id:
        try:
            order = client.trading_client.get_order_by_id(order_id=order_id)
            entry_fill = float(getattr(order, "filled_avg_price", 0) or 0)
        except Exception as e:
            logger.warning(f"[CATSTOP] Could not fetch entry fill for {pos.get('symbol')}: {e}")
    if not entry_fill or entry_fill <= 0:
        return None
    try:
        from core.strategies.swing_rsi2_mean_reversion import (
            load_daily, add_indicators, BASELINE_CFG, CAT_STOP_ATR_MULT,
        )
        df = add_indicators(load_daily(client, pos["symbol"], 30), dict(BASELINE_CFG))
        atr = float(df["atr"].iloc[-1])
    except Exception as e:
        logger.warning(f"[CATSTOP] Could not compute ATR for {pos.get('symbol')}: {e}")
        return None
    if atr <= 0:
        return None
    return entry_fill - CAT_STOP_ATR_MULT * atr


def run_eod_scan(dry_run: bool = False) -> dict:
    """EOD 4:05 PM signal scan + stage MOO orders for open slots."""
    from core.strategies.swing_rsi2_mean_reversion import (
        scan_signals, stage_orders, MAX_SLOTS, SLOT_SIZE_PCT,
    )
    from core.alpaca_client import AlpacaClient

    client = AlpacaClient()
    candidates = scan_signals(client)
    positions = _load_positions()
    open_slots = max(0, MAX_SLOTS - len(positions))
    logger.info(f"EOD scan: {len(candidates)} candidates, {len(positions)} open, "
                f"{open_slots} slots available")

    if open_slots <= 0:
        logger.info("No open slots; skipping staging.")
        return {"candidates": len(candidates), "staged": 0, "open_slots": 0}

    staged = stage_orders(candidates, open_slots, dry_run=dry_run)
    if not dry_run:
        # Submit OPG market-on-open orders for the top slots.
        account = client.get_account_state()
        equity = float(account.get("equity", 0.0) or 0.0)
        for s in staged:
            # Position size = SLOT_SIZE_PCT of equity, converted to shares.
            notional = SLOT_SIZE_PCT * equity
            # Approximate qty from the signal close (fill at open may differ).
            # Cast to INTEGER (math.floor) to avoid 422 errors if fractional
            # share trading is not enabled on the account.
            qty = max(1, math.floor(notional / s.get("close", 1.0)))
            res = place_market_on_open_order(client, s["symbol"], qty, "buy")
            s["order_result"] = res
            # Record staged position (paper-trade) ONLY if the order was accepted.
            if res.get("status") in ("submitted", "accepted", "held"):
                positions[s["symbol"]] = {
                    "entry_ts": _now_et().isoformat(),
                    "signal_date": s["signal_date"],
                    "rsi": s["rsi"],
                    "order_id": res.get("order_id"),
                    "qty": qty,  # staged qty; actual fill verified at sell time
                    "cat_stop": None,  # filled at open, computed in monitor
                    "day0": _now_et().date().isoformat(),
                }
            else:
                logger.warning(f"[STAGE] Order for {s['symbol']} not accepted; not recording position.")
        _save_positions(positions)
    return {"candidates": len(candidates), "staged": len(staged), "open_slots": open_slots}


def _prune_unfilled_orders(client, positions: dict) -> list[str]:
    """Prune staged orders that failed to fill in the opening cross.

    Checks each staged position's order status via Alpaca. If the order was
    cancelled/expired (did not fill at the 9:30 AM open), remove it from
    positions so the cash slot is freed and no phantom tracking occurs.
    Returns the list of pruned symbols.
    """
    pruned = []
    for sym, pos in list(positions.items()):
        order_id = pos.get("order_id")
        if not order_id:
            # No order id -> cannot verify; keep (may be a legacy entry).
            continue
        try:
            order = client.trading_client.get_order_by_id(order_id=order_id)
            status = str(getattr(order, "status", "")).lower()
            filled = float(getattr(order, "filled_qty", 0) or 0)
            # Filled -> log the actual buy fill (entry slippage) and keep.
            if filled > 0:
                fill_px = float(getattr(order, "filled_avg_price", 0) or 0)
                if fill_px > 0:
                    _log_fill(sym, fill_px, _latest_open_print(client, sym), side="buy")
                continue
            if status in ("cancelled", "expired", "canceled", "rejected", "done_for_day"):
                logger.warning(f"[PRUNE] {sym} order {order_id} status={status}, no fill. Pruning.")
                pruned.append(sym)
                del positions[sym]
        except Exception as e:
            logger.warning(f"[PRUNE] Could not check {sym} order {order_id}: {e}")
    return pruned


def run_monitor(dry_run: bool = False) -> dict:
    """Intraday monitor: check open positions for exits + submit sells.

    Exit segmentation (matches backtest timing):
      - Catastrophic stop: evaluated on EVERY session check (9:35 AM - 3:55 PM).
      - SMA5 touch + Day-5 time exit: evaluated ONLY in the 3:45 PM close
        window, so we exit on a confirmed daily close, not an intra-bar touch.

    When an exit fires, a real market SELL is submitted to Alpaca and the
    actual filled_avg_price is logged (not the theoretical trigger price).
    """
    from core.strategies.swing_rsi2_mean_reversion import monitor_exits
    from core.alpaca_client import AlpacaClient

    client = AlpacaClient()
    positions = _load_positions()
    if not positions:
        logger.info("No open positions to monitor.")
        return {"exits": 0, "open": 0}

    # First morning run: prune any staged orders that failed to fill at open.
    pruned = []
    if not dry_run:
        pruned = _prune_unfilled_orders(client, positions)

    # Close window: the last intraday check before the 4:05 PM EOD scan —
    # evaluates SMA5/time exits on the confirmed daily close. The window width
    # is configurable via SWING_CLOSE_WINDOW_MINUTES (default 15), so the
    # 3:45 PM pass (minute >= 45) sits inside it. The 4:00 PM minute is also
    # included so a run landing exactly on the hour still evaluates.
    #
    # NOTE: Alpaca rejects Market-On-Close (TimeInForce.CLS) orders submitted
    # after 3:50 PM ET (Nasdaq MOC deadline). We therefore execute exits with an
    # immediate TimeInForce.DAY market order, which fills immediately and lets
    # us retrieve the filled_avg_price within the container lifecycle.
    close_window_mins = int(os.environ.get("SWING_CLOSE_WINDOW_MINUTES", "15"))
    now = _now_et()
    close_window = (now.hour == 15 and now.minute >= (60 - close_window_mins)) or \
                   (now.hour == 16 and now.minute == 0)

    # Ensure catastrophic stops are computed before evaluating exits.
    if not dry_run:
        for pos in positions.values():
            cs = _ensure_cat_stop(client, pos)
            if cs is not None:
                pos["cat_stop"] = cs
        _save_positions(positions)

    exits = monitor_exits(client, positions, dry_run=dry_run, close_window=close_window)
    sold = []
    if not dry_run:
        for e in exits:
            sym = e["symbol"]
            pos = positions.get(sym, {})
            qty = _get_position_qty(client, sym, pos.get("qty"))
            if qty <= 0:
                logger.warning(f"[SELL] No qty for {sym}; removing position without sell.")
                positions.pop(sym, None)
                continue
            res = place_market_sell_order(client, sym, qty)
            if res.get("status", "").lower() in ("submitted", "accepted", "held",
                                                  "filled", "partially_filled"):
                sold.append({"symbol": sym, "reason": e["reason"], "qty": qty,
                             "order_id": res.get("order_id")})
                positions.pop(sym, None)
                # Log the ACTUAL fill price vs the theoretical trigger price.
                fill_px = res.get("filled_avg_price") or e["exit_px"]
                _log_fill(sym, fill_px, e["exit_px"], side="sell")
            else:
                logger.warning(f"[SELL] Order for {sym} not accepted; keeping position.")
        _save_positions(positions)
    return {"exits": len(exits), "sold": sold, "open": len(positions), "pruned": pruned}


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing RSI-2 scheduler entrypoint")
    parser.add_argument("--scan", action="store_true", help="EOD 4:05 PM signal scan")
    parser.add_argument("--monitor", action="store_true", help="Intraday exit monitor")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-select mode by time-of-day (4:05 PM ET = scan, else monitor)")
    parser.add_argument("--dry-run", action="store_true", help="Compute, no orders/writes")
    parser.add_argument("--force", action="store_true", help="Bypass market-hours gate")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-swing-trader")

    # Auto mode: pick scan vs monitor by time-of-day. The EOD scan fires at
    # 4:05 PM ET (after the 4:00 PM close); everything else is a monitor run.
    if args.auto:
        now = _now_et()
        args.scan = (now.hour == 16 and now.minute >= 0 and now.minute <= 10)
        args.monitor = not args.scan

    # The market-hours gate applies ONLY to intraday monitoring. The EOD scan
    # runs AFTER the 4:00 PM close (4:05 PM ET), so it must bypass the gate —
    # otherwise the post-close scan is always skipped as "Post-market".
    if not args.force and not args.scan:
        is_open, reason = _is_equity_market_hours()
        if not is_open:
            logger.info(f"Skipping swing trader: {reason}")
            return

    # Cloud Run containers are ephemeral — pull the persisted swing state from
    # GCS at startup so this run starts from the last-known positions/fills.
    _sync_down_from_gcs()

    try:
        if args.scan:
            r = run_eod_scan(dry_run=args.dry_run)
            logger.info(f"EOD scan result: {r}")
        elif args.monitor:
            r = run_monitor(dry_run=args.dry_run)
            logger.info(f"Monitor result: {r}")
        else:
            parser.print_help()
    except Exception as e:
        logger.critical(f"Swing trader failed: {e}")
        log_exception_to_jira(e, "Swing Trader Failure")
        raise
    finally:
        # Persist the swing state back to GCS so the next ephemeral run can
        # pick up where this one left off.
        _sync_up_to_gcs()


if __name__ == "__main__":
    main()