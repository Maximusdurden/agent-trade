#!/usr/bin/env python3
"""SPY/QQQ 0DTE relative-strength scorer (Component 3).

Implements the "latch-on" strategy's ticker-selection layer (§6.7):

  - Score SPY and QQQ relative strength against each other between
    09:30 and 09:45 AM ET:
        Relative Strength = (Current Spot - Open Price) / Open Price
  - Trade ONLY the single ticker with greater relative momentum in the
    direction of the signal. Max 1 open trade.

This module is importable (for use by the full strategy engine) and also
runnable standalone for a smoke test / manual check.

Usage:
    python -m sideload.options_relative_strength --date 2026-09-25
    python -m sideload.options_relative_strength --date 2026-09-25 --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("OptionsRelativeStrength")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe (Phase 1: SPY/QQQ/IWM index funds).
TICKERS = ["SPY", "QQQ", "IWM"]

# Relative-strength scoring window (ET).
RS_START = dtime(9, 30, 0)
RS_END = dtime(9, 45, 0)

# Intraday bar interval for sampling spot.
INTRADAY_INTERVAL = "1min"


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _load_intraday(client: AlpacaClient, symbol: str, days_back: int = 5) -> pd.DataFrame:
    """Fetch intraday bars for a symbol, return an ET-indexed frame."""
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


def compute_relative_strength(client: AlpacaClient, symbol: str, session_date: str) -> dict:
    """Compute the relative-strength score for a symbol in the 09:30-09:45 window.

    Relative Strength = (Current Spot - Open Price) / Open Price

    Args:
        client: AlpacaClient.
        symbol: Ticker (SPY/QQQ).
        session_date: YYYY-MM-DD (ET).

    Returns:
        Dict with open_price, spot (last close in window), rs_score, and
        whether the window had data.
    """
    day = datetime.strptime(session_date, "%Y-%m-%d").date()
    intraday = _load_intraday(client, symbol)
    if intraday.empty:
        logger.warning(f"No intraday bars for {symbol}.")
        return {"symbol": symbol, "date": session_date, "open_price": None,
                "spot": None, "rs_score": None, "has_data": False}

    day_start = pd.Timestamp(day, tz=ET)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        logger.warning(f"No intraday bars on {session_date} for {symbol}.")
        return {"symbol": symbol, "date": session_date, "open_price": None,
                "spot": None, "rs_score": None, "has_data": False}

    # Open price = first bar's open at/after 09:30.
    open_bars = day_bars[day_bars.index >= day_start.replace(hour=9, minute=30)]
    if open_bars.empty:
        return {"symbol": symbol, "date": session_date, "open_price": None,
                "spot": None, "rs_score": None, "has_data": False}
    open_price = float(open_bars.iloc[0]["open"])

    # Spot = last close in the 09:30-09:45 window.
    rs_start = day_start.replace(hour=RS_START.hour, minute=RS_START.minute)
    rs_end = day_start.replace(hour=RS_END.hour, minute=RS_END.minute)
    window = day_bars[(day_bars.index >= rs_start) & (day_bars.index <= rs_end)]
    if window.empty:
        return {"symbol": symbol, "date": session_date, "open_price": open_price,
                "spot": None, "rs_score": None, "has_data": False}
    spot = float(window["close"].iloc[-1])

    rs_score = (spot - open_price) / open_price if open_price else 0.0

    return {
        "symbol": symbol,
        "date": session_date,
        "open_price": round(open_price, 2),
        "spot": round(spot, 2),
        "rs_score": round(rs_score, 6),
        "has_data": True,
    }


def pick_stronger_ticker(client: AlpacaClient, session_date: str,
                         direction: str | None = None) -> dict:
    """Score SPY/QQQ/IWM relative strength and pick the strongest ticker.

    Args:
        client: AlpacaClient.
        session_date: YYYY-MM-DD (ET).
        direction: Optional 'BULLISH' or 'BEARISH' signal direction. If
            provided, the strongest ticker is the one with greater momentum
            *in that direction* (i.e., for BULLISH, the higher rs_score; for
            BEARISH, the lower rs_score). If None, returns the raw scores.

    Returns:
        Dict with per-ticker scores and the selected ticker.
    """
    scores = {}
    for sym in TICKERS:
        scores[sym] = compute_relative_strength(client, sym, session_date)

    # Determine the selected ticker.
    selected = None
    if all(s["has_data"] for s in scores.values()):
        if direction == "BEARISH":
            # For bearish, the stronger move is the more negative rs_score.
            selected = min(TICKERS, key=lambda s: scores[s]["rs_score"])
        else:
            # Default / BULLISH: higher rs_score wins.
            selected = max(TICKERS, key=lambda s: scores[s]["rs_score"])

    return {
        "date": session_date,
        "direction": direction,
        "scores": scores,
        "selected_ticker": selected,
    }


def run(session_date: str, direction: str | None = None,
        send_discord: bool = True) -> dict:
    """Run the relative-strength scorer for SPY/QQQ on a session date."""
    client = AlpacaClient()
    results = pick_stronger_ticker(client, session_date, direction=direction)

    for sym, s in results["scores"].items():
        logger.info(
            f"[{sym}] open={s['open_price']} spot={s['spot']} "
            f"rs={s['rs_score']} has_data={s['has_data']}"
        )
    logger.info(f"Selected ticker: {results['selected_ticker']}")

    # Persist JSON.
    out_path = os.path.join(OUT_DIR, f"options_relative_strength_{session_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Wrote results to {out_path}")

    if send_discord:
        try:
            lines = [f"**Options Relative Strength — {session_date}**"]
            for sym, s in results["scores"].items():
                lines.append(
                    f"`{sym}` open={s['open_price']} spot={s['spot']} "
                    f"rs={s['rs_score']}"
                )
            lines.append(f"Selected: **{results['selected_ticker']}**")
            send_discord_message("\n".join(lines))
        except Exception as e:
            logger.warning(f"Discord notification failed (non-fatal): {e}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ 0DTE relative strength")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--direction", choices=["BULLISH", "BEARISH"], default=None,
                        help="Signal direction to pick the stronger ticker.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        results = run(args.date, direction=args.direction,
                      send_discord=not args.no_discord)
        print(json.dumps(results, indent=2, default=str))
    except Exception as e:
        log_exception_to_jira(e, "options_relative_strength", {"date": args.date})
        logger.exception("options_relative_strength failed")
        raise


if __name__ == "__main__":
    main()