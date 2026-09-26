#!/usr/bin/env python3
"""Model A (Sweep Fade) asymmetry & execution calibration (Phase 1e).

With the pricing model corrected, Model A on QQQ achieved PF 1.56. This module
eliminates the -36% bar-close stop overshoot, tests scaled positive asymmetry
(+25%/+50%), and introduces an intraday volatility filter to target PF >= 2.0.

Engine corrections:
  A. Intrabar stop precision: check intrabar extremes (Low for Calls, High for
     Puts), capping realized loss at -20% + 2% slippage = -22% hard cap.
  B. Asymmetric two-tranche exit:
       Tranche 1 (50%): limit exit at +25% premium gain.
       Breakeven ratchet: once Tranche 1 fills, move Tranche 2 stop to 0%.
       Tranche 2 (50%): limit exit at +50% premium gain, or 30-min time stop.
  C. Pre-market volatility filter: only arm if (PMH - PML) / PML >= 0.0035.

Test matrix (60 days, QQQ & SPY):
  Run 1: Single target (+35% TP, hard -22% stop, 30m max hold).
  Run 2: Two-tranche scaled target (50% @ +25%, 50% @ +50%, BE ratchet).

Usage:
    python -m sideload.backtest_model_a_asymmetry --days 60
    python -m sideload.backtest_model_a_asymmetry --days 60 --no-discord
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

logger = logging.getLogger("BacktestModelAAsymmetry")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Model A window.
MODEL_A_START = dtime(9, 30, 0)
MODEL_A_END = dtime(10, 15, 0)

# Exit architecture.
STOP_LOSS_PCT = 0.20          # -20% premium stop
SPREAD_SLIPPAGE = 0.02        # +2% empirical spread slippage
HARD_STOP_CAP = STOP_LOSS_PCT + SPREAD_SLIPPAGE  # -22% hard cap
MAX_HOLD_MINUTES = 30         # 30-min max hold

# Two-tranche targets.
TRANCH1_PCT = 0.25            # Tranche 1: +25%
TRANCH2_PCT = 0.50            # Tranche 2: +50%
TRANCH1_FRACTION = 0.50       # 50% of contracts in Tranche 1

# Pre-market volatility filter.
PM_VOL_MIN = 0.0035           # (PMH - PML) / PML >= 0.35%

# Corrected premium model.
DELTA = 0.45
THETA_DECAY_PER_15MIN = 0.015

# Dynamic contract pricing per ticker (Phase 1f).
TICKER_CONFIG = {
    "QQQ": {"entry_premium": 1.50, "delta": 0.45},
    "NVDA": {"entry_premium": 2.50, "delta": 0.45},
    "TSLA": {"entry_premium": 3.50, "delta": 0.45},
}
# Default entry premium (fallback).
ENTRY_PREMIUM = 1.50

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


def _premium_at(entry_price: float, current_price: float, direction: str,
                minutes: float, entry_premium: float = ENTRY_PREMIUM,
                delta: float = DELTA) -> float:
    """Compute the option premium at a given underlying price (corrected model)."""
    if direction == "BEARISH":
        dollar_move = entry_price - current_price
    else:
        dollar_move = current_price - entry_price
    option_delta_gain = dollar_move * delta
    decay_loss = (minutes / 15.0) * THETA_DECAY_PER_15MIN * entry_premium
    return max(0.01, entry_premium + option_delta_gain - decay_loss)


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


def _simulate_single(day_bars: pd.DataFrame, entry_ts, direction: str,
                     entry_price: float, tp_pct: float,
                     entry_premium: float = ENTRY_PREMIUM,
                     delta: float = DELTA) -> dict:
    """Run 1: single target with intrabar stop precision.

    - Take-profit at +tp_pct.
    - Hard stop at -22% (intrabar, using Low for calls / High for puts).
    - 30-min max hold.
    """
    max_exit_ts = entry_ts + timedelta(minutes=MAX_HOLD_MINUTES)
    window = day_bars[(day_bars.index >= entry_ts) & (day_bars.index <= max_exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    exit_premium = None
    exit_reason = "time_stop"
    hold_minutes = 0.0

    for ts, bar in window.iterrows():
        minutes = (ts - entry_ts).total_seconds() / 60.0
        hold_minutes = minutes
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])

        # Intrabar stop check: use the extreme against us.
        if direction == "BULLISH":
            stop_px = low
        else:
            stop_px = high
        stop_premium = _premium_at(entry_price, stop_px, direction, minutes,
                                   entry_premium, delta)
        if stop_premium <= entry_premium * (1.0 - HARD_STOP_CAP):
            # Cap the realized loss at exactly -22% (hard cap), regardless of
            # how far the intrabar extreme actually moved.
            exit_premium, exit_reason = entry_premium * (1.0 - HARD_STOP_CAP), "stop_22pct"
            break

        # Take-profit on close.
        close_premium = _premium_at(entry_price, close, direction, minutes,
                                    entry_premium, delta)
        if close_premium >= entry_premium * (1.0 + tp_pct):
            exit_premium, exit_reason = close_premium, "take_profit"
            break

        exit_premium = close_premium

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    return {"traded": True, "direction": direction, "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct * 100.0, 2), "hold_minutes": round(hold_minutes, 1)}


def _simulate_two_tranche(day_bars: pd.DataFrame, entry_ts, direction: str,
                          entry_price: float,
                          entry_premium: float = ENTRY_PREMIUM,
                          delta: float = DELTA) -> dict:
    """Run 2: two-tranche scaled target with breakeven ratchet.

    - Tranche 1 (50%): exit at +25%.
    - Breakeven ratchet: once Tranche 1 fills, move Tranche 2 stop to 0%.
    - Tranche 2 (50%): exit at +50%, or 30-min time stop.
    - Hard stop at -22% (intrabar).
    """
    max_exit_ts = entry_ts + timedelta(minutes=MAX_HOLD_MINUTES)
    window = day_bars[(day_bars.index >= entry_ts) & (day_bars.index <= max_exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    tranche1_filled = False
    tranche2_stop = HARD_STOP_CAP  # -22% until Tranche 1 fills
    exit_premium = None
    exit_reason = "time_stop"
    hold_minutes = 0.0

    for ts, bar in window.iterrows():
        minutes = (ts - entry_ts).total_seconds() / 60.0
        hold_minutes = minutes
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])

        # Intrabar stop check.
        stop_px = low if direction == "BULLISH" else high
        stop_premium = _premium_at(entry_price, stop_px, direction, minutes,
                                   entry_premium, delta)
        if stop_premium <= entry_premium * (1.0 - tranche2_stop):
            # Cap the realized loss at the stop level (hard cap).
            exit_premium, exit_reason = entry_premium * (1.0 - tranche2_stop), "stop_22pct"
            break

        close_premium = _premium_at(entry_price, close, direction, minutes,
                                    entry_premium, delta)

        # Tranche 1: fill at +25%.
        if not tranche1_filled and close_premium >= entry_premium * (1.0 + TRANCH1_PCT):
            tranche1_filled = True
            tranche2_stop = 0.0  # breakeven ratchet
            # Continue holding Tranche 2.

        # Tranche 2: fill at +50%.
        if tranche1_filled and close_premium >= entry_premium * (1.0 + TRANCH2_PCT):
            # Both tranches filled: weighted average.
            avg = (TRANCH1_FRACTION * entry_premium * (1.0 + TRANCH1_PCT)
                   + (1 - TRANCH1_FRACTION) * close_premium)
            exit_premium, exit_reason = avg, "two_tranche_full"
            break

        exit_premium = close_premium

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    # If only Tranche 1 filled, weight it; else full close.
    if tranche1_filled and exit_reason != "two_tranche_full":
        # Tranche 1 locked at +25%, Tranche 2 at current premium.
        t1 = entry_premium * (1.0 + TRANCH1_PCT)
        t2 = exit_premium
        exit_premium = TRANCH1_FRACTION * t1 + (1 - TRANCH1_FRACTION) * t2

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    return {"traded": True, "direction": direction, "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct * 100.0, 2), "hold_minutes": round(hold_minutes, 1)}


def run_model_a(client: AlpacaClient, symbol: str, days_back: int,
                run: str, pm_vol_min: float = PM_VOL_MIN) -> dict:
    """Run Model A with a specific exit configuration."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return {"symbol": symbol, "run": run, "trades": 0, "summary": {}}

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

        # Pre-market volatility filter: (PMH - PML) / PML >= threshold.
        if pmh is None or pml is None or pml <= 0:
            continue
        if (pmh - pml) / pml < pm_vol_min:
            continue

        vwap_series = _vwap_series(day_bars, day)
        setup = _model_a_setup(day_bars, day, anchors, vwap_series)
        if setup is None:
            continue

        # Per-ticker contract pricing.
        cfg = TICKER_CONFIG.get(symbol, {"entry_premium": ENTRY_PREMIUM, "delta": DELTA})
        ep = cfg["entry_premium"]
        dl = cfg["delta"]

        if run == "Run1":
            sim = _simulate_single(day_bars, setup["entry_ts"], setup["direction"],
                                   setup["entry_price"], tp_pct=0.35,
                                   entry_premium=ep, delta=dl)
        else:  # Run2
            sim = _simulate_two_tranche(day_bars, setup["entry_ts"], setup["direction"],
                                        setup["entry_price"], entry_premium=ep, delta=dl)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    if not results:
        return {"symbol": symbol, "run": run, "trades": 0, "summary": {}}

    df = pd.DataFrame(results)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    # Net PnL in % (sum of per-trade pnl_pct).
    total_pnl = float(df["pnl_pct"].sum())
    # Max drawdown: cumulative sum of pnl_pct.
    cum = df["pnl_pct"].cumsum()
    peak = cum.cummax()
    max_dd = float((cum - peak).min())
    summary = {
        "trades": len(df),
        "win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "avg_pnl_pct": round(float(df["pnl_pct"].mean()), 2),
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_pct": round(max_dd, 2),
        "avg_hold_minutes": round(float(df["hold_minutes"].mean()), 1),
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
    }
    return {"symbol": symbol, "run": run, "trades": len(df), "summary": summary,
            "trades_detail": results}


