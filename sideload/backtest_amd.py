#!/usr/bin/env python3
"""AMD deterministic grid-search backtest (grid-only, no ML model).

The off-hours learning engine for the sideload lane. It pulls AMD history from
Alpaca, computes the same technical indicators the live lane uses, and sweeps
ALL combos of ALL trading variables to find the edge. It is DETERMINISTIC and
rule-based (indicator thresholds -> buy/sell/hold) — the LLM brain is for live
execution, not backtesting.

Three passes:
  1. Coarse grid (2-3 values/var) -> promising regions.
  2. Fine grid around the coarse winners.
  3. Walk-forward out-of-sample validation (train past, test future) to kill
     overfitting. Only configs that hold up out-of-sample are shippable.

Variables swept (>= 5m floor, prefer longer intervals):
  interval, RSI entry max, RSI exit overbought, MACD filter, VWAP dead-zone,
  min edge sigma, ATR sizing baseline, max hold hours, trailing-stop giveback,
  time-of-day window, regime filter.

Usage:
    python -m sideload.backtest_amd --coarse     # coarse grid first
    python -m sideload.backtest_amd --fine       # fine grid around winners
    python -m sideload.backtest_amd --walkforward  # out-of-sample validation
    python -m sideload.backtest_amd --all        # run all three passes
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from datetime import datetime

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestAMD")

# ---------------------------------------------------------------------------
# Grid definitions
# ---------------------------------------------------------------------------
COARSE_GRID = {
    "interval": ["1d", "1h", "15min"],
    "rsi_entry_max": [40.0, 45.0, 50.0],
    "rsi_exit_overbought": [65.0, 70.0, 0.0],  # 0 = disabled
    "macd_filter": ["hist_gt_0", "off"],
    "vwap_dead_zone_sigma": [0.5, 1.0, 1.5],
    "min_edge_sigma": [0.3, 0.5, 0.7],
    "atr_sizing_baseline_pct": [1.5, 2.0, 2.5],
    "max_hold_hours": [24.0, 72.0, 0.0],  # 0 = disabled
    "trail_stop_giveback_pct": [0.0, 0.03, 0.05],  # 0 = disabled
    "time_of_day": ["all", "am", "pm"],
    "regime_filter": ["all", "trending", "ranging"],
}

# Fine grid: denser values around the coarse winners (filled in at runtime).
FINE_GRID = {
    "interval": ["1d", "1h"],
    "rsi_entry_max": [40.0, 42.0, 45.0, 47.0, 50.0],
    "rsi_exit_overbought": [60.0, 65.0, 68.0, 70.0, 0.0],
    "macd_filter": ["hist_gt_0", "off"],
    "vwap_dead_zone_sigma": [0.6, 0.8, 1.0, 1.2],
    "min_edge_sigma": [0.4, 0.5, 0.6],
    "atr_sizing_baseline_pct": [1.8, 2.0, 2.2],
    "max_hold_hours": [48.0, 72.0, 96.0, 0.0],
    "trail_stop_giveback_pct": [0.0, 0.02, 0.03, 0.04],
    "time_of_day": ["all", "am", "pm"],
    "regime_filter": ["all", "trending", "ranging"],
}


def _grid_size(grid: dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_amd_bars(client: AlpacaClient, interval: str, limit: int = 2000,
                  days_back: int = 730) -> pd.DataFrame:
    """Fetch AMD historical bars for an interval and compute indicators.

    For intraday intervals (1h/15min/5min/1min) a single Alpaca request is
    capped at ~2000 bars (only weeks-months of history). We paginate to pull up
    to ``days_back`` calendar days so the multi-fold walk-forward has enough
    bars for a meaningful OOS sample. Daily bars are fetched directly (the full
    ~6y history fits in one request).
    """
    dp = DataProvider(client)
    bar_hours = _bar_hours_from_interval(interval)
    if bar_hours >= 20.0:
        # Daily: single request covers full history.
        df = client.get_historical_bars(sl_cfg.SL_SYMBOL, limit=limit, timeframe_str=interval)
    else:
        # Intraday: paginate to get more history.
        df = client.get_historical_bars_paginated(
            sl_cfg.SL_SYMBOL, timeframe_str=interval, days_back=days_back)
    if df is None or df.empty:
        return pd.DataFrame()
    # Compute the same indicators the live lane uses.
    df = dp._add_technical_indicators(df)
    return df


def _bar_hours_from_interval(interval: str) -> float:
    """Approximate hours per bar for an interval string (for daily detection)."""
    iv = (interval or "").strip().lower()
    if iv in ("1d", "1day", "day"):
        return 24.0
    if iv in ("1h", "1hour", "hour"):
        return 1.0
    if iv in ("15min", "15m"):
        return 0.25
    if iv in ("5min", "5m"):
        return 5.0 / 60.0
    if iv in ("1min", "1m", "minute"):
        return 1.0 / 60.0
    return 0.25


# ---------------------------------------------------------------------------
# Deterministic rule engine
# ---------------------------------------------------------------------------
def _hour_of_day(ts) -> int:
    try:
        return int(pd.Timestamp(ts).hour)
    except Exception:
        return 12


def _regime_of(df: pd.DataFrame, i: int) -> str:
    """Lightweight regime classifier for a row (trending/ranging)."""
    if i < 20:
        return "ranging"
    try:
        sma20 = df["sma_20"].iloc[i]
        sma20_prev = df["sma_20"].iloc[i - 5]
        if pd.isna(sma20) or pd.isna(sma20_prev) or sma20_prev == 0:
            return "ranging"
        slope_pct = (sma20 - sma20_prev) / sma20_prev * 100.0
        if slope_pct >= 0.1:
            return "trending"
        if slope_pct <= -0.1:
            return "trending"
        return "ranging"
    except Exception:
        return "ranging"


def simulate_config(df: pd.DataFrame, cfg: dict, equity: float = 10000.0) -> dict:
    """Simulate one config over the indicator frame. Returns stats dict.

    Deterministic and VECTORIZED (numpy): computes boolean entry/exit signal
    arrays once, then finds round-trips via numpy argwhere. This is ~100-1000x
    faster than a per-bar Python loop, which is essential for the large grid.

    Direction (P2): ``cfg["direction"]`` selects the trade side:
      - "long"  (default): buy RSI pullback, exit on trailing stop / max-hold.
      - "short": sell/put on RSI overbought (or price >= 1 ATR above VWAP),
                 exit when price fades back toward entry (trailing stop on the
                 short side) / max-hold.
      - "both":  run long and short independently and combine round-trips.
    """
    if df is None or len(df) < 60:
        return {"trades": 0, "pnl": 0.0, "win_rate": 0.0, "expectancy": 0.0}

    max_alloc = sl_cfg.SL_MAX_ALLOCATION_PCT * equity
    baseline_atr = float(cfg.get("atr_sizing_baseline_pct", 2.0))
    rsi_entry_max = float(cfg.get("rsi_entry_max", 45.0))
    rsi_exit = float(cfg.get("rsi_exit_overbought", 0.0))
    macd_filter = cfg.get("macd_filter", "off")
    vwap_sigma = float(cfg.get("vwap_dead_zone_sigma", 1.0))
    min_edge = float(cfg.get("min_edge_sigma", 0.5))
    max_hold = float(cfg.get("max_hold_hours", 0.0))
    trail_giveback = float(cfg.get("trail_stop_giveback_pct", 0.0))
    time_of_day = cfg.get("time_of_day", "all")
    regime_filter = cfg.get("regime_filter", "all")
    direction = cfg.get("direction", "long")
    # Short-side entry gate (P2): RSI at/above this = overbought fade (bearish).
    rsi_short_entry_min = float(cfg.get("rsi_short_entry_min", 60.0))

    # --- Vectorized signal computation (numpy arrays) ---
    price = df["close"].to_numpy(dtype=float)
    n = len(price)
    rsi = df["rsi_14"].to_numpy(dtype=float)
    atr = df["atr_14"].to_numpy(dtype=float)
    atr_pct = df["atr_pct"].to_numpy(dtype=float)
    vwap = df["vwap"].to_numpy(dtype=float)
    macd_hist = df["macd_hist"].to_numpy(dtype=float)

    # Replace NaN with safe defaults.
    rsi = np.where(np.isnan(rsi), 50.0, rsi)
    atr = np.where(np.isnan(atr), 0.0, atr)
    atr_pct = np.where(np.isnan(atr_pct), 0.0, atr_pct)
    vwap = np.where(np.isnan(vwap), 0.0, vwap)
    macd_hist = np.where(np.isnan(macd_hist), 0.0, macd_hist)

    # Time-of-day filter (vectorized).
    hod = np.array([_hour_of_day(ts) for ts in df.index], dtype=int)
    tod_mask = np.ones(n, dtype=bool)
    if time_of_day == "am":
        tod_mask = (hod >= 6) & (hod < 12)
    elif time_of_day == "pm":
        tod_mask = (hod >= 12) & (hod < 18)

    # Regime filter (vectorized via SMA-20 slope).
    regime_mask = np.ones(n, dtype=bool)
    if regime_filter != "all":
        sma20 = df["sma_20"].to_numpy(dtype=float)
        sma20_prev = np.roll(sma20, 5)
        sma20_prev[:5] = np.nan
        slope_pct = np.where(
            (sma20_prev > 0) & ~np.isnan(sma20_prev),
            (sma20 - sma20_prev) / sma20_prev * 100.0,
            0.0,
        )
        trending = np.abs(slope_pct) >= 0.1
        if regime_filter == "trending":
            regime_mask = trending
        else:  # ranging
            regime_mask = ~trending

    # VWAP dead zone + min edge (vectorized).
    #
    # NOTE (2026-09-19 audit): the VWAP dead-zone gate is only meaningful for
    # INTRADAY bars. On DAILY bars (1 bar/day) the session-based VWAP collapses
    # to that single day's typical price, so |price - vwap| ~ 0 and the gate
    # blocks ~97% of bars — an artifact, not a signal. Detect daily bars and
    # disable the dead-zone gate (keep the min-edge gate, which is still a
    # volatility-normalized distance check).
    bar_hours = _bar_hours(df)
    is_daily = bar_hours >= 20.0  # ~24h bars
    valid_vwap = (vwap > 0) & (atr > 0)
    edge_sigma = np.where(valid_vwap, np.abs(price - vwap) / np.where(atr > 0, atr, 1.0), np.nan)
    if is_daily:
        # VWAP is degenerate on daily bars — do not veto entries on it.
        in_dead_zone = np.zeros(n, dtype=bool)
    else:
        in_dead_zone = valid_vwap & (np.abs(price - vwap) <= vwap_sigma * atr)
    has_edge = np.isnan(edge_sigma) | (edge_sigma >= min_edge)

    # Entry signals (vectorized).
    # LONG: RSI pullback + outside dead zone + has edge + MACD + filters.
    long_entry_sig = (rsi <= rsi_entry_max) & (~in_dead_zone) & has_edge & tod_mask & regime_mask
    if macd_filter == "hist_gt_0":
        long_entry_sig = long_entry_sig & (macd_hist > 0)
    # SHORT (P2): RSI overbought (>= rsi_short_entry_min) OR price >= 1 ATR above
    # VWAP (extended above VWAP), outside dead zone, has edge, filters.
    short_entry_sig = (rsi >= rsi_short_entry_min) & (~in_dead_zone) & has_edge & tod_mask & regime_mask
    if macd_filter == "hist_gt_0":
        short_entry_sig = short_entry_sig & (macd_hist > 0)

    # Exit signal: RSI overbought (if enabled) — for LONG exits.
    exit_sig = np.zeros(n, dtype=bool)
    if rsi_exit > 0:
        exit_sig = rsi >= rsi_exit

    max_hold_bars = int(max_hold / bar_hours) if (max_hold > 0 and bar_hours > 0) else 0

    # --- Walk entries/exits to form round-trips (still a loop, but only over
    #     entry candidates, which is far fewer than all bars). ---
    #
    # IMPORTANT (look-ahead-bias fix): the loop index ``i`` MUST be advanced to
    # the entry bar ``e`` before opening a position. The previous implementation
    # opened the position at the current ``i`` while recording ``entry_i = e``
    # (a FUTURE bar), so the trailing-stop/max-hold exits measured PnL against
    # prices BEFORE the entry (negative hold durations) — i.e. it peeked at
    # future data and inflated the backtest. We use an explicit ``while`` loop
    # so we can jump ``i`` to the entry bar.
    trades = []
    # Run long and short independently (for "both", combine).
    if direction in ("long", "both"):
        trades += _walk_direction(
            price, rsi, atr_pct, exit_sig, long_entry_sig, n,
            max_alloc, baseline_atr, max_hold_bars, trail_giveback, "long")
    if direction in ("short", "both"):
        trades += _walk_direction(
            price, rsi, atr_pct, exit_sig, short_entry_sig, n,
            max_alloc, baseline_atr, max_hold_bars, trail_giveback, "short")

    if not trades:
        return {"trades": 0, "pnl": 0.0, "win_rate": 0.0, "expectancy": 0.0}

    pnls = [t["pnl"] for t in trades]
    total_pnl = float(sum(pnls))
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls)
    return {
        "trades": len(trades),
        "pnl": total_pnl,
        "win_rate": win_rate,
        "expectancy": total_pnl / len(trades),
        "long_trades": sum(1 for t in trades if t["direction"] == "long"),
        "short_trades": sum(1 for t in trades if t["direction"] == "short"),
    }


def _walk_direction(price, rsi, atr_pct, exit_sig, entry_sig, n,
                    max_alloc, baseline_atr, max_hold_bars, trail_giveback,
                    direction: str) -> list[dict]:
    """Walk one direction (long or short) to form round-trips.

    Long:  buy at entry, profit when price rises (exit on trailing stop / max-hold).
    Short: sell/put at entry, profit when price FALLS (exit when price fades back
           toward entry — trailing stop on the short side / max-hold).
    """
    entry_idx = np.flatnonzero(entry_sig)
    trades = []
    ei = 0
    position = None
    i = 20
    while i < n:
        if position is None:
            while ei < len(entry_idx) and entry_idx[ei] < i:
                ei += 1
            if ei >= len(entry_idx):
                break
            e = entry_idx[ei]
            p = price[e]
            if p <= 0 or np.isnan(p):
                ei += 1
                continue
            i = e
            size_pct = max_alloc
            ap = atr_pct[e]
            if ap > 0 and baseline_atr > 0:
                size_pct = max_alloc * min(1.0, baseline_atr / ap)
            qty = max(1.0, size_pct / p)
            position = {"entry_price": p, "qty": qty, "entry_i": e, "extreme": p}
            ei += 1
            i += 1
        else:
            exit_reason = None
            if max_hold_bars > 0 and (i - position["entry_i"]) >= max_hold_bars:
                exit_reason = "max_hold"
            if trail_giveback > 0:
                if direction == "long":
                    # Long: trailing stop on the upside (price falls back from peak).
                    extreme = max(position["extreme"], price[i])
                    position["extreme"] = extreme
                    gain = (extreme - position["entry_price"]) / position["entry_price"]
                    giveback = (extreme - price[i]) / extreme if extreme > 0 else 0.0
                    if gain >= 0.02 and giveback >= trail_giveback:
                        exit_reason = "trailing_stop"
                else:
                    # Short: trailing stop on the downside (price rises back from trough).
                    extreme = min(position["extreme"], price[i])
                    position["extreme"] = extreme
                    gain = (position["entry_price"] - extreme) / position["entry_price"]
                    giveback = (price[i] - extreme) / extreme if extreme > 0 else 0.0
                    if gain >= 0.02 and giveback >= trail_giveback:
                        exit_reason = "trailing_stop"
            if exit_reason:
                if direction == "long":
                    pnl = (price[i] - position["entry_price"]) * position["qty"]
                else:
                    pnl = (position["entry_price"] - price[i]) * position["qty"]
                trades.append({
                    "pnl": pnl, "entry_i": position["entry_i"], "exit_i": i,
                    "direction": direction,
                })
                position = None
            i += 1
    return trades


def _bar_hours(df: pd.DataFrame) -> float:
    """Estimate hours per bar from the index (fallback 0.25).

    Uses the MODE of consecutive-bar deltas rather than the last two bars,
    because the last two bars often span a market-close gap (e.g. 20:00 ->
    next 09:30), which would overstate the bar length for intraday intervals.
    """
    try:
        if len(df.index) >= 2:
            idx = df.index
            if isinstance(idx, pd.MultiIndex):
                idx = idx.get_level_values(1)
            deltas = pd.Series(idx).diff().dropna()
            if len(deltas) > 0:
                # Mode of deltas (most common bar spacing).
                mode_delta = deltas.mode()
                if len(mode_delta) > 0:
                    return max(0.05, float(mode_delta.iloc[0].total_seconds()) / 3600.0)
    except Exception:
        pass
    return 0.25


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------
def _eval_combo(args):
    """Evaluate one combo (picklable for multiprocessing). Returns a result dict
    or None if the config produced too few trades."""
    combo, keys, df_by_interval = args
    cfg = dict(zip(keys, combo))
    interval = cfg["interval"]
    df = df_by_interval.get(interval)
    if df is None or df.empty:
        return None
    stats = simulate_config(df, cfg)
    if stats["trades"] < 5:
        return None
    return {**cfg, **stats, "interval": interval}


def run_grid(df_by_interval: dict, grid: dict, top_n: int = 20,
             workers: int | None = None) -> list[dict]:
    """Run the full cartesian grid over all intervals and return top configs.

    Parallelized across CPU cores via ProcessPoolExecutor. ``workers`` defaults
    to os.cpu_count().
    """
    import os as _os
    from concurrent.futures import ProcessPoolExecutor
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    logger.info(f"Grid size: {len(combos)} combos across {len(df_by_interval)} intervals")

    if workers is None:
        workers = max(1, _os.cpu_count() or 1)
    logger.info(f"Running grid with {workers} workers...")

    tasks = [(combo, keys, df_by_interval) for combo in combos]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_eval_combo, tasks, chunksize=64):
            if res is not None:
                results.append(res)

    results.sort(key=lambda r: r["expectancy"], reverse=True)
    return results[:top_n]


def walk_forward_validate(df_by_interval: dict, configs: list[dict],
                          train_frac: float = 0.7) -> list[dict]:
    """Validate top configs out-of-sample: train on past, test on future."""
    validated = []
    for cfg in configs:
        interval = cfg["interval"]
        df = df_by_interval.get(interval)
        if df is None or len(df) < 100:
            continue
        split = int(len(df) * train_frac)
        train_df = df.iloc[:split]
        test_df = df.iloc[split:]
        train_stats = simulate_config(train_df, cfg)
        test_stats = simulate_config(test_df, cfg)
        if test_stats["trades"] < 3:
            continue
        validated.append({
            **cfg,
            "train_trades": train_stats["trades"],
            "train_pnl": train_stats["pnl"],
            "train_win_rate": train_stats["win_rate"],
            "train_expectancy": train_stats["expectancy"],
            "test_trades": test_stats["trades"],
            "test_pnl": test_stats["pnl"],
            "test_win_rate": test_stats["win_rate"],
            "test_expectancy": test_stats["expectancy"],
        })
    # Rank by out-of-sample expectancy, requiring min win rate.
    validated = [v for v in validated
                 if v["test_win_rate"] >= sl_cfg.SL_MIN_WIN_RATE
                 and v["test_expectancy"] >= sl_cfg.SL_MIN_EXPECTANCY_USD]
    validated.sort(key=lambda v: v["test_expectancy"], reverse=True)
    return validated


def walk_forward_validate_multifold(df_by_interval: dict, configs: list[dict],
                                    n_folds: int = 5,
                                    min_oos_trades: int = 30) -> list[dict]:
    """Multi-fold walk-forward validation (P1: honest OOS sample).

    The single-split ``walk_forward_validate`` produces a razor-thin OOS sample
    (the 2026-09-19 audit found only 4 OOS trades — statistically meaningless).
    This version splits history into ``n_folds`` chronological segments and, for
    each fold, trains on all bars BEFORE the fold and tests on the fold itself.
    OOS trades are AGGREGATED across folds, so a config must produce at least
    ``min_oos_trades`` out-of-sample trades to be considered — a much more
    honest bar than 3-4.

    Returns configs enriched with per-fold and aggregate OOS stats, ranked by
    aggregate OOS expectancy (requiring the aggregate win rate and expectancy
    to clear the configured minimums).
    """
    validated = []
    for cfg in configs:
        interval = cfg["interval"]
        df = df_by_interval.get(interval)
        if df is None or len(df) < 100:
            continue
        n = len(df)
        # Chronological fold boundaries (expanding-window walk-forward).
        fold_edges = [int(n * (i + 1) / n_folds) for i in range(n_folds)]
        fold_stats = []
        agg_trades = 0
        agg_pnl = 0.0
        agg_wins = 0
        for fold_idx, end in enumerate(fold_edges):
            start = 0 if fold_idx == 0 else fold_edges[fold_idx - 1]
            train_df = df.iloc[:start] if start > 0 else df.iloc[:1]  # empty train -> skip
            test_df = df.iloc[start:end]
            if len(test_df) < 60:
                continue
            # Train on the past (if any), test on the fold.
            train_stats = simulate_config(train_df, cfg) if len(train_df) >= 60 else {"trades": 0}
            test_stats = simulate_config(test_df, cfg)
            fold_stats.append({
                "fold": fold_idx,
                "start": str(test_df.index[0]),
                "end": str(test_df.index[-1]),
                "train_trades": train_stats.get("trades", 0),
                "test_trades": test_stats["trades"],
                "test_pnl": test_stats["pnl"],
                "test_win_rate": test_stats["win_rate"],
                "test_expectancy": test_stats["expectancy"],
            })
            agg_trades += test_stats["trades"]
            agg_pnl += test_stats["pnl"]
            agg_wins += int(test_stats["trades"] * test_stats["win_rate"])
        if agg_trades < min_oos_trades:
            continue
        agg_win_rate = agg_wins / agg_trades if agg_trades else 0.0
        agg_expectancy = agg_pnl / agg_trades if agg_trades else 0.0
        validated.append({
            **cfg,
            "n_folds": len(fold_stats),
            "oos_trades": agg_trades,
            "oos_pnl": agg_pnl,
            "oos_win_rate": agg_win_rate,
            "oos_expectancy": agg_expectancy,
            "folds": fold_stats,
        })
    validated = [v for v in validated
                 if v["oos_win_rate"] >= sl_cfg.SL_MIN_WIN_RATE
                 and v["oos_expectancy"] >= sl_cfg.SL_MIN_EXPECTANCY_USD]
    validated.sort(key=lambda v: v["oos_expectancy"], reverse=True)
    return validated


def _load_all_intervals(client: AlpacaClient, intervals: list[str]) -> dict:
    df_by_interval = {}
    for interval in intervals:
        try:
            df = load_amd_bars(client, interval)
            logger.info(f"Loaded {len(df)} bars for interval={interval}")
            if len(df) >= 60:
                df_by_interval[interval] = df
        except Exception as e:
            logger.error(f"Failed to load bars for interval={interval}: {e}")
            log_exception_to_jira(e, "AMD Backtest Data Load Failure",
                                  {"interval": interval})
    return df_by_interval


def _notify_discord(pass_name: str, results: list[dict]) -> None:
    """Send a Discord summary of a backtest pass (best configs)."""
    try:
        if not results:
            send_discord_message(
                f"AMD backtest ({pass_name}) done: no configs met the trade threshold."
            )
            return
        best = results[0]
        lines = [
            f"AMD backtest ({pass_name}) done — {len(results)} configs.",
            f"Best: {best.get('interval')} bars, RSI entry<={best.get('rsi_entry_max')}, "
            f"expectancy ${best.get('expectancy', 0.0):.2f}/trade, "
            f"win {best.get('win_rate', 0.0):.0%}, {best.get('trades', 0)} trades.",
        ]
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD grid-search backtest")
    parser.add_argument("--coarse", action="store_true", help="Run coarse grid")
    parser.add_argument("--fine", action="store_true", help="Run fine grid")
    parser.add_argument("--walkforward", action="store_true", help="Walk-forward validate")
    parser.add_argument("--all", action="store_true", help="Run all passes")
    parser.add_argument("--limit", type=int, default=2000, help="Bars per interval")
    parser.add_argument("--folds", type=int, default=5,
                        help="Number of walk-forward folds (multi-fold OOS)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest")
    client = AlpacaClient()

    try:
        if args.all or args.coarse:
            logger.info("=== COARSE GRID ===")
            df_by_interval = _load_all_intervals(client, sl_cfg.SL_INTERVALS)
            coarse_winners = run_grid(df_by_interval, COARSE_GRID, top_n=sl_cfg.SL_TOP_N_CONFIGS)
            logger.info(f"Coarse winners: {len(coarse_winners)}")
            for w in coarse_winners:
                logger.info(f"  {w['interval']} rsi<={w['rsi_entry_max']} exp=${w['expectancy']:.2f} "
                            f"win={w['win_rate']:.0%} trades={w['trades']}")
            with open("sideload/backtest_coarse.json", "w") as f:
                json.dump(coarse_winners, f, indent=2, default=str)
            _notify_discord("coarse", coarse_winners)

        if args.all or args.fine:
            logger.info("=== FINE GRID ===")
            df_by_interval = _load_all_intervals(client, sl_cfg.SL_INTERVALS)
            fine_winners = run_grid(df_by_interval, FINE_GRID, top_n=sl_cfg.SL_TOP_N_CONFIGS)
            logger.info(f"Fine winners: {len(fine_winners)}")
            for w in fine_winners:
                logger.info(f"  {w['interval']} rsi<={w['rsi_entry_max']} exp=${w['expectancy']:.2f} "
                            f"win={w['win_rate']:.0%} trades={w['trades']}")
            with open("sideload/backtest_fine.json", "w") as f:
                json.dump(fine_winners, f, indent=2, default=str)
            _notify_discord("fine", fine_winners)

        if args.all or args.walkforward:
            logger.info("=== WALK-FORWARD VALIDATION ===")
            df_by_interval = _load_all_intervals(client, sl_cfg.SL_INTERVALS)
            # Use fine winners if present, else coarse.
            candidates = []
            try:
                with open("sideload/backtest_fine.json") as f:
                    candidates = json.load(f)
            except Exception:
                candidates = []
            if not candidates:
                try:
                    with open("sideload/backtest_coarse.json") as f:
                        candidates = json.load(f)
                except Exception:
                    candidates = []
            if not candidates:
                candidates = run_grid(df_by_interval, COARSE_GRID, top_n=sl_cfg.SL_TOP_N_CONFIGS)
            # P1: use multi-fold walk-forward for an honest OOS sample (>=30
            # aggregated OOS trades) instead of the single-split 3-4 trade sample.
            validated = walk_forward_validate_multifold(
                df_by_interval, candidates,
                n_folds=args.folds,
                min_oos_trades=sl_cfg.SL_MIN_OOS_TRADES,
            )
            logger.info(f"Multi-fold walk-forward shippable configs: {len(validated)}")
            for v in validated:
                logger.info(f"  {v['interval']} rsi<={v['rsi_entry_max']} "
                            f"oos_exp=${v['oos_expectancy']:.2f} oos_win={v['oos_win_rate']:.0%} "
                            f"oos_trades={v['oos_trades']} folds={v['n_folds']}")
            with open("sideload/backtest_validated.json", "w") as f:
                json.dump(validated, f, indent=2, default=str)
            _notify_discord("walk-forward", validated)
    except Exception as e:
        logger.critical(f"Backtest run failed: {e}")
        log_exception_to_jira(e, "AMD Backtest Run Failure")
        raise


if __name__ == "__main__":
    main()