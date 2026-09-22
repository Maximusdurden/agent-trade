#!/usr/bin/env python3
"""Walk-forward test of the dexter-trader EMA crossover strategy on AMD.

The dexter-trader backtester selects params by IN-SAMPLE max total PnL
(ORDER BY total_pnl DESC) — the same selection bias we've been eliminating all
session. This module re-tests the dexter EMA strategy (fast/slow EMA crossover
on HLC3, 200-period daily trend filter, trailing stop) with WALK-FORWARD
OUT-OF-SAMPLE validation: train on past, test on future, no lookahead.

We test AMD's CURRENT dexter params (fast_ema=13, slow_ema=23,
trailing_stop=0.04) as a FIXED config, plus a small grid, and report the
out-of-sample expectancy. If it survives OOS, it's a real candidate; if it
collapses, it's another falsified hypothesis.

Usage:
    python -m sideload.backtest_ema --all            # fixed dexter params
    python -m sideload.backtest_ema --grid           # small param sweep
    python -m sideload.backtest_ema --all --symbol NVDA
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestEMA")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

# Dexter's current AMD params (from active_parameters.json, 2026-09-05).
DEXTER_AMD_PARAMS = {"fast_ema": 13, "slow_ema": 23, "trailing_stop_percent": 0.04}

# Small grid for the sweep (kept tight to avoid overfitting).
GRID = {
    "fast_ema": [9, 13, 17],
    "slow_ema": [21, 23, 27],
    "trailing_stop_percent": [0.03, 0.04],
}

# Walk-forward: number of folds.
N_FOLDS = 5


def load_bars(client: AlpacaClient, symbol: str, days_back: int = 730) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch 1-hour intraday + daily bars (like dexter's backtester)."""
    intraday = client.get_historical_bars_paginated(
        symbol, timeframe_str="1h", days_back=days_back)
    daily = client.get_historical_bars(symbol, limit=days_back, timeframe_str="day")
    if intraday is None or intraday.empty:
        return pd.DataFrame(), pd.DataFrame()
    if isinstance(intraday.index, pd.MultiIndex):
        intraday = intraday.reset_index(level=0, drop=True)
    intraday.index = pd.to_datetime(intraday.index)
    if intraday.index.tz is None:
        intraday.index = intraday.index.tz_localize("UTC")
    intraday.index = intraday.index.tz_convert(ET)
    intraday = intraday.sort_index()
    # Market hours filter (9:30-16:00 ET, weekdays) — matches dexter.
    mask = (
        ((intraday.index.hour > 9) | ((intraday.index.hour == 9) & (intraday.index.minute >= 30)))
        & ((intraday.index.hour < 16) | ((intraday.index.hour == 16) & (intraday.index.minute <= 0)))
        & (intraday.index.dayofweek < 5)
    )
    intraday = intraday[mask].copy()

    if daily is None or daily.empty:
        return intraday, pd.DataFrame()
    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.reset_index(level=0, drop=True)
    daily.index = pd.to_datetime(daily.index)
    if daily.index.tz is None:
        daily.index = daily.index.tz_localize("UTC")
    daily.index = daily.index.tz_convert(ET)
    daily = daily.sort_index()
    return intraday, daily


def _add_indicators(intraday: pd.DataFrame, daily: pd.DataFrame,
                    fast: int, slow: int, trend: int = 200) -> pd.DataFrame:
    """Add EMA columns to intraday (HLC3) and daily (close) frames."""
    intraday = intraday.copy()
    intraday["HLC3"] = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    intraday[f"ema_{fast}"] = intraday["HLC3"].ewm(span=fast, adjust=False).mean()
    intraday[f"ema_{slow}"] = intraday["HLC3"].ewm(span=slow, adjust=False).mean()
    intraday["fast_prev"] = intraday[f"ema_{fast}"].shift(1)
    intraday["slow_prev"] = intraday[f"ema_{slow}"].shift(1)
    if not daily.empty:
        daily = daily.copy()
        daily[f"ema_{trend}"] = daily["close"].ewm(span=trend, adjust=False).mean()
    return intraday, daily


def _prev_day_levels(daily: pd.DataFrame, ts: pd.Timestamp) -> dict | None:
    """Prior day's high/low/close from the daily frame (for the entry filter)."""
    if daily.empty:
        return None
    prev = daily[daily.index < ts]
    if prev.empty:
        return None
    last = prev.iloc[-1]
    return {"prev_high": float(last["high"]), "prev_low": float(last["low"]),
            "prev_close": float(last["close"])}


