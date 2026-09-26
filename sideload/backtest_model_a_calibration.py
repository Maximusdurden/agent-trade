#!/usr/bin/env python3
"""Model A (Sweep Fade) exit & premium architecture calibration.

Model A on SPY established a 56.0% underlying edge. This module fixes the
premium-model distortion and structural duration drag by testing:

  - Shorter hold windows (30-min max instead of 11:30 time stop).
  - Explicit profit targets (+25% premium, VWAP reversion).
  - A calibrated premium model with accelerated 0DTE decay.

Exit architecture:
  - Take-Profit: exit at +25.0% premium gain over entry ask.
  - Underlying Target (VWAP Reversion): exit when underlying crosses VWAP.
  - Max Duration Cap: 30 minutes (entry_bar + 30).
  - Stop-Loss: -20.0% premium loss.

Premium model (calibrated):
  dPremium = (dUnderlying * Delta * 100) - (DecayRate * MinutesHeld)
  Delta = 0.45, DecayRate capped at 1.5% premium per 15 min (0.1%/min).

Test matrix (SPY, 60 days):
  Run 1: VWAP exit only + 30-min time stop.
  Run 2: +25% target only + -20% stop + 30-min time stop.
  Run 3: Confluent (+25% OR VWAP) + 30-min time stop.

Usage:
    python -m sideload.backtest_model_a_calibration --days 60
    python -m sideload.backtest_model_a_calibration --days 60 --no-discord
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
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestModelACalibration")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Model A window.
MODEL_A_START = dtime(9, 30, 0)
MODEL_A_END = dtime(10, 15, 0)

# Exit architecture.
STOP_LOSS_PCT = 0.20          # -20% premium stop
TAKE_PROFIT_PCT = 0.25        # +25% premium target
MAX_HOLD_MINUTES = 30         # 30-min max hold

# Calibrated premium model (corrected first-order Taylor expansion).
DELTA = 0.45
ENTRY_PREMIUM = 1.50          # ~$1.50 for SPY first-OTM 0DTE
THETA_DECAY_PER_15MIN = 0.015  # ~0.015 per 15 min of hold

INTRADAY_INTERVAL = "1min"


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
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


def _load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
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


def _model_a_setup(day_bars: pd.DataFrame, day: pd.Timestamp,
                   anchors: dict, vwap_series: pd.Series) -> dict | None:
    """Detect a liquidity sweep fade setup (Model A)."""
    a_start = day.replace(hour=MODEL_A_START.hour, minute=MODEL_A_START.minute)
    a_end = day.replace(hour=MODEL_A_END.hour, minute=MODEL_A_END.minute)
    window = day_bars[(day_bars.index >= a_start) & (day_bars.index <= a_end)]
    if window.empty:
        return None
    pmh, pml = anchors.get("pmh"), anchors.get("pml")
    if pmh is None or pml is None:
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


def _simulate_exit(day_bars: pd.DataFrame, entry_ts, direction: str,
                   entry_price: float, vwap_series: pd.Series,
                   use_vwap_exit: bool, use_tp: bool) -> dict:
    """Simulate the exit with the calibrated premium model.

    Exit rules:
      - Stop: -20% premium.
      - Take-profit: +25% premium (if use_tp).
      - VWAP reversion: underlying crosses VWAP (if use_vwap_exit).
      - Max hold: 30 minutes.
    """
    max_exit_ts = entry_ts + timedelta(minutes=MAX_HOLD_MINUTES)
    window = day_bars[(day_bars.index >= entry_ts) & (day_bars.index <= max_exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    entry_premium = ENTRY_PREMIUM
    premium = entry_premium
    exit_premium = None
    exit_reason = "time_stop"
    entry_underlying = entry_price
    exit_underlying = entry_price
    hold_minutes = 0.0

    for ts, bar in window.iterrows():
        px = float(bar["close"])
        # Dollar move (signed by direction).
        if direction == "BEARISH":
            dollar_move = entry_price - px
        else:
            dollar_move = px - entry_price
        minutes = (ts - entry_ts).total_seconds() / 60.0
        hold_minutes = minutes
        # Correct premium model: dollar_move * delta - theta decay.
        option_delta_gain = dollar_move * DELTA
        decay_loss = (minutes / 15.0) * THETA_DECAY_PER_15MIN * entry_premium
        premium = max(0.01, entry_premium + option_delta_gain - decay_loss)
        exit_underlying = px

        # Stop-loss.
        if premium <= entry_premium * (1.0 - STOP_LOSS_PCT):
            exit_premium, exit_reason = premium, "stop_20pct"
            break
        # Take-profit.
        if use_tp and premium >= entry_premium * (1.0 + TAKE_PROFIT_PCT):
            exit_premium, exit_reason = premium, "take_profit"
            break
        # VWAP reversion exit.
        if use_vwap_exit:
            vwap = _vwap_at(vwap_series, ts)
            if vwap is not None:
                if direction == "BULLISH" and px <= vwap:
                    exit_premium, exit_reason = premium, "vwap_reversion"
                    break
                if direction == "BEARISH" and px >= vwap:
                    exit_premium, exit_reason = premium, "vwap_reversion"
                    break
        exit_premium = premium

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    underlying_move = (exit_underlying - entry_underlying) / entry_underlying
    if direction == "BEARISH":
        underlying_move = -underlying_move
    return {
        "traded": True,
        "direction": direction,
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct * 100.0, 2),
        "underlying_move_pct": round(underlying_move * 100.0, 2),
        "hold_minutes": round(hold_minutes, 1),
    }


def run_model_a(client: AlpacaClient, symbol: str, days_back: int,
                use_vwap_exit: bool, use_tp: bool) -> dict:
    """Run Model A with a specific exit configuration."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return {"symbol": symbol, "trades": 0, "summary": {}}

    days = sorted(intraday.index.normalize().unique())
    results = []
    for day in days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
        if day_bars.empty:
            continue

        day_naive = day.tz_localize(None) if day.tzinfo is not None else day
        prior = daily[daily.index < day_naive]
        if prior.empty:
            continue
        prior_row = prior.iloc[-1]
        pdh, pdl = float(prior_row["high"]), float(prior_row["low"])
        pm_start = day_start.replace(hour=4, minute=0)
        pm_end = day_start.replace(hour=9, minute=29)
        pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
        pmh = float(pm_bars["high"].max()) if not pm_bars.empty else None
        pml = float(pm_bars["low"].min()) if not pm_bars.empty else None
        anchors = {"pdh": pdh, "pdl": pdl, "pmh": pmh, "pml": pml}

        vwap_series = _vwap_series(day_bars, day)
        setup = _model_a_setup(day_bars, day, anchors, vwap_series)
        if setup is None:
            continue
        sim = _simulate_exit(day_bars, setup["entry_ts"], setup["direction"],
                             setup["entry_price"], vwap_series,
                             use_vwap_exit=use_vwap_exit, use_tp=use_tp)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    if not results:
        return {"symbol": symbol, "trades": 0, "summary": {}}

    df = pd.DataFrame(results)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    um = df["underlying_move_pct"]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    # Net PnL in $: assume $500 allocation, 1R = $500 * 20% = $100.
    # PnL% is on premium; net $ = sum(pnl_pct/100 * 500).
    net_pnl = float((df["pnl_pct"] / 100.0 * 500.0).sum())
    summary = {
        "trades": len(df),
        "underlying_win_rate": round(float((um > 0).mean() * 100.0), 1),
        "option_win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "avg_pnl_pct": round(float(df["pnl_pct"].mean()), 2),
        "total_pnl_pct": round(float(df["pnl_pct"].sum()), 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_hold_minutes": round(float(df["hold_minutes"].mean()), 1),
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
    }
    return {"symbol": symbol, "trades": len(df), "summary": summary,
            "trades_detail": results}


def run_matrix(client: AlpacaClient, days_back: int) -> dict:
    """Run the 3-configuration test matrix for SPY."""
    configs = {
        "Run1_VWAP": {"use_vwap_exit": True, "use_tp": False},
        "Run2_TP25": {"use_vwap_exit": False, "use_tp": True},
        "Run3_Confluent": {"use_vwap_exit": True, "use_tp": True},
    }
    results = {}
    for name, cfg in configs.items():
        logger.info(f"Running {name} for SPY over {days_back} days...")
        results[name] = run_model_a(client, "SPY", days_back, **cfg)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Model A exit & premium calibration")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history to backtest.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        results = run_matrix(client, args.days)

        out_path = os.path.join(OUT_DIR, "backtest_model_a_calibration.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote calibration to {out_path}")

        # Print comparative table.
        print(f"{'Config':<16} {'Trades':<8} {'UMWin%':<8} {'OptWin%':<8} {'NetPnL$':<10} {'PF':<8} {'AvgHold':<8}")
        for name, res in results.items():
            s = res.get("summary", {})
            print(f"{name:<16} {s.get('trades',0):<8} "
                  f"{s.get('underlying_win_rate',0):<8} {s.get('option_win_rate',0):<8} "
                  f"{s.get('net_pnl',0):<10} {s.get('profit_factor',0):<8} "
                  f"{s.get('avg_hold_minutes',0):<8}")

        if not args.no_discord:
            try:
                lines = [f"**Model A Calibration ({args.days}d)**"]
                for name, res in results.items():
                    s = res.get("summary", {})
                    lines.append(
                        f"`{name}` trades={s.get('trades',0)} "
                        f"UMWin={s.get('underlying_win_rate',0)}% "
                        f"OptWin={s.get('option_win_rate',0)}% "
                        f"NetPnL=${s.get('net_pnl',0)} PF={s.get('profit_factor',0)} "
                        f"Hold={s.get('avg_hold_minutes',0)}m"
                    )
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_model_a_calibration", {"days": args.days})
        logger.exception("backtest_model_a_calibration failed")
        raise


if __name__ == "__main__":
    main()