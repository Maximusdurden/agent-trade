#!/usr/bin/env python3
"""Flat Base Breakout production runner (Phase 2).

Scans the 29-symbol momentum universe for Stage 2 flat base breakouts, stages
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
"""

from __future__ import annotations

import argparse
import logging
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


def _now_et() -> datetime:
    return datetime.now(ET)


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
    """Submit a market order (MOC proxy) with TIF=day."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=side_enum, time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        logger.info(f"[MOC] Submitted {side} {qty} {symbol} (market, DAY)")
        return {"symbol": symbol, "qty": qty, "side": side, "status": "submitted",
                "order_id": getattr(order, "id", None)}
    except Exception as e:
        logger.error(f"[MOC] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": side, "status": "failed",
                "error": str(e)}


def _sell(client: AlpacaClient, symbol: str, qty: float) -> dict:
    """Submit a market SELL order (TIF=day)."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.trading_client.submit_order(order_data=req)
        logger.info(f"[SELL] Submitted {qty} {symbol} (market, DAY)")
        return {"symbol": symbol, "qty": qty, "side": "sell", "status": "submitted",
                "order_id": getattr(order, "id", None)}
    except Exception as e:
        logger.error(f"[SELL] Failed to submit {symbol}: {e}")
        return {"symbol": symbol, "qty": qty, "side": "sell", "status": "failed",
                "error": str(e)}


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
        qty = fb_cfg.RISK_PER_TRADE_USD / setup["r"]
        if dry_run:
            logger.info(f"[Dry-run] Would buy {qty:.2f} {symbol} @ MOC "
                        f"(stop {setup['stop']:.2f}, 3R {setup['target_3r']:.2f})")
            continue
        result = _place_moc_order(client, symbol, qty)
        if result["status"] == "submitted":
            fb_state.open_position(
                symbol, setup["entry_ref"], qty, setup["stop"],
                setup["target_3r"], setup["target_5r"],
            )
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
            qty = 0.33 * initial
            if dry_run:
                logger.info(f"[Dry-run] Scale 1: sell {qty:.2f} {symbol} @ +3R")
            else:
                _sell(client, symbol, qty)
                fb_state.update_position(symbol, scale_1_filled=True,
                                         remaining_shares=remaining - qty)
            logger.info(f"[Monitor] {symbol}: Scale 1 filled @ +3R")

        # Scale 2: sell 33% at +5R.
        if pos["scale_1_filled"] and not pos["scale_2_filled"] and high >= pos["target_5r"]:
            qty = 0.33 * initial
            if dry_run:
                logger.info(f"[Dry-run] Scale 2: sell {qty:.2f} {symbol} @ +5R")
            else:
                _sell(client, symbol, qty)
                fb_state.update_position(symbol, scale_2_filled=True,
                                         remaining_shares=remaining - qty)
            logger.info(f"[Monitor] {symbol}: Scale 2 filled @ +5R")

        # Runner exit: trail remaining 34% with Close < SMA20.
        if pos["scale_1_filled"] and close < sma20:
            qty = pos["remaining_shares"]
            if dry_run:
                logger.info(f"[Dry-run] Trail exit: sell {qty:.2f} {symbol} (Close<SMA20)")
            else:
                _sell(client, symbol, qty)
                pnl = (close - entry) * qty
                fb_state.close_position(symbol, pnl)
            logger.info(f"[Monitor] {symbol}: trail exit (Close<SMA20)")

        # Time stop: liquidate if held >= 45 trading days.
        if pos["bars_held"] >= fb_cfg.MAX_HOLD_DAYS:
            qty = pos["remaining_shares"]
            if dry_run:
                logger.info(f"[Dry-run] Time stop: sell {qty:.2f} {symbol} (held {pos['bars_held']}d)")
            else:
                _sell(client, symbol, qty)
                pnl = (close - entry) * qty
                fb_state.close_position(symbol, pnl)
            logger.info(f"[Monitor] {symbol}: time stop (held {pos['bars_held']}d)")

    # Increment bars_held for all open positions.
    fb_state.increment_bars_held()


def _isna(v) -> bool:
    import pandas as pd
    return pd.isna(v)


def main() -> None:
    parser = argparse.ArgumentParser(description="Flat base breakout production runner")
    parser.add_argument("--scan", action="store_true", help="Scan for breakouts, stage MOC orders")
    parser.add_argument("--monitor", action="store_true", help="Manage open positions (exits/scaling)")
    parser.add_argument("--dry-run", action="store_true", help="Compute, no orders/writes")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-flatbase-runner")
    client = AlpacaClient()

    try:
        # Pull state from GCS at startup (ephemeral container).
        fb_state.sync_down_from_gcs()

        if args.scan:
            run_scan(client, dry_run=args.dry_run)
        elif args.monitor:
            run_monitor(client, dry_run=args.dry_run)
        else:
            parser.print_help()

        # Push state to GCS at shutdown.
        fb_state.sync_up_to_gcs()

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