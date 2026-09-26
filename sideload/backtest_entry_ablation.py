#!/usr/bin/env python3
"""Entry-engine replacement ablation for the SPY/QQQ/IWM 0DTE strategy.

The PMH/PML latch-on failed (35-44% win rate). This module tests two structural
entry alternatives on historical 1-minute data:

  Model A: Liquidity Sweep Fade (Mean Reversion)
    - Window: 09:30 to 10:15 ET
    - Setup: High[t] > PMH (liquidity swept), Close[t] < PMH (failed to hold),
             Close[t] crosses below VWAP.
    - Execution: Buy first OTM Put on candle close.
    - Exit: 20% premium stop; profit exit at opposite VWAP band or 11:30 time stop.
    - Mirror for PML sweep / Call buy.

  Model B: 15-Minute Compressed ORB (Volatility Expansion)
    - Range: 09:30 to 09:45 ET (OR_High, OR_Low).
    - Volatility gate: (OR_High - OR_Low) <= 0.65 * ATR(14). Abort if wide.
    - Execution (09:45 to 10:30 ET): 1-min Close > OR_High AND Volume > 1.25x
      SMA(Vol,10) -> Call; Close < OR_Low AND Volume > 1.25x SMA(Vol,10) -> Put.
    - Exit: 20% premium stop; trailing 5-min EMA or 11:30 time stop.

Because Alpaca paper has no OPRA option bars, we backtest the UNDERLYING move
and model option premium via a delta proxy. The key metric is the UNDERLYING
directional win rate (>= 55% required).

Usage:
    python -m sideload.backtest_entry_ablation --days 60
    python -m sideload.backtest_entry_ablation --days 60 --no-discord
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

logger = logging.getLogger("BacktestEntryAblation")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe.
TICKERS = ["SPY", "QQQ", "IWM"]

# Exit rules.
STOP_LOSS_PCT = 0.20          # 20% premium stop (per directive)
HARD_EXIT_TIME = dtime(11, 30, 0)  # hard time stop

# Option premium model (corrected first-order Taylor expansion).
DELTA_PROXY = 0.45
ENTRY_PREMIUM = 1.50          # ~$1.50 for SPY first-OTM 0DTE
THETA_DECAY_PER_15MIN = 0.015  # ~0.015 per 15 min of hold

# Model A window.
MODEL_A_START = dtime(9, 30, 0)
MODEL_A_END = dtime(10, 15, 0)

# Model B windows.
ORB_START = dtime(9, 30, 0)
ORB_END = dtime(9, 45, 0)
ORB_EXEC_START = dtime(9, 45, 0)
ORB_EXEC_END = dtime(10, 30, 0)
ORB_VOL_GATE = 0.65            # (OR_High - OR_Low) <= 0.65 * ATR(14)
ORB_VOL_MULT = 1.25            # Volume > 1.25x SMA(Vol, 10)

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


def _atr14(daily: pd.DataFrame) -> float:
    if len(daily) < 15:
        return 0.0
    df = daily.tail(15)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def _vwap_series(day_bars: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
    """Anchored VWAP from the 09:30 open bar (cumulative, no lookahead)."""
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


def _simulate_exit(day_bars: pd.DataFrame, entry_ts, direction: str,
                   entry_price: float, stop_loss_pct: float = STOP_LOSS_PCT,
                   use_ema_trail: bool = False) -> dict:
    """Simulate the exit from entry to the 11:30 hard exit.

    Models option premium as: premium = entry_premium * (1 + delta*move - theta).
    Applies the hard stop and (optionally) a 5-min EMA trailing stop.
    """
    exit_ts = entry_ts.replace(hour=HARD_EXIT_TIME.hour, minute=HARD_EXIT_TIME.minute)
    window = day_bars[(day_bars.index >= entry_ts) & (day_bars.index <= exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    entry_premium = ENTRY_PREMIUM
    premium = entry_premium
    peak_premium = entry_premium
    exit_premium = None
    exit_reason = "time_exit"
    entry_underlying = entry_price
    exit_underlying = entry_price

    # 5-min EMA for trailing (Model B).
    ema = None
    ema_span = 5

    for ts, bar in window.iterrows():
        px = float(bar["close"])
        # Dollar move (signed by direction).
        if direction == "BEARISH":
            dollar_move = entry_price - px
        else:
            dollar_move = px - entry_price
        minutes = (ts - entry_ts).total_seconds() / 60.0
        # Correct premium model: dollar_move * delta - theta decay.
        option_delta_gain = dollar_move * DELTA_PROXY
        decay_loss = (minutes / 15.0) * THETA_DECAY_PER_15MIN * entry_premium
        premium = max(0.01, entry_premium + option_delta_gain - decay_loss)
        exit_underlying = px

        # EMA for trailing.
        if use_ema_trail:
            ema = px if ema is None else px * (2 / (ema_span + 1)) + ema * (1 - 2 / (ema_span + 1))

        # Hard stop.
        if premium <= entry_premium * (1.0 - stop_loss_pct):
            exit_premium, exit_reason = premium, "stop_20pct"
            break
        # Trailing stop: 5-min EMA cross (Model B) or peak giveback (Model A).
        if use_ema_trail and ema is not None:
            if direction == "BULLISH" and px < ema:
                exit_premium, exit_reason = premium, "ema_trail"
                break
            if direction == "BEARISH" and px > ema:
                exit_premium, exit_reason = premium, "ema_trail"
                break
        else:
            peak_premium = max(peak_premium, premium)
            if peak_premium > entry_premium:
                if (peak_premium - premium) / peak_premium >= 0.15:
                    exit_premium, exit_reason = premium, "trailing_stop"
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
    }


# ---------------------------------------------------------------------------
# Model A: Liquidity Sweep Fade (Mean Reversion)
# ---------------------------------------------------------------------------
def model_a_sweep_fade(day_bars: pd.DataFrame, day: pd.Timestamp,
                       anchors: dict, vwap_series: pd.Series) -> dict | None:
    """Detect a liquidity sweep fade setup.

    Returns {'direction', 'entry_ts', 'entry_price'} or None.
    """
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
        close = float(bar["close"])
        vwap = _vwap_at(vwap_series, ts)

        # Bearish sweep: High > PMH (swept), Close < PMH (failed), Close < VWAP.
        if high > pmh and close < pmh and vwap is not None and close < vwap:
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close}

        # Bullish sweep: Low < PML (swept), Close > PML (failed), Close > VWAP.
        if low := float(bar["low"]):
            if low < pml and close > pml and vwap is not None and close > vwap:
                return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close}

    return None


# ---------------------------------------------------------------------------
# Model B: 15-Minute Compressed ORB (Volatility Expansion)
# ---------------------------------------------------------------------------
def model_b_compressed_orb(day_bars: pd.DataFrame, day: pd.Timestamp,
                           atr: float) -> dict | None:
    """Detect a compressed-ORB breakout setup.

    Returns {'direction', 'entry_ts', 'entry_price'} or None.
    """
    orb_start = day.replace(hour=ORB_START.hour, minute=ORB_START.minute)
    orb_end = day.replace(hour=ORB_END.hour, minute=ORB_END.minute)
    orb = day_bars[(day_bars.index >= orb_start) & (day_bars.index <= orb_end)]
    if orb.empty:
        return None
    or_high = float(orb["high"].max())
    or_low = float(orb["low"].min())

    # Volatility gate: range must be compressed.
    if atr <= 0:
        return None
    if (or_high - or_low) > ORB_VOL_GATE * atr:
        return None

    # Execution window 09:45-10:30.
    exec_start = day.replace(hour=ORB_EXEC_START.hour, minute=ORB_EXEC_START.minute)
    exec_end = day.replace(hour=ORB_EXEC_END.hour, minute=ORB_EXEC_END.minute)
    exec_bars = day_bars[(day_bars.index >= exec_start) & (day_bars.index <= exec_end)]
    if exec_bars.empty:
        return None

    # Volume SMA(10) for the volume confirmation.
    vol = exec_bars["volume"].astype(float)
    vol_sma = vol.rolling(10).mean()

    for ts, bar in exec_bars.iterrows():
        close = float(bar["close"])
        v = float(bar["volume"])
        sma = vol_sma.loc[ts] if ts in vol_sma.index else None
        if sma is None or pd.isna(sma) or sma <= 0:
            continue
        if close > or_high and v > ORB_VOL_MULT * sma:
            return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close}
        if close < or_low and v > ORB_VOL_MULT * sma:
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close}

    return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_model(client: AlpacaClient, symbol: str, days_back: int, model: str) -> dict:
    """Run a single entry model over N days for a symbol."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return {"symbol": symbol, "model": model, "trades": 0, "summary": {}}

    days = sorted(intraday.index.normalize().unique())
    results = []
    for day in days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
        if day_bars.empty:
            continue

        # Prior-day anchors (PDH/PDL/PDC, PMH/PML).
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
        atr = _atr14(daily)

        if model == "A":
            setup = model_a_sweep_fade(day_bars, day, anchors, vwap_series)
            use_ema = False
        else:  # "B"
            setup = model_b_compressed_orb(day_bars, day, atr)
            use_ema = True

        if setup is None:
            continue
        sim = _simulate_exit(day_bars, setup["entry_ts"], setup["direction"],
                             setup["entry_price"], use_ema_trail=use_ema)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    if not results:
        return {"symbol": symbol, "model": model, "trades": 0, "summary": {}}

    df = pd.DataFrame(results)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    um = df["underlying_move_pct"]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    summary = {
        "trades": len(df),
        "underlying_win_rate": round(float((um > 0).mean() * 100.0), 1),
        "option_win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "avg_pnl_pct": round(float(df["pnl_pct"].mean()), 2),
        "total_pnl_pct": round(float(df["pnl_pct"].sum()), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_underlying_move_pct": round(float(um.mean()), 3),
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
    }
    return {"symbol": symbol, "model": model, "trades": len(df),
            "summary": summary, "trades_detail": results}


def run_ablation(client: AlpacaClient, days_back: int) -> dict:
    """Run both models across all tickers."""
    results = {}
    for model in ["A", "B"]:
        for sym in TICKERS:
            logger.info(f"Running Model {model} for {sym} over {days_back} days...")
            results[f"{model}_{sym}"] = run_model(client, sym, days_back, model)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Entry-engine ablation (Sweep Fade vs Compressed ORB)")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history to backtest.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        results = run_ablation(client, args.days)

        out_path = os.path.join(OUT_DIR, "backtest_entry_ablation.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote ablation to {out_path}")

        # Print comparative table.
        print(f"{'Model':<8} {'Ticker':<8} {'Trades':<8} {'UMWin%':<8} {'OptWin%':<8} {'TotalPnL%':<10} {'PF':<8}")
        for key, res in results.items():
            model, sym = key.split("_", 1)
            s = res.get("summary", {})
            print(f"{model:<8} {sym:<8} {s.get('trades',0):<8} "
                  f"{s.get('underlying_win_rate',0):<8} {s.get('option_win_rate',0):<8} "
                  f"{s.get('total_pnl_pct',0):<10} {s.get('profit_factor',0):<8}")

        if not args.no_discord:
            try:
                lines = [f"**Entry Ablation ({args.days}d)**"]
                for key, res in results.items():
                    model, sym = key.split("_", 1)
                    s = res.get("summary", {})
                    lines.append(
                        f"`{model}-{sym}` trades={s.get('trades',0)} "
                        f"UMWin={s.get('underlying_win_rate',0)}% "
                        f"OptWin={s.get('option_win_rate',0)}% "
                        f"PnL={s.get('total_pnl_pct',0)}% PF={s.get('profit_factor',0)}"
                    )
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_entry_ablation", {"days": args.days})
        logger.exception("backtest_entry_ablation failed")
        raise


if __name__ == "__main__":
    main()