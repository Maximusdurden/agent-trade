#!/usr/bin/env python3
"""Forward (walk-forward) validation of Model A conviction filters on TSLA.

Evaluates candidate filters on chronological folds so each trade is scored
out-of-sample relative to the filter decision (no lookahead in the filter
itself — the filter is a static rule, but we verify the edge is stable across
time rather than concentrated in one regime).

Filters evaluated (all on top of the $0.25 baseline penetration gate):
  1. PEN1.00      : penetration >= $1.00
  2. RANGE_PCT    : PM range within 0.8% - 1.5% of TSLA price
  3. RANGE_CEIL   : penetration >= $1.00 AND PM range <= $4.00
  4. RANGE_BAND   : PM range within $2.00 - $4.00 (the original find)

Usage:
    python -m sideload.forward_validate_conviction [--days 252] [--folds 3]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.alpaca_client import AlpacaClient
from sideload.backtest_entry_ablation import (
    _load_daily,
    _load_intraday,
    _simulate_exit,
    _vwap_at,
    _vwap_series,
    MODEL_A_START,
    MODEL_A_END,
    TICKER_PREMIUMS,
    DEFAULT_PREMIUM,
    SWEEP_MIN_PENETRATION,
)

logger = logging.getLogger("ForwardValidateConviction")
ET = ZoneInfo("America/New_York")

SYMBOL = "TSLA"


def _detect_setup(day_bars: pd.DataFrame, day: pd.Timestamp,
                  anchors: dict, vwap_series: pd.Series,
                  min_pen: float) -> dict | None:
    """Model A setup detection with penetration + PM range metadata.

    Mirrors model_a_sweep_fade but returns penetration/range for filtering.
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

        if (high - pmh) >= min_pen and close < pmh and vwap is not None and close < vwap:
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close,
                    "penetration": round(high - pmh, 2),
                    "pm_range": round(pmh - pml, 2)}
        low = float(bar["low"])
        if (pml - low) >= min_pen and close > pml and vwap is not None and close > vwap:
            return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close,
                    "penetration": round(pml - low, 2),
                    "pm_range": round(pmh - pml, 2)}
    return None


