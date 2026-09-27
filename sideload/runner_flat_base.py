#!/usr/bin/env python3
"""Flat Base Breakout production runner (Phase 2/3).

Scans the 27-symbol momentum universe for Stage 2 flat base breakouts, stages
Market-on-Close (MOC) orders at 3:45 PM ET, and manages open positions (breakeven
stop, 33%@3R / 33%@5R scaling, SMA20 trail, 45d time stop).

Reuses the leakage-free detection logic from `backtest_flat_base.py` and the
state engine from `flatbase_state.py`. Writes to the SAME database so the blog
+ dashboard pick the lane up.

Usage:
    python -m sideload.runner_flat_base --scan --dry-run   # detect breakouts, no orders
    python -m sideload.runner_flat_base --scan             # stage MOC orders
    python -m sideload.runner_flat_base --monitor --dry-run  # manage exits, no orders
    python -m sideload.runner_flat_base --monitor
    python -m sideload.runner_flat_base --auto             # auto-select by time (3:45 PM = scan)
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Apply flat-base overrides BEFORE constructing the Alpaca client.
from sideload import config_sideload_flatbase as fb_cfg
from sideload import flatbase_state as fb_state
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
fb_cfg.apply_sideload_overrides()

from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message
from sideload.backtest_flat_base import (
    load_daily, add_indicators, _prior_uptrend_met, _base_detected,
    _base_high, _macro_ok,
)

logger = logging.getLogger("FlatBaseRunner")

ET = ZoneInfo("America/New_York")

# Slippage/fill log (validates the $0.05 backtest assumption).
FILL_LOG = os.path.join(PROJECT_ROOT, "sideload", "data", "flatbase_fills.csv")
GCS_FILLS_BLOB = "flatbase_fills.csv"


def _now_et() -> datetime:
    return datetime.now(ET)


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


def _log_fill(cluster_id: str, symbol: str, side: str, shares: float,
              fill_price: float, ref_price: float) -> None:
    """Record a fill for slippage validation.

    fill_price = actual broker fill (from the Alpaca order).
    ref_price  = the theoretical reference price:
                 - buy:  the t-1 breakout close (entry_ref, MOC proxy)
                 - sell: the exit trigger price (target_3r / target_5r / sma20)
    slippage_bps = (fill_price - ref_price) / ref_price * 10000
    """
    os.makedirs(os.path.dirname(FILL_LOG), exist_ok=True)
    import csv
    new = not os.path.exists(FILL_LOG)
    with open(FILL_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "cluster_id", "symbol", "side", "shares",
                        "fill_price", "ref_price", "slippage_bps"])
        slip_bps = (fill_price - ref_price) / ref_price * 10000 if ref_price else 0.0
        w.writerow([
            _now_et().isoformat(), cluster_id, symbol, side,
            round(shares, 4), round(fill_price, 4), round(ref_price, 4),
            round(slip_bps, 2),
        ])


def _sync_fills_from_gcs() -> None:
    """Download flatbase_fills.csv from GCS at container startup."""
    bucket = fb_state._gcs_bucket()
    if bucket is None:
        return
    os.makedirs(os.path.dirname(FILL_LOG), exist_ok=True)
    try:
        blob = bucket.blob(GCS_FILLS_BLOB)
        if blob.exists():
            blob.download_to_filename(FILL_LOG)
            logger.info(f"[GCS] Downloaded {GCS_FILLS_BLOB} -> {FILL_LOG}")
    except Exception as e:
        logger.warning(f"[GCS] Could not download {GCS_FILLS_BLOB}: {e}")


def _sync_fills_to_gcs() -> None:
    """Upload flatbase_fills.csv to GCS at container shutdown."""
    bucket = fb_state._gcs_bucket()
    if bucket is None:
        return
    if not os.path.exists(FILL_LOG):
        return
    try:
        bucket.blob(GCS_FILLS_BLOB).upload_from_filename(FILL_LOG)
        logger.info(f"[GCS] Uploaded {FILL_LOG} -> {GCS_FILLS_BLOB}")
    except Exception as e:
        logger.warning(f"[GCS] Could not upload {GCS_FILLS_BLOB}: {e}")


def _build_cfg() -> dict:
    """Build the runner config from the locked flat-base parameters."""
    return {
        "uptrend_gain_pct": fb_cfg.UPTREND_GAIN_PCT,
        "uptrend_lookback": fb_cfg.UPTREND_LOOKBACK,
        "base_min_bars": fb_cfg.BASE_MIN_BARS,
        "base_max_bars": fb_cfg.BASE_MAX_BARS,
        "base_tightness": fb_cfg.BASE_TIGHTNESS,
        "base_sma": fb_cfg.BASE_SMA,
        "breakout_rvol": fb_cfg.BREAKOUT_RVOL,
        "macro_sma": fb_cfg.MACRO_SMA,
        "atr_stop_mult": fb_cfg.ATR_STOP_MULT,
        "trail_sma": fb_cfg.TRAIL_SMA,
        "slippage": 0.05,
    }


def _load_macro_sig(client: AlpacaClient, cfg: dict):
    """Load the QQQ macro filter signal frame (shifted, no lookahead)."""
    macro = load_daily(client, fb_cfg.MACRO_SYMBOL, fb_cfg.LOOKBACK_BARS)
    if macro.empty:
        return None
    macro = add_indicators(macro, cfg)
    return macro[["close", "sma20"]].shift(1)


def _detect_breakout(client: AlpacaClient, symbol: str, cfg: dict, macro_sig) -> dict | None:
    """Detect a flat base breakout for one symbol. Returns setup dict or None."""
    df = load_daily(client, symbol, fb_cfg.LOOKBACK_BARS)
    if df.empty or len(df) < 60:
        return None
    df = add_indicators(df, cfg)
    sig = df[["close", "high", "low", "open", "volume", "sma20", "sma_vol20",
              "sma50", "atr14"]].shift(1)
    i = len(sig) - 1  # last completed bar (t-1)
    if i < 1:
        return None
    # Prior uptrend gate.
    if not _prior_uptrend_met(sig, i, cfg):
        return None
    # Base consolidation ending at t-1.
    base_len = _base_detected(sig, i, cfg)
    if not base_len:
        return None
    # Breakout: Close[t-1] > Base_High.
    base_high = _base_high(sig, i, base_len)
    if not (sig["close"].iloc[i] > base_high):
        return None
    # Volume confirmation.
    vol20 = sig["sma_vol20"].iloc[i]
    if (vol20 is None or vol20 <= 0
            or not (sig["volume"].iloc[i] >= fb_cfg.BREAKOUT_RVOL * vol20)):
        return None
    # Macro filter.
    if not _macro_ok(macro_sig, i, cfg):
        return None
    # Compute stop + targets.
    atr = sig["atr14"].iloc[i]
    if atr is None or atr <= 0:
        return None
    stop = base_high - fb_cfg.ATR_STOP_MULT * atr
    entry_ref = sig["close"].iloc[i]  # MOC fill proxy = t-1 close
    if stop >= entry_ref:
        return None
    r = entry_ref - stop
    return {
        "symbol": symbol,
        "base_high": base_high,
        "entry_ref": entry_ref,
        "stop": stop,
        "r": r,
        "target_3r": entry_ref + fb_cfg.TP1_R * r,
        "target_5r": entry_ref + fb_cfg.TP2_R * r,
        "breakeven_price": entry_ref + fb_cfg.BREAKEVEN_R * r,
    }


def _place_moc_order(client: AlpacaClient, symbol: str, qty: float, side: str = "buy") -> dict:
    """Submit a market order (MOC proxy) with TIF=day and poll for the fill.

    Returns the order dict including the actual ``filled_avg_price`` so the
    entry fill is logged empirically (not the theoretical entry_ref).
    """
    import time
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=side_enum, time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        order_id = str(getattr(order, "id", ""))
        logger.info(f"[MOC] Submitted {side} {qty} {symbol} (market, DAY) order_id={order_id}")
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
                logger.warning(f"[MOC] Poll error for {symbol}: {poll_err}")
            time.sleep(0.5)
        return {"symbol": symbol, "qty": qty, "side": side, "status": status_str,
                "order_id": order_id, "filled_avg_price": filled_price}
    except Exception as e:
        logger.error(f"[MOC] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": side, "status": "failed",
                "order_id": None, "filled_avg_price": None, "error": str(e)}


def _sell(client: AlpacaClient, symbol: str, qty: float) -> dict:
    """Submit a market SELL order (TIF=day) and poll for the fill."""
    import time
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        order_id = str(getattr(order, "id", ""))
        logger.info(f"[SELL] Submitted {qty} {symbol} (market, DAY) order_id={order_id}")
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
        return {"symbol": symbol, "qty": qty, "side": "sell", "status": status_str,
                "order_id": order_id, "filled_avg_price": filled_price}
    except Exception as e:
        logger.error(f"[SELL] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": "sell", "status": "failed",
                "order_id": None, "filled_avg_price": None, "error": str(e)}


def run_scan(client: AlpacaClient, dry_run: bool = False) -> None:
    """Scan the universe for flat base breakouts and stage MOC orders."""
    cfg = _build_cfg()
    macro_sig = _load_macro_sig(client, cfg)
    open_positions = fb_state.get_open_positions()
    open_count = len(open_positions)
    slots = fb_cfg.MAX_CONCURRENT_POSITIONS - open_count
    logger.info(f"[Scan] Open positions: {open_count}, slots available: {slots}")

    if slots <= 0:
        logger.info("[Scan] At max concurrent positions; skipping new entries.")
        return

    candidates = []
    for symbol in fb_cfg.FLATBASE_UNIVERSE:
        if symbol in open_positions:
            continue
        setup = _detect_breakout(client, symbol, cfg, macro_sig)
        if setup:
            candidates.append(setup)
            logger.info(f"[Scan] Breakout candidate: {symbol} "
                        f"entry_ref={setup['entry_ref']:.2f} stop={setup['stop']:.2f} "
                        f"R={setup['r']:.2f}")

    # Stage up to `slots` candidates (highest R first).
    candidates.sort(key=lambda s: -s["r"])
    staged = candidates[:slots]
    for setup in staged:
        symbol = setup["symbol"]
        # Floor to whole shares (Alpaca rejects fractional equity qty).
        qty = math.floor(fb_cfg.RISK_PER_TRADE_USD / setup["r"])
        if qty < 1:
            logger.warning(f"[Scan] {symbol} risk sizing yields <1 share; skipping.")
            continue
        if dry_run:
            logger.info(f"[Dry-run] Would buy {qty} {symbol} @ MOC "
                        f"(stop {setup['stop']:.2f}, 3R {setup['target_3r']:.2f})")
            continue
        result = _place_moc_order(client, symbol, qty)
        # Only open a position if the order FULLY filled (status filled with a
        # fill price). A partial fill would track the full qty while the broker
        # holds fewer shares, causing later over-sells.
        status = str(result.get("status", "")).lower()
        fill_px = result.get("filled_avg_price")
        if status == "filled" and fill_px is not None:
            pos = fb_state.open_position(
                symbol, setup["entry_ref"], qty, setup["stop"],
                setup["target_3r"], setup["target_5r"],
            )
            # Log the entry fill (empirical fill price vs entry_ref proxy).
            _log_fill(pos["cluster_id"], symbol, "buy", qty, fill_px, setup["entry_ref"])
        else:
            logger.warning(f"[Scan] {symbol} MOC order not fully filled (status={status}); "
                           f"not opening position.")
    logger.info(f"[Scan] Staged {len(staged)} candidates.")


def run_monitor(client: AlpacaClient, dry_run: bool = False) -> None:
    """Manage open positions: breakeven, scaling, trail, time stop."""
    cfg = _build_cfg()
    open_positions = fb_state.get_open_positions()
    if not open_positions:
        logger.info("[Monitor] No open positions.")
        return

    for symbol, pos in list(open_positions.items()):
        df = load_daily(client, symbol, fb_cfg.LOOKBACK_BARS)
        if df.empty:
            continue
        df = add_indicators(df, cfg)
        last = df.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        sma20 = float(last["sma20"]) if not _isna(last["sma20"]) else close

        entry = pos["entry_price"]
        r = entry - pos["stop_loss"]
        remaining = pos["remaining_shares"]
        initial = pos["initial_shares"]

        # Breakeven floor: once price reaches entry + 2R, move stop to entry.
        if not pos["breakeven_active"] and high >= entry + fb_cfg.BREAKEVEN_R * r:
            pos["breakeven_active"] = True
            pos["stop_loss"] = entry
            fb_state.update_position(symbol, breakeven_active=True, stop_loss=entry)
            logger.info(f"[Monitor] {symbol}: breakeven armed at {entry:.2f}")

        # Scale 1: sell 33% at +3R.
        if not pos["scale_1_filled"] and high >= pos["target_3r"]:
            qty = math.floor(0.33 * initial)
            if qty < 1:
                logger.warning(f"[Monitor] {symbol}: scale-1 qty <1 share; skipping.")
            elif dry_run:
                logger.info(f"[Dry-run] Scale 1: sell {qty} {symbol} @ +3R")
            else:
                res = _sell(client, symbol, qty)
                status = str(res.get("status", "")).lower()
                fill_px = res.get("filled_avg_price")
                if status == "filled" and fill_px is not None:
                    _log_fill(pos["cluster_id"], symbol, "sell", qty, fill_px, pos["target_3r"])
                    fb_state.update_position(symbol, scale_1_filled=True,
                                             remaining_shares=remaining - qty)
                    remaining -= qty
                    logger.info(f"[Monitor] {symbol}: Scale 1 filled @ +3R")
                else:
                    logger.warning(f"[Monitor] {symbol}: scale-1 sell not fully filled (status={status}); "
                                   f"state unchanged.")

        # Scale 2: sell 33% at +5R.
        if pos["scale_1_filled"] and not pos["scale_2_filled"] and high >= pos["target_5r"]:
            qty = math.floor(0.33 * initial)
            if qty < 1:
                logger.warning(f"[Monitor] {symbol}: scale-2 qty <1 share; skipping.")
            elif dry_run:
                logger.info(f"[Dry-run] Scale 2: sell {qty} {symbol} @ +5R")
            else:
                res = _sell(client, symbol, qty)
                status = str(res.get("status", "")).lower()
                fill_px = res.get("filled_avg_price")
                if status == "filled" and fill_px is not None:
                    _log_fill(pos["cluster_id"], symbol, "sell", qty, fill_px, pos["target_5r"])
                    fb_state.update_position(symbol, scale_2_filled=True,
                                             remaining_shares=remaining - qty)
                    remaining -= qty
                    logger.info(f"[Monitor] {symbol}: Scale 2 filled @ +5R")
                else:
                    logger.warning(f"[Monitor] {symbol}: scale-2 sell not fully filled (status={status}); "
                                   f"state unchanged.")

        # Runner exit: trail remaining 34% with Close < SMA20.
        # Use elif so trail and time-stop are mutually exclusive in one pass
        # (both firing would double-sell the full remaining position -> short).
        if pos["scale_1_filled"] and close < sma20:
            qty = pos["remaining_shares"]
            if dry_run:
                logger.info(f"[Dry-run] Trail exit: sell {qty:.2f} {symbol} (Close<SMA20)")
            else:
                res = _sell(client, symbol, qty)
                status = str(res.get("status", "")).lower()
                fill_px = res.get("filled_avg_price")
                if status == "filled" and fill_px is not None:
                    _log_fill(pos["cluster_id"], symbol, "sell", qty, fill_px, sma20)
                    pnl = (fill_px - entry) * qty
                    fb_state.close_position(symbol, pnl)
                    logger.info(f"[Monitor] {symbol}: trail exit (Close<SMA20)")
                else:
                    logger.warning(f"[Monitor] {symbol}: trail sell not fully filled (status={status}); "
                                   f"position kept.")

        # Time stop: liquidate if held >= 45 trading days.
        elif pos["bars_held"] >= fb_cfg.MAX_HOLD_DAYS:
            qty = pos["remaining_shares"]
            if dry_run:
                logger.info(f"[Dry-run] Time stop: sell {qty:.2f} {symbol} (held {pos['bars_held']}d)")
            else:
                res = _sell(client, symbol, qty)
                status = str(res.get("status", "")).lower()
                fill_px = res.get("filled_avg_price")
                if status == "filled" and fill_px is not None:
                    _log_fill(pos["cluster_id"], symbol, "sell", qty, fill_px, close)
                    pnl = (fill_px - entry) * qty
                    fb_state.close_position(symbol, pnl)
                    logger.info(f"[Monitor] {symbol}: time stop (held {pos['bars_held']}d)")
                else:
                    logger.warning(f"[Monitor] {symbol}: time-stop sell not fully filled (status={status}); "
                                   f"position kept.")

    # Increment bars_held for all open positions.
    fb_state.increment_bars_held()


def _isna(v) -> bool:
    import pandas as pd
    return pd.isna(v)


def main() -> None:
    parser = argparse.ArgumentParser(description="Flat base breakout production runner")
    parser.add_argument("--scan", action="store_true", help="Scan for breakouts, stage MOC orders")
    parser.add_argument("--monitor", action="store_true", help="Manage open positions (exits/scaling)")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-select mode by time-of-day (3:45 PM ET = scan, else monitor)")
    parser.add_argument("--force", action="store_true", help="Bypass market-hours gate")
    parser.add_argument("--dry-run", action="store_true", help="Compute, no orders/writes")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-flatbase-runner")
    client = AlpacaClient()

    # Auto mode: pick scan vs monitor by time-of-day. The MOC scan fires at
    # 3:45 PM ET (before the 4:00 PM close); everything else is a monitor run.
    if args.auto:
        now = _now_et()
        args.scan = (now.hour == fb_cfg.MOC_HOUR and now.minute >= fb_cfg.MOC_MINUTE - 5
                     and now.minute <= fb_cfg.MOC_MINUTE + 5)
        args.monitor = not args.scan

    # The market-hours gate applies ONLY to intraday monitoring. The MOC scan
    # runs at 3:45 PM ET (during market hours), so it must bypass the gate —
    # otherwise the scan is skipped as "Post-market" if it runs a few minutes late.
    if not args.force and not args.scan:
        is_open, reason = _is_equity_market_hours()
        if not is_open:
            logger.info(f"Skipping flat base runner: {reason}")
            return

    try:
        # Pull state + fills from GCS at startup (ephemeral container).
        fb_state.sync_down_from_gcs()
        _sync_fills_from_gcs()

        if args.scan:
            run_scan(client, dry_run=args.dry_run)
        elif args.monitor:
            run_monitor(client, dry_run=args.dry_run)
        else:
            parser.print_help()

        # Push state + fills to GCS at shutdown.
        fb_state.sync_up_to_gcs()
        _sync_fills_to_gcs()

        if not args.no_discord and not args.dry_run:
            try:
                send_discord_message(f"[FlatBase] Runner cycle complete "
                                     f"({len(fb_state.get_open_positions())} open positions)")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Flat base runner failed: {e}")
        log_exception_to_jira(e, "Flat Base Runner Failure")
        raise


if __name__ == "__main__":
    main()