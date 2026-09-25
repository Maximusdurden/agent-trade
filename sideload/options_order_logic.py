#!/usr/bin/env python3
"""SPY/QQQ/IWM 0DTE order logic orchestrator (Component 10).

Ties together all components into the full "latch-on" strategy flow (§7):

  09:15  Compute sentiment score (Alpaca News API, 07:00-09:15 headlines)
         Arm bias only if |sentiment| >= 0.40
  09:30  Compute S/R anchors: PDH/PDL/PDC, PMH/PML, Anchored VWAP
  09:30-09:45  Score SPY/QQQ/IWM relative strength; pick the strongest ticker
         Latch-on requires: break & hold above PMH (calls) / below PML (puts)
         AND holding above intraday VWAP
         Round-number rule: no entry within $0.50 below round resistance;
         a clean 1-min close above round number = breakout trigger
         Select contract: first OTM strike, delta 0.40-0.50
         Size: floor($500 / (ask x 100)) contracts
         Place order with 20% hard stop on premium
  Ride   Trailing stop (5-min low for longs) + momentum flip (RSI/MACD)
         + 15-min candle reversal
  11:30  HARD TIME EXIT — liquidate regardless of setup

This module is importable (for the full strategy engine) and runnable
standalone for a smoke test.

Usage:
    python -m sideload.options_order_logic --date 2026-09-25 --dry-run
    python -m sideload.options_order_logic --date 2026-09-25 --no-discord
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
from sideload.options_sentiment_sr import score_sentiment, compute_sr_anchors
from sideload.options_relative_strength import pick_stronger_ticker
from sideload.options_strike_sizer import select_strike
from sideload.options_execution_guards import (
    check_spread, check_can_trade, record_trade, check_macro_blackout,
)
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("OptionsOrderLogic")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe.
TICKERS = ["SPY", "QQQ", "IWM"]

# Latch-on confirmation window (09:30-09:45 ET).
LATCH_START = dtime(9, 30, 0)
LATCH_END = dtime(9, 45, 0)

# Hard time exit (§6.5).
HARD_EXIT_TIME = dtime(11, 30, 0)

# Intraday bar interval for latch-on confirmation.
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


def _confirm_latch_on(client: AlpacaClient, symbol: str, direction: str,
                      session_date: str, anchors: dict) -> dict:
    """Check the latch-on confirmation for a symbol/direction.

    Improved spec (plan §12):
      - 2-bar hold: Close[t] and Close[t-1] both beyond PMH/PML.
      - Minimum clearance: price >= PMH * 1.001 (calls) / <= PML * 0.999 (puts).
      - VWAP slope gate: dVWAP_3m > 0 (bullish) / < 0 (bearish).
    """
    intraday = _load_intraday(client, symbol)
    if intraday.empty:
        return {"confirmed": False, "reason": "No intraday data"}

    day = datetime.strptime(session_date, "%Y-%m-%d").date()
    day_start = pd.Timestamp(day, tz=ET)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return {"confirmed": False, "reason": "No bars on session date"}

    # Latch window.
    latch_start = day_start.replace(hour=LATCH_START.hour, minute=LATCH_START.minute)
    latch_end = day_start.replace(hour=LATCH_END.hour, minute=LATCH_END.minute)
    window = day_bars[(day_bars.index >= latch_start) & (day_bars.index <= latch_end)]
    if len(window) < 4:
        return {"confirmed": False, "reason": "Not enough bars in latch window"}

    pmh = anchors.get("pmh")
    pml = anchors.get("pml")

    # Anchored VWAP series from the 09:30 open bar.
    vwap_start = day_start.replace(hour=9, minute=30)
    vwap_bars = day_bars[day_bars.index >= vwap_start]
    vwap_series = None
    if not vwap_bars.empty and {"close", "volume"}.issubset(vwap_bars.columns):
        tp = (vwap_bars["high"] + vwap_bars["low"] + vwap_bars["close"]) / 3.0
        cum_pv = (tp * vwap_bars["volume"]).cumsum()
        cum_v = vwap_bars["volume"].cumsum()
        vwap_series = cum_pv / cum_v.replace(0, pd.NA)

    closes = window["close"].to_numpy()
    idx = window.index

    for i in range(1, len(window)):
        close_t = float(closes[i])
        close_prev = float(closes[i - 1])
        ts_t = idx[i]

        # VWAP slope: VWAP[t] - VWAP[t-3].
        d_vwap = None
        if vwap_series is not None:
            vwap_t = _vwap_at(vwap_series, vwap_bars.index, ts_t)
            ts_3 = ts_t - timedelta(minutes=3)
            vwap_3 = _vwap_at(vwap_series, vwap_bars.index, ts_3)
            if vwap_t is not None and vwap_3 is not None:
                d_vwap = vwap_t - vwap_3

        if direction == "BULLISH":
            if pmh is None:
                return {"confirmed": False, "reason": "Missing PMH anchor"}
            if close_t >= pmh * 1.001 and close_prev >= pmh * 1.001:
                if d_vwap is not None and d_vwap > 0:
                    return {"confirmed": True, "current": close_t, "pmh": pmh,
                            "d_vwap": d_vwap, "reason": "OK"}
        else:  # BEARISH
            if pml is None:
                return {"confirmed": False, "reason": "Missing PML anchor"}
            if close_t <= pml * 0.999 and close_prev <= pml * 0.999:
                if d_vwap is not None and d_vwap < 0:
                    return {"confirmed": True, "current": close_t, "pml": pml,
                            "d_vwap": d_vwap, "reason": "OK"}

    return {"confirmed": False, "reason": "Latch-on not confirmed (2-bar hold / VWAP slope / clearance)"}


def _vwap_at(vwap_series: pd.Series, vwap_index, ts) -> float | None:
    """Return the VWAP value at or before timestamp ``ts`` (no lookahead)."""
    mask = vwap_index <= ts
    if not mask.any():
        return None
    vals = vwap_series[mask]
    if vals.empty:
        return None
    return float(vals.iloc[-1])


def run(session_date: str, dry_run: bool = True, send_discord: bool = True) -> dict:
    """Run the full order-logic flow for a session date.

    Args:
        session_date: YYYY-MM-DD (ET).
        dry_run: If True, do not place real orders (default).
        send_discord: Send a Discord notification.

    Returns:
        Dict describing the strategy decision.
    """
    client = AlpacaClient()
    result = {"date": session_date, "dry_run": dry_run, "decision": "NO_TRADE"}

    # 1. Circuit breaker — max 1 trade/day.
    cb = check_can_trade(session_date)
    result["circuit_breaker"] = cb
    if not cb["can_trade"]:
        result["reason"] = cb["reason"]
        return result

    # 2. Macro blackout — block entries 09:58-10:03 if a release is scheduled.
    blackout = check_macro_blackout(session_date)
    result["macro_blackout"] = blackout
    if blackout["blocked"]:
        result["reason"] = blackout["reason"]
        return result

    # 3. Sentiment — arm bias only if |score| >= 0.40.
    sentiment_results = {}
    for sym in TICKERS:
        sentiment_results[sym] = score_sentiment(client, sym, session_date)
    result["sentiment"] = sentiment_results

    # Determine the armed direction from the strongest sentiment.
    armed = [s for s in sentiment_results.values() if s["armed"]]
    if not armed:
        result["reason"] = "No ticker armed (|sentiment| < 0.40)"
        return result
    # Use the most extreme sentiment to set direction.
    strongest = max(armed, key=lambda s: abs(s["score"]))
    direction = strongest["bias"]  # BULLISH or BEARISH
    result["direction"] = direction
    result["sentiment_driver"] = strongest["symbol"]

    # 4. S/R anchors.
    anchors = {}
    for sym in TICKERS:
        anchors[sym] = compute_sr_anchors(client, sym, session_date)
    result["sr_anchors"] = anchors

    # 5. Relative strength — pick the strongest ticker in the signal direction.
    rs = pick_stronger_ticker(client, session_date, direction=direction)
    result["relative_strength"] = rs
    selected_ticker = rs["selected_ticker"]
    if not selected_ticker:
        result["reason"] = "No ticker selected by relative strength"
        return result
    result["selected_ticker"] = selected_ticker

    # 5b. Sentiment confluence gate — the selected ticker must independently
    #     clear the threshold in the signal direction.
    sel_sent = sentiment_results.get(selected_ticker, {})
    sel_score = sel_sent.get("score", 0.0)
    if direction == "BULLISH":
        confluence_ok = sel_score >= 0.40
    else:  # BEARISH
        confluence_ok = sel_score <= -0.40
    result["sentiment_confluence"] = {
        "ticker": selected_ticker, "score": sel_score, "pass": confluence_ok,
    }
    if not confluence_ok:
        result["reason"] = (f"Sentiment confluence failed: {selected_ticker} "
                            f"score {sel_score} not in {direction} direction")
        return result

    # 6. Latch-on confirmation.
    latch = _confirm_latch_on(client, selected_ticker, direction, session_date,
                              anchors.get(selected_ticker, {}))
    result["latch_on"] = latch
    if not latch["confirmed"]:
        result["reason"] = f"Latch-on not confirmed for {selected_ticker}: {latch['reason']}"
        return result

    # 7. Select contract + size.
    strike = select_strike(client, selected_ticker, direction, session_date)
    result["strike"] = strike
    if not strike or strike.get("contracts", 0) < 1:
        result["reason"] = f"No tradeable contract for {selected_ticker}"
        return result

    # 8. Spread gate.
    spread = check_spread(strike["bid"], strike["ask"])
    result["spread_gate"] = spread
    if not spread["pass"]:
        result["reason"] = f"Spread gate failed: {spread['reason']}"
        return result

    # 9. Place order (unless dry run).
    result["decision"] = "BUY"
    result["order"] = {
        "occ": strike["selected_occ"],
        "qty": strike["contracts"],
        "side": "buy",
        "limit_price": strike["ask"],
        "notional": strike["notional"],
        "max_loss_20pct": strike["max_loss_20pct"],
        "hard_exit_time": str(HARD_EXIT_TIME),
    }

    if not dry_run:
        try:
            order = client.place_option_order(
                symbol=strike["selected_occ"],
                qty=strike["contracts"],
                side="buy",
                limit_price=strike["ask"],
            )
            result["order"]["result"] = order
            record_trade(session_date, stopped_out=False)
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            result["decision"] = "ORDER_FAILED"
            result["reason"] = str(e)
            return result

    # Persist JSON.
    out_path = os.path.join(OUT_DIR, f"options_order_logic_{session_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info(f"Wrote results to {out_path}")

    if send_discord:
        try:
            send_discord_message(
                f"**Options Order Logic — {session_date}**\n"
                f"Decision: {result['decision']} | Direction: {result.get('direction')} | "
                f"Ticker: {result.get('selected_ticker')} | "
                f"Strike: {strike.get('strike')} | Contracts: {strike.get('contracts')}"
            )
        except Exception as e:
            logger.warning(f"Discord notification failed (non-fatal): {e}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ/IWM 0DTE order logic")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (default is dry-run).")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        results = run(args.date, dry_run=not args.live,
                      send_discord=not args.no_discord)
        print(json.dumps(results, indent=2, default=str))
    except Exception as e:
        log_exception_to_jira(e, "options_order_logic", {"date": args.date})
        logger.exception("options_order_logic failed")
        raise


if __name__ == "__main__":
    main()