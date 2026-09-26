#!/usr/bin/env python3
"""Model A high-beta calibration & acceptance gate push (Phase 1h).

Drop the QQQ index drag. Calibrate target asymmetry across the 252-day dataset
for TSLA and NVDA to achieve Portfolio PF >= 1.80.

Universe: TSLA ($3.50, delta 0.45), NVDA ($2.50, delta 0.45). QQQ dropped.

Ablation matrix (252 days):
  Ablation 1: Baseline +35% single target.
  Ablation 2: Expanded asymmetry +45% single target.
  Ablation 3: Two-tranche (50% @ +25%, breakeven ratchet, 50% @ +55%).
  Ablation 4: Ablation 2 + VWAP 3-bar slope filter.

Usage:
    python -m sideload.backtest_model_a_phase1h --days 252
    python -m sideload.backtest_model_a_phase1h --days 252 --no-discord
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

logger = logging.getLogger("BacktestModelAPhase1h")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Model A window.
MODEL_A_START = dtime(9, 30, 0)
MODEL_A_END = dtime(10, 15, 0)

# Exit architecture.
STOP_LOSS_PCT = 0.20
SPREAD_SLIPPAGE = 0.02
HARD_STOP_CAP = STOP_LOSS_PCT + SPREAD_SLIPPAGE  # -22%
MAX_HOLD_MINUTES = 30

# Pre-market volatility filter.
PM_VOL_MIN = 0.0035

# Corrected premium model.
THETA_DECAY_PER_15MIN = 0.015

# Universe (QQQ dropped).
TICKER_CONFIG = {
    "NVDA": {"entry_premium": 2.50, "delta": 0.45},
    "TSLA": {"entry_premium": 3.50, "delta": 0.45},
}
UNIVERSE = ["TSLA", "NVDA"]

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


def _vwap_slope_ok(vwap_series: pd.Series, ts, direction: str) -> bool:
    """VWAP 3-bar slope filter.

    Bullish: VWAP[t] > VWAP[t-3].  Bearish: VWAP[t] < VWAP[t-3].
    """
    vwap_t = _vwap_at(vwap_series, ts)
    ts_3 = ts - timedelta(minutes=3)
    vwap_3 = _vwap_at(vwap_series, ts_3)
    if vwap_t is None or vwap_3 is None:
        return False
    if direction == "BULLISH":
        return vwap_t > vwap_3
    else:
        return vwap_t < vwap_3


def _premium_at(entry_price: float, current_price: float, direction: str,
                minutes: float, entry_premium: float, delta: float) -> float:
    if direction == "BEARISH":
        dollar_move = entry_price - current_price
    else:
        dollar_move = current_price - entry_price
    option_delta_gain = dollar_move * delta
    decay_loss = (minutes / 15.0) * THETA_DECAY_PER_15MIN * entry_premium
    return max(0.01, entry_premium + option_delta_gain - decay_loss)


def _model_a_setup(day_bars: pd.DataFrame, day: pd.Timestamp,
                   anchors: dict, vwap_series: pd.Series,
                   use_vwap_slope: bool = False) -> dict | None:
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
            if use_vwap_slope and not _vwap_slope_ok(vwap_series, ts, "BEARISH"):
                continue
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close}

        # Bullish sweep: Low < PML, Close > PML, Close > VWAP.
        if low < pml and close > pml and vwap is not None and close > vwap:
            if use_vwap_slope and not _vwap_slope_ok(vwap_series, ts, "BULLISH"):
                continue
            return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close}

    return None


def _simulate_single(day_bars: pd.DataFrame, entry_ts, direction: str,
                     entry_price: float, tp_pct: float,
                     entry_premium: float, delta: float) -> dict:
    """Single target with intrabar stop precision and symmetric execution."""
    max_exit_ts = entry_ts + timedelta(minutes=MAX_HOLD_MINUTES)
    window = day_bars[(day_bars.index > entry_ts) & (day_bars.index <= max_exit_ts)]
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

        favorable_px = high if direction == "BULLISH" else low
        adverse_px = low if direction == "BULLISH" else high

        stop_premium = _premium_at(entry_price, adverse_px, direction, minutes,
                                   entry_premium, delta)
        target_premium = _premium_at(entry_price, favorable_px, direction, minutes,
                                     entry_premium, delta)

        # Stop priority.
        if stop_premium <= entry_premium * (1.0 - HARD_STOP_CAP):
            exit_premium, exit_reason = entry_premium * (1.0 - HARD_STOP_CAP), "stop_22pct"
            break
        # Take-profit on favorable extreme.
        if target_premium >= entry_premium * (1.0 + tp_pct):
            exit_premium, exit_reason = entry_premium * (1.0 + tp_pct), "take_profit"
            break
        # Mark-to-market on close.
        exit_premium = _premium_at(entry_price, close, direction, minutes,
                                   entry_premium, delta)

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    return {"traded": True, "direction": direction, "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct * 100.0, 2), "hold_minutes": round(hold_minutes, 1)}


def _simulate_two_tranche(day_bars: pd.DataFrame, entry_ts, direction: str,
                          entry_price: float, entry_premium: float,
                          delta: float, tranch1_pct: float = 0.25,
                          tranch2_pct: float = 0.55) -> dict:
    """Two-tranche scaled target with breakeven ratchet."""
    max_exit_ts = entry_ts + timedelta(minutes=MAX_HOLD_MINUTES)
    window = day_bars[(day_bars.index > entry_ts) & (day_bars.index <= max_exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    tranche1_filled = False
    tranche2_stop = HARD_STOP_CAP
    exit_premium = None
    exit_reason = "time_stop"
    hold_minutes = 0.0

    for ts, bar in window.iterrows():
        minutes = (ts - entry_ts).total_seconds() / 60.0
        hold_minutes = minutes
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])

        favorable_px = high if direction == "BULLISH" else low
        adverse_px = low if direction == "BULLISH" else high

        stop_premium = _premium_at(entry_price, adverse_px, direction, minutes,
                                   entry_premium, delta)
        target_premium = _premium_at(entry_price, favorable_px, direction, minutes,
                                     entry_premium, delta)

        # Stop priority.
        if stop_premium <= entry_premium * (1.0 - tranche2_stop):
            if tranche1_filled and tranche2_stop == 0.0:
                exit_premium, exit_reason = entry_premium, "breakeven_stop"
            else:
                exit_premium, exit_reason = entry_premium * (1.0 - tranche2_stop), "stop_22pct"
            break

        # Tranche 1.
        if not tranche1_filled and target_premium >= entry_premium * (1.0 + tranch1_pct):
            tranche1_filled = True
            tranche2_stop = 0.0

        # Tranche 2.
        if tranche1_filled and target_premium >= entry_premium * (1.0 + tranch2_pct):
            avg = (0.5 * entry_premium * (1.0 + tranch1_pct)
                   + 0.5 * entry_premium * (1.0 + tranch2_pct))
            exit_premium, exit_reason = avg, "two_tranche_full"
            break

        exit_premium = _premium_at(entry_price, close, direction, minutes,
                                   entry_premium, delta)

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    if tranche1_filled and exit_reason not in ("two_tranche_full", "breakeven_stop"):
        t1 = entry_premium * (1.0 + tranch1_pct)
        t2 = exit_premium
        exit_premium = 0.5 * t1 + 0.5 * t2

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    return {"traded": True, "direction": direction, "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct * 100.0, 2), "hold_minutes": round(hold_minutes, 1)}


def run_ablation(client: AlpacaClient, symbol: str, days_back: int,
                 ablation: str) -> dict:
    """Run a single ablation for a symbol."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return {"symbol": symbol, "ablation": ablation, "trades": 0, "summary": {}}

    cfg = TICKER_CONFIG[symbol]
    ep = cfg["entry_premium"]
    dl = cfg["delta"]

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

        # Pre-market volatility filter.
        if pmh is None or pml is None or pml <= 0:
            continue
        if (pmh - pml) / pml < PM_VOL_MIN:
            continue

        vwap_series = _vwap_series(day_bars, day)
        use_vwap_slope = (ablation == "Abl4")
        setup = _model_a_setup(day_bars, day, anchors, vwap_series,
                               use_vwap_slope=use_vwap_slope)
        if setup is None:
            continue

        if ablation == "Abl1":
            sim = _simulate_single(day_bars, setup["entry_ts"], setup["direction"],
                                   setup["entry_price"], tp_pct=0.35, entry_premium=ep, delta=dl)
        elif ablation == "Abl2":
            sim = _simulate_single(day_bars, setup["entry_ts"], setup["direction"],
                                   setup["entry_price"], tp_pct=0.45, entry_premium=ep, delta=dl)
        elif ablation == "Abl3":
            sim = _simulate_two_tranche(day_bars, setup["entry_ts"], setup["direction"],
                                        setup["entry_price"], ep, dl, 0.25, 0.55)
        else:  # Abl4
            sim = _simulate_single(day_bars, setup["entry_ts"], setup["direction"],
                                   setup["entry_price"], tp_pct=0.45, entry_premium=ep, delta=dl)

        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    if not results:
        return {"symbol": symbol, "ablation": ablation, "trades": 0, "summary": {}}

    df = pd.DataFrame(results)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    total_pnl = float(df["pnl_pct"].sum())
    cum = df["pnl_pct"].cumsum()
    peak = cum.cummax()
    max_dd = float((cum - peak).min())
    summary = {
        "trades": len(df),
        "win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_pct": round(max_dd, 2),
        "avg_hold_minutes": round(float(df["hold_minutes"].mean()), 1),
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
    }
    return {"symbol": symbol, "ablation": ablation, "trades": len(df),
            "summary": summary, "trades_detail": results}