def _collect_trades(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Replay Model A setups with live exit rules; return per-trade rows."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return pd.DataFrame()

    days = sorted(intraday.index.normalize().unique())
    rows = []
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
        pm_start = day_start.replace(hour=4, minute=0)
        pm_end = day_start.replace(hour=9, minute=29)
        pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
        pmh = float(pm_bars["high"].max()) if not pm_bars.empty else None
        pml = float(pm_bars["low"].min()) if not pm_bars.empty else None
        if pmh is None or pml is None:
            continue
        anchors = {"pmh": pmh, "pml": pml}

        vwap_series = _vwap_series(day_bars, day)
        setup = _detect_setup(day_bars, day, anchors, vwap_series,
                              float(SWEEP_MIN_PENETRATION))
        if setup is None:
            continue

        opt_prem = TICKER_PREMIUMS.get(symbol, DEFAULT_PREMIUM)
        sim = _simulate_exit(day_bars, setup["entry_ts"], setup["direction"],
                             setup["entry_price"], entry_premium=opt_prem)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["penetration"] = setup["penetration"]
            sim["pm_range"] = setup["pm_range"]
            sim["pm_mid"] = (pmh + pml) / 2.0
            rows.append(sim)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
    return df


def _stats(sub: pd.DataFrame) -> dict:
    if len(sub) == 0:
        return {"n": 0, "win_rate": None, "avg_pnl_pct": None, "pf": None}
    wins = sub[sub["pnl_pct"] > 0]
    losses = sub[sub["pnl_pct"] <= 0]
    gw = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gl = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    return {
        "n": len(sub),
        "win_rate": round(len(wins) / len(sub) * 100.0, 1),
        "avg_pnl_pct": round(float(sub["pnl_pct"].mean()), 2),
        "pf": round(gw / gl, 2) if gl > 0 else None,
    }


def _apply_filter(df: pd.DataFrame, name: str) -> pd.DataFrame:
    if name == "BASELINE_025":
        return df
    if name == "PEN1.00":
        return df[df["penetration"] >= 1.00]
    if name == "RANGE_PCT":
        pct_lo = df["pm_range"] / df["pm_mid"] * 100.0
        return df[(pct_lo >= 0.8) & (pct_lo <= 1.5)]
    if name == "RANGE_CEIL":
        return df[(df["penetration"] >= 1.00) & (df["pm_range"] <= 4.00)]
    if name == "RANGE_BAND":
        return df[(df["pm_range"] >= 2.00) & (df["pm_range"] <= 4.00)]
    raise ValueError(f"Unknown filter: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward conviction filter validation")
    parser.add_argument("--days", type=int, default=252)
    parser.add_argument("--folds", type=int, default=3,
                        help="Number of chronological folds (>=2).")
    args = parser.parse_args()

    client = AlpacaClient()
    df = _collect_trades(client, SYMBOL, args.days)
    if df.empty:
        logger.error("No trades collected.")
        return

    filters = ["BASELINE_025", "PEN1.00", "RANGE_PCT", "RANGE_CEIL", "RANGE_BAND"]

    # Full-sample stats per filter.
    print(f"\n=== {SYMBOL} Model A — {len(df)} trades over {args.days}d ===")
    print(f"{'Filter':<14} {'n':<5} {'Win%':<7} {'AvgPnL%':<9} {'PF':<7}")
    full = {}
    for f in filters:
        sub = _apply_filter(df, f)
        s = _stats(sub)
        full[f] = s
        pf = "inf" if s["pf"] is None else s["pf"]
        print(f"{f:<14} {s['n']:<5} {str(s['win_rate']):<7} {str(s['avg_pnl_pct']):<9} {pf:<7}")

    # Walk-forward: split chronologically into folds, evaluate each filter per fold.
    n = len(df)
    fold_size = n // args.folds
    print(f"\n=== Walk-forward ({args.folds} chronological folds) ===")
    print(f"{'Filter':<14} " + " ".join(f"Fold{i+1}(n,PF)" for i in range(args.folds)))
    for f in filters:
        sub = _apply_filter(df, f)
        cells = []
        for i in range(args.folds):
            lo = i * fold_size
            hi = n if i == args.folds - 1 else (i + 1) * fold_size
            fold = sub[(sub["date"] >= df["date"].iloc[lo]) & (sub["date"] <= df["date"].iloc[hi - 1])]
            s = _stats(fold)
            pf = "inf" if s["pf"] is None else s["pf"]
            cells.append(f"({s['n']},{pf})")
        print(f"{f:<14} " + " ".join(cells))

    # Stability: does the filter's PF beat baseline in a majority of folds?
    print("\n=== Fold stability (filter PF > baseline PF per fold) ===")
    base = _apply_filter(df, "BASELINE_025")
    for f in filters[1:]:
        sub = _apply_filter(df, f)
        beats = 0
        for i in range(args.folds):
            lo = i * fold_size
            hi = n if i == args.folds - 1 else (i + 1) * fold_size
            bf = _stats(base[(base["date"] >= df["date"].iloc[lo]) & (base["date"] <= df["date"].iloc[hi - 1])])
            sf = _stats(sub[(sub["date"] >= df["date"].iloc[lo]) & (sub["date"] <= df["date"].iloc[hi - 1])])
            if sf["pf"] is not None and bf["pf"] is not None and sf["pf"] > bf["pf"]:
                beats += 1
        print(f"{f:<14} beats baseline in {beats}/{args.folds} folds")

    out_path = os.path.join(PROJECT_ROOT, "sideload", "forward_validate_conviction.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"full": full, "trades": df.to_dict("records")}, f, indent=2, default=str)
    logger.info(f"Wrote forward validation to {out_path}")


if __name__ == "__main__":
    main()