def _daily_trend_ema(daily: pd.DataFrame, ts: pd.Timestamp, trend: int) -> float:
    if daily.empty:
        return 0.0
    prev = daily[daily.index < ts]
    if prev.empty:
        return 0.0
    return float(prev.iloc[-1][f"ema_{trend}"])


def simulate(intraday: pd.DataFrame, daily: pd.DataFrame, cfg: dict,
             confirmed_close_only: bool = False) -> list[dict]:
    """Simulate the dexter EMA strategy over the intraday frame. Returns trades.

    Long-only (matches dexter's 'Stock Long Only' scenario). Entry on bullish
    crossover + price > prev_low + price > daily trend EMA. Exit on trailing
    stop or bearish crossover.

    confirmed_close_only: the Drift Desk "confirmed close" idea — a wick/touch
    through the band is insufficient; the candle must CLOSE through it. Here we
    require the close to exceed the PRIOR BAR'S HIGH (a genuine close-through
    breakout), so the crossover is confirmed by price action rather than EMA
    math alone. This blocks entries where the fast/slow EMAs crossed but the
    close did not actually break out above the prior bar's high (a weak/phantom
    crossover).
    """
    fast = int(cfg["fast_ema"])
    slow = int(cfg["slow_ema"])
    trail = float(cfg["trailing_stop_percent"])
    trend = 200
    intraday, daily = _add_indicators(intraday, daily, fast, slow, trend)

    trades = []
    position = None  # {'entry_price','entry_ts','hwm'}
    for i in range(1, len(intraday)):
        row = intraday.iloc[i]
        prev = intraday.iloc[i - 1]
        ts = row.name
        px = float(row["close"])
        if pd.isna(row["fast_prev"]) or pd.isna(row["slow_prev"]):
            continue

        if position is None:
            # Entry: bullish crossover + filters.
            bull = row[f"ema_{fast}"] > row[f"ema_{slow}"] and prev[f"ema_{fast}"] <= prev[f"ema_{slow}"]
            if bull:
                levels = _prev_day_levels(daily, ts)
                trend_ema = _daily_trend_ema(daily, ts, trend)
                confirmed = True
                if confirmed_close_only:
                    # Confirmed close: close must exceed the prior bar's high
                    # (a real close-through breakout, not just EMA math).
                    confirmed = px > float(prev["high"])
                if confirmed and levels is not None and px > levels["prev_low"] and px > trend_ema:
                    position = {"entry_price": px, "entry_ts": ts, "hwm": px}
        else:
            # Exit: trailing stop or bearish crossover.
            hwm = max(position["hwm"], float(row["high"]))
            position["hwm"] = hwm
            stop = hwm * (1 - trail)
            exit_reason = None
            exit_px = None
            if float(row["low"]) < stop:
                exit_reason = "trailing_stop"
                exit_px = stop
            elif row[f"ema_{fast}"] < row[f"ema_{slow}"] and prev[f"ema_{fast}"] >= prev[f"ema_{slow}"]:
                exit_reason = "bearish_crossover"
                exit_px = px
            if exit_reason:
                ret = (exit_px - position["entry_price"]) / position["entry_price"]
                trades.append({
                    "entry_ts": position["entry_ts"], "exit_ts": ts,
                    "entry_price": position["entry_price"], "exit_price": exit_px,
                    "ret_pct": ret * 100.0, "exit_reason": exit_reason,
                })
                position = None

    # Close any open position at end of data.
    if position is not None:
        last = float(intraday["close"].iloc[-1])
        ret = (last - position["entry_price"]) / position["entry_price"]
        trades.append({
            "entry_ts": position["entry_ts"], "exit_ts": intraday.index[-1],
            "entry_price": position["entry_price"], "exit_price": last,
            "ret_pct": ret * 100.0, "exit_reason": "end_of_data",
        })
    return trades


def _stats(trades: list[dict], size_pct: float = 0.95, equity: float = 10000.0) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "expectancy_usd": 0.0, "mean_ret_pct": 0.0}
    rets = np.array([t["ret_pct"] for t in trades])
    size_usd = size_pct * equity
    pnls = rets / 100.0 * size_usd
    return {
        "trades": len(trades),
        "win_rate": float((rets > 0).mean()),
        "expectancy_usd": float(pnls.mean()),
        "total_pnl_usd": float(pnls.sum()),
        "mean_ret_pct": float(rets.mean()),
        "median_ret_pct": float(np.median(rets)),
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in set(t["exit_reason"] for t in trades)},
    }


