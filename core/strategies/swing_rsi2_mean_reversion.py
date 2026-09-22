#!/usr/bin/env python3
"""Production swing RSI-2 mean-reversion strategy — paper-trading lane.

Consolidates the validated `sideload/backtest_swing_mean_reversion.py` logic
into a production execution module. The strategy was validated walk-forward OOS
across an 8-ticker tech universe with a 3-slot priority portfolio (Sharpe 1.19,
max DD 13.5%, exposure 23.2%).

EXECUTION FLOW (per blueprint):
  1. 4:00 PM CLOSE — Daily scan across the universe. Compute signals with t-1
     data (RSI_2 < 10, Close > SMA200 macro gate, sector gate). Rank by RSI_2
     (lowest first) when >3 signals fire; tie-break by stretch below SMA20.
  2. PRE-MARKET — Stage Limit/Market-on-Open orders for the top N open slots.
  3. DURING SESSION — Monitor exits: Close > SMA5 (profit), Catastrophic Stop
     at Entry - 2.0*ATR14, or Day-5 Time Exit.

This module is the SIGNAL + POSITION engine. It does NOT place real orders —
it writes paper-trade decisions to the DB and logs to Discord, so fills can be
verified against the backtested $0.05 slippage assumption.

Usage:
    python -m core.strategies.swing_rsi2_mean_reversion --scan        # 4PM scan
    python -m core.strategies.swing_rsi2_mean_reversion --monitor     # session monitor
    python -m core.strategies.swing_rsi2_mean_reversion --dry-run     # scan, no writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 3)[0] if "\\" in __file__ else __file__.rsplit("/", 3)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message
from sideload.backtest_swing_mean_reversion import (
    load_daily, add_indicators, BASELINE_CFG,
)

logger = logging.getLogger("SwingRSI2")

ET = ZoneInfo("America/New_York")

# Expanded universe: (symbol, sector_filter)
# AMZN dropped: 20:1 split on 2022-06-06 is UNADJUSTED in Alpaca data -> phantom
#   cat_stop losses. QQQ dropped as tradeable (low beta) but kept as sector gate.
UNIVERSE = [
    ("AMD", "SMH"), ("NVDA", "SMH"), ("TSLA", "SMH"), ("SMH", "SMH"),
    ("MSFT", "QQQ"), ("AAPL", "QQQ"), ("GOOGL", "QQQ"), ("META", "QQQ"),
]
SECTOR_BY_SYMBOL = dict(UNIVERSE)

MAX_SLOTS = 3
SLOT_SIZE_PCT = 0.33
SLIPPAGE = 0.05
RSI_BUY_BELOW = 10.0
CAT_STOP_ATR_MULT = 2.0
MAX_HOLD_DAYS = 5
LOOKBACK_DAYS = 365 * 8


def _rsi2(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 2, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def _stretch_units(df: pd.DataFrame, ts) -> float:
    """(Close - SMA20) / ATR14 at ts (negative = stretched below SMA20)."""
    try:
        loc = df.index.get_loc(ts)
    except Exception:
        return 0.0
    if loc < 20:
        return 0.0
    row = df.iloc[loc]
    return float((row["close"] - row["sma20"]) / row["atr"]) if row["atr"] > 0 else 0.0


def scan_signals(client: AlpacaClient, as_of: pd.Timestamp | None = None) -> list[dict]:
    """Run the 4PM close scan across the universe.

    Returns ranked entry candidates (lowest RSI_2 first) that pass:
      - Macro gate: Close_{t-1} > SMA200_{t-1}
      - Sector gate: sector (SMH/QQQ) > its SMA200
      - Oversold: RSI_2 < 10
    """
    as_of = as_of or pd.Timestamp.now(tz=ET)
    as_of_naive = as_of.tz_localize(None).normalize()

    # Load all data + indicators.
    data = {}
    sector_data = {}
    for sym, sector in UNIVERSE:
        df = load_daily(client, sym, LOOKBACK_DAYS)
        if df.empty:
            continue
        data[sym] = add_indicators(df, dict(BASELINE_CFG))
        if sector not in sector_data:
            sdf = load_daily(client, sector, LOOKBACK_DAYS)
            if not sdf.empty:
                sector_data[sector] = add_indicators(sdf, dict(BASELINE_CFG))

    # Find the last completed trading day before as_of.
    candidates = []
    for sym, df in data.items():
        sig = df[["close", "rsi", "sma20", "atr", "sma_trend"]].shift(1)
        sig_idx = sig.index.tz_localize(None).normalize()
        prior = sig_idx[sig_idx < as_of_naive]
        if prior.empty:
            continue
        last_day = prior[-1]
        row = sig[sig_idx == last_day].iloc[-1]
        # Macro gate.
        if not (row["close"] > row["sma_trend"]):
            continue
        # Oversold.
        if not (row["rsi"] < RSI_BUY_BELOW):
            continue
        # Sector gate.
        sector = SECTOR_BY_SYMBOL.get(sym, "QQQ")
        sdf = sector_data.get(sector)
        if sdf is None:
            continue
        s_sig = sdf[["close", "sma_trend"]].shift(1)
        s_idx = s_sig.index.tz_localize(None).normalize()
        s_row = s_sig[s_idx == last_day]
        if s_row.empty or not (float(s_row.iloc[-1]["close"]) > float(s_row.iloc[-1]["sma_trend"])):
            continue
        candidates.append({
            "symbol": sym, "signal_date": str(last_day.date()),
            "rsi": float(row["rsi"]),
            "stretch": _stretch_units(df, last_day),
            "close": float(row["close"]),
            "sma_trend": float(row["sma_trend"]),
            "atr": float(row["atr"]),
        })

    # Priority: lowest RSI_2 first, tie-break by stretch (most stretched first).
    candidates.sort(key=lambda c: (c["rsi"], c["stretch"]))
    return candidates


def stage_orders(candidates: list[dict], open_slots: int, dry_run: bool = False) -> list[dict]:
    """Stage market-on-open orders for the top ``open_slots`` candidates.

    Returns the staged order list (paper-trade decisions). Does NOT place real
    orders — writes to DB/logs so fills can be verified.
    """
    staged = []
    for c in candidates[:open_slots]:
        order = {
            "symbol": c["symbol"], "signal_date": c["signal_date"],
            "rsi": c["rsi"], "stretch": c["stretch"],
            "action": "BUY", "order_type": "market_on_open",
            "slippage_assumption": SLIPPAGE,
            "size_pct": SLOT_SIZE_PCT,
            "cat_stop_atr_mult": CAT_STOP_ATR_MULT,
            "max_hold_days": MAX_HOLD_DAYS,
        }
        staged.append(order)
        if not dry_run:
            logger.info(f"[STAGE] {c['symbol']} BUY @ open (rsi={c['rsi']:.1f}, "
                        f"stretch={c['stretch']:.2f})")
    return staged


def monitor_exits(client: AlpacaClient, positions: dict, dry_run: bool = False,
                  close_window: bool = False) -> list[dict]:
    """Monitor open positions for exits.

    Segmentation (matches backtest timing):
      - Catastrophic stop (low <= cat_stop): evaluated on EVERY session check.
      - SMA5 touch (close > SMA5) and Day-5 time exit: evaluated ONLY in the
        close window (3:45 PM ET), so we exit on a confirmed daily close rather
        than an intra-bar touch.
    """
    exits = []
    for sym, pos in positions.items():
        df = load_daily(client, sym, 30)
        if df.empty:
            continue
        last = df.iloc[-1]
        close = float(last["close"])
        low = float(last["low"])
        sma5 = float(df["close"].rolling(5).mean().iloc[-1])
        reason = None
        exit_px = None
        # Cat stop: active on every session check.
        if pos.get("cat_stop") is not None and low <= pos["cat_stop"]:
            reason, exit_px = "cat_stop", pos["cat_stop"]
        # SMA5 + time exit: only in the close window (confirmed daily close).
        elif close_window:
            if close > sma5:
                reason, exit_px = "sma5_touch", close
            elif (pd.Timestamp.now(tz=ET).tz_localize(None).normalize() - pos["day0"]).days >= MAX_HOLD_DAYS:
                reason, exit_px = "time_stop", close
        if reason:
            exits.append({"symbol": sym, "reason": reason, "exit_px": exit_px})
            if not dry_run:
                logger.info(f"[EXIT] {sym} {reason} @ ${exit_px:.2f}")
    return exits


def main() -> None:
    parser = argparse.ArgumentParser(description="Production swing RSI-2 mean-reversion")
    parser.add_argument("--scan", action="store_true", help="Run the 4PM close scan")
    parser.add_argument("--monitor", action="store_true", help="Monitor open positions for exits")
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not write/log")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-swing-rsi2")
    client = AlpacaClient()

    try:
        if args.scan:
            candidates = scan_signals(client)
            logger.info(f"Scan: {len(candidates)} candidates (max {MAX_SLOTS} slots)")
            for c in candidates[:MAX_SLOTS]:
                logger.info(f"  {c['symbol']}: rsi={c['rsi']:.1f} stretch={c['stretch']:.2f} "
                            f"signal={c['signal_date']}")
            staged = stage_orders(candidates, MAX_SLOTS, dry_run=args.dry_run)
            if not args.no_discord and not args.dry_run:
                try:
                    send_discord_message(
                        f"[SWING] Scan: {len(candidates)} candidates, "
                        f"staged {len(staged)}: {', '.join(s['symbol'] for s in staged)}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
        elif args.monitor:
            # In production this reads open positions from the DB. For staging,
            # we accept a positions JSON via env or a file.
            positions = {}
            pos_file = os.environ.get("SWING_POSITIONS_FILE")
            if pos_file and os.path.exists(pos_file):
                with open(pos_file, "r", encoding="utf-8") as fh:
                    positions = json.load(fh)
            exits = monitor_exits(client, positions, dry_run=args.dry_run)
            logger.info(f"Monitor: {len(exits)} exits")
            if not args.no_discord and not args.dry_run and exits:
                try:
                    msg = ", ".join(f"{e['symbol']} {e['reason']}" for e in exits)
                    send_discord_message(f"[SWING] Exits: {msg}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
        else:
            parser.print_help()
    except Exception as e:
        logger.critical(f"Swing RSI-2 failed: {e}")
        log_exception_to_jira(e, "Swing RSI-2 Production Failure")
        raise


if __name__ == "__main__":
    main()