def _combine(results: dict, ablation: str) -> dict:
    """Combine all tickers for an ablation into a portfolio summary."""
    all_trades = []
    for key, res in results.items():
        if res.get("ablation") == ablation:
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


def run_matrix(client: AlpacaClient, days_back: int) -> dict:
    """Run all ablations across the universe."""
    results = {}
    for ablation in ["Abl1", "Abl2", "Abl3", "Abl4"]:
        for sym in UNIVERSE:
            logger.info(f"Running {ablation} for {sym} over {days_back} days...")
            results[f"{ablation}_{sym}"] = run_ablation(client, sym, days_back, ablation)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Model A high-beta calibration (Phase 1h)")
    parser.add_argument("--days", type=int, default=252,
                        help="Days of history to backtest.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        results = run_matrix(client, args.days)

        out_path = os.path.join(OUT_DIR, "backtest_model_a_phase1h.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote phase1h calibration to {out_path}")

        # Per-ticker table.
        print(f"{'Ablation':<10} {'Ticker':<8} {'Trades':<8} {'Win%':<8} {'NetPnL%':<10} {'PF':<8} {'MaxDD%':<8}")
        for key, res in results.items():
            abl, sym = key.split("_", 1)
            s = res.get("summary", {})
            print(f"{abl:<10} {sym:<8} {s.get('trades',0):<8} "
                  f"{s.get('win_rate',0):<8} {s.get('total_pnl_pct',0):<10} "
                  f"{s.get('profit_factor',0):<8} {s.get('max_dd_pct',0):<8}")

        # Combined portfolio table.
        print("\nCombined Portfolio:")
        print(f"{'Ablation':<10} {'Trades':<8} {'Win%':<8} {'NetPnL%':<10} {'PF':<8} {'MaxDD%':<8}")
        for ablation in ["Abl1", "Abl2", "Abl3", "Abl4"]:
            combo = _combine(results, ablation)
            cs = combo.get("summary", {})
            print(f"{ablation:<10} {cs.get('trades',0):<8} "
                  f"{cs.get('win_rate',0):<8} {cs.get('total_pnl_pct',0):<10} "
                  f"{cs.get('profit_factor',0):<8} {cs.get('max_dd_pct',0):<8}")

        if not args.no_discord:
            try:
                lines = [f"**Model A Phase 1h ({args.days}d)**"]
                for ablation in ["Abl1", "Abl2", "Abl3", "Abl4"]:
                    combo = _combine(results, ablation)
                    cs = combo.get("summary", {})
                    lines.append(
                        f"`{ablation}` trades={cs.get('trades',0)} "
                        f"Win={cs.get('win_rate',0)}% PnL={cs.get('total_pnl_pct',0)}% "
                        f"PF={cs.get('profit_factor',0)} DD={cs.get('max_dd_pct',0)}%"
                    )
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_model_a_phase1h", {"days": args.days})
        logger.exception("backtest_model_a_phase1h failed")
        raise


if __name__ == "__main__":
    main()