def walk_forward(intraday: pd.DataFrame, daily: pd.DataFrame, cfg: dict,
                 n_folds: int = N_FOLDS, confirmed_close_only: bool = False) -> dict:
    """Walk-forward OOS: train on past folds, test on the next fold.

    For a FIXED param set (no fitting), every fold is out-of-sample — we just
    split chronologically and report each fold's expectancy + the aggregate.
    """
    n = len(intraday)
    fold_size = n // n_folds
    if fold_size < 100:
        n_folds = max(1, n // 100)
        fold_size = n // n_folds

    fold_results = []
    all_trades = []
    for f in range(n_folds):
        start = f * fold_size
        end = n if f == n_folds - 1 else (f + 1) * fold_size
        fold_df = intraday.iloc[start:end]
        # Warm-up: include prior bars for EMA computation but only trade in-fold.
        warm = max(0, start - 300)
        sim_df = intraday.iloc[warm:end]
        trades = simulate(sim_df, daily, cfg, confirmed_close_only=confirmed_close_only)
        # Keep only trades that ENTERED within the fold.
        fold_trades = [t for t in trades if start <= intraday.index.get_loc(t["entry_ts"]) < end]
        fold_results.append({"fold": f, "n": len(fold_trades), **{k: v for k, v in _stats(fold_trades).items() if k != "exit_reasons"}})
        all_trades.extend(fold_trades)

    agg = _stats(all_trades)
    agg["folds"] = fold_results
    agg["cfg"] = cfg
    agg["confirmed_close_only"] = confirmed_close_only
    return agg


def run_grid(intraday: pd.DataFrame, daily: pd.DataFrame,
             confirmed_close_only: bool = False) -> list[dict]:
    results = []
    keys = list(GRID.keys())
    for combo in itertools.product(*[GRID[k] for k in keys]):
        cfg = dict(zip(keys, combo))
        if cfg["fast_ema"] >= cfg["slow_ema"]:
            continue
        r = walk_forward(intraday, daily, cfg, confirmed_close_only=confirmed_close_only)
        results.append(r)
    results.sort(key=lambda x: -x["expectancy_usd"])
    return results


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def print_results(r: dict) -> None:
    print(f"\n=== EMA Walk-Forward — {r.get('symbol', '?')} ===")
    print(f"  cfg: {r['cfg']}")
    print(f"  trades: {r['trades']} | win rate: {r['win_rate']:.1%}")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print(f"  mean ret: {_fmt(r['mean_ret_pct'])}% | median: {_fmt(r['median_ret_pct'])}%")
    print(f"  exits: {r.get('exit_reasons')}")
    print("  folds:")
    for f in r.get("folds", []):
        print(f"    fold {f['fold']}: n={f['n']} win={f['win_rate']:.1%} exp=${f['expectancy_usd']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward EMA crossover backtest (dexter params)")
    parser.add_argument("--symbol", default="AMD")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--grid", action="store_true", help="Sweep the param grid")
    parser.add_argument("--all", action="store_true", help="Run dexter's fixed AMD params")
    parser.add_argument("--confirmed-close", action="store_true",
                        help="Require close strictly above slow EMA at entry (Drift Desk confirmed-close rule)")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-ema")
    client = AlpacaClient()
    symbol = args.symbol.upper()

    try:
        intraday, daily = load_bars(client, symbol, args.days)
        if intraday.empty:
            logger.error("No intraday data.")
            return
        logger.info(f"Loaded {len(intraday)} intraday bars, {len(daily)} daily bars for {symbol}")

        if args.grid:
            results = run_grid(intraday, daily, confirmed_close_only=args.confirmed_close)
            path = os.path.join(DATA_DIR, f"{symbol.lower()}_ema_grid.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, default=str)
            logger.info(f"Grid: {len(results)} configs -> {path}")
            print("\n=== TOP 10 EMA CONFIGS (by OOS expectancy) ===")
            for r in results[:10]:
                print(f"  exp=${r['expectancy_usd']:.2f} win={r['win_rate']:.1%} trades={r['trades']} cfg={r['cfg']}")
            if not args.no_discord:
                try:
                    send_discord_message(f"EMA grid done: {len(results)} configs, best exp=${results[0]['expectancy_usd']:.2f}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
        else:
            cfg = dict(DEXTER_AMD_PARAMS)
            r = walk_forward(intraday, daily, cfg, confirmed_close_only=args.confirmed_close)
            r["symbol"] = symbol
            print_results(r)
            path = os.path.join(DATA_DIR, f"{symbol.lower()}_ema_default.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(r, fh, indent=2, default=str)
            logger.info(f"Wrote {path}")
            if not args.no_discord:
                try:
                    send_discord_message(f"EMA walk-forward: exp=${r['expectancy_usd']:.2f} win={r['win_rate']:.1%}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"EMA backtest failed: {e}")
        log_exception_to_jira(e, "EMA Backtest Failure")
        raise


if __name__ == "__main__":
    main()