def run_matrix(client: AlpacaClient, days_back: int,
               pm_vol_min: float = PM_VOL_MIN) -> dict:
    """Run the test matrix for the expanded universe (NVDA, TSLA, QQQ)."""
    results = {}
    for sym in ["QQQ", "NVDA", "TSLA"]:
        for run in ["Run1", "Run2"]:
            logger.info(f"Running {run} for {sym} over {days_back} days...")
            results[f"{run}_{sym}"] = run_model_a(client, sym, days_back, run,
                                                  pm_vol_min=pm_vol_min)
    return results


def _combine_portfolio(results: dict, run: str) -> dict:
    """Combine all tickers for a given run into a portfolio summary."""
    all_trades = []
    for key, res in results.items():
        if key.startswith(run + "_"):
            all_trades.extend(res.get("trades_detail", []))
    if not all_trades:
        return {"trades": 0, "summary": {}}
    df = pd.DataFrame(all_trades)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    total_pnl = float(df["pnl_pct"].sum())
    cum = df["pnl_pct"].cumsum()
    peak = cum.cummax()
    max_dd = float((cum - peak).min())
    return {
        "trades": len(df),
        "summary": {
            "trades": len(df),
            "win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
            "total_pnl_pct": round(total_pnl, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "max_dd_pct": round(max_dd, 2),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Model A asymmetry & execution calibration")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history to backtest.")
    parser.add_argument("--pm-vol", type=float, default=PM_VOL_MIN,
                        help="Pre-market volatility filter threshold (fraction, e.g. 0.0035).")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        results = run_matrix(client, args.days, pm_vol_min=args.pm_vol)

        out_path = os.path.join(OUT_DIR, "backtest_model_a_asymmetry.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote asymmetry calibration to {out_path}")

        # Print comparative table.
        print(f"{'Run':<8} {'Ticker':<8} {'Trades':<8} {'Win%':<8} {'NetPnL%':<10} {'PF':<8} {'MaxDD%':<8} {'AvgHold':<8}")
        for key, res in results.items():
            run, sym = key.split("_", 1)
            s = res.get("summary", {})
            print(f"{run:<8} {sym:<8} {s.get('trades',0):<8} "
                  f"{s.get('win_rate',0):<8} {s.get('total_pnl_pct',0):<10} "
                  f"{s.get('profit_factor',0):<8} {s.get('max_dd_pct',0):<8} "
                  f"{s.get('avg_hold_minutes',0):<8}")

        # Combined portfolio stats.
        print("\nCombined Portfolio:")
        for run in ["Run1", "Run2"]:
            combo = _combine_portfolio(results, run)
            cs = combo.get("summary", {})
            print(f"  {run}: trades={cs.get('trades',0)} win={cs.get('win_rate',0)}% "
                  f"PnL={cs.get('total_pnl_pct',0)}% PF={cs.get('profit_factor',0)} "
                  f"MaxDD={cs.get('max_dd_pct',0)}%")

        if not args.no_discord:
            try:
                lines = [f"**Model A Asymmetry ({args.days}d)**"]
                for key, res in results.items():
                    run, sym = key.split("_", 1)
                    s = res.get("summary", {})
                    lines.append(
                        f"`{run}-{sym}` trades={s.get('trades',0)} "
                        f"Win={s.get('win_rate',0)}% PnL={s.get('total_pnl_pct',0)}% "
                        f"PF={s.get('profit_factor',0)} DD={s.get('max_dd_pct',0)}%"
                    )
                for run in ["Run1", "Run2"]:
                    combo = _combine_portfolio(results, run)
                    cs = combo.get("summary", {})
                    lines.append(
                        f"`{run}-PORTFOLIO` trades={cs.get('trades',0)} "
                        f"Win={cs.get('win_rate',0)}% PnL={cs.get('total_pnl_pct',0)}% "
                        f"PF={cs.get('profit_factor',0)} DD={cs.get('max_dd_pct',0)}%"
                    )
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_model_a_asymmetry", {"days": args.days})
        logger.exception("backtest_model_a_asymmetry failed")
        raise


if __name__ == "__main__":
    main()