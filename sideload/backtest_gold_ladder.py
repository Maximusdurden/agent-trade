#!/usr/bin/env python3
"""GLD bounded mean-reversion ladder backtest — leakage-free from day one.

Implements the spec's bounded mean-reversion ladder (modified grid) engine for
GLD (SPDR Gold Shares). Deploys capital only during macro uptrends on acute
oversold pullbacks, scaling into a maximum of 3 tranches before taking profit
on a 20-day SMA mean-reversion or exiting via a hard -7.5% stop-loss circuit
breaker.

CORE LEAKAGE-FREE BOUNDARY (per blueprint):
    [ Day t-1 Close ] --( Compute indicators: RSI14, BB20, SMA200, SMA20 )
            |
            v (Condition met at 4:00 PM close)
    [ Day t Open ]    --( Execution: market fill at open + gap slippage )
            |
            v (Ladder: T2/T3 on intraday low <= t1*(1-step); exits on close)
    [ Day t+N Close ] --( Exit: SMA20 cross, blended-basis TP, or hard stop )

All signal columns are computed with .shift(1) relative to the entry trade day,
so no lookahead: the setup at day t-1's close is known before day t's open.

Usage:
    python -m sideload.backtest_gold_ladder --single          # run spec config
    python -m sideload.backtest_gold_ladder --coarse          # coarse grid
    python -m sideload.backtest_gold_ladder --fine            # fine grid
    python -m sideload.backtest_gold_ladder --walkforward     # multi-fold OOS
    python -m sideload.backtest_gold_ladder --all             # all passes
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload_gld as slg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestGoldLadder")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

# Lookback: 8 years to cover both bear and bull regimes.
LOOKBACK_DAYS = 365 * 8

# --- Spec config (Phase 1b: Connors mean-reversion variant) ---
SPEC_CFG = {
    "trend_sma": slg.SLG_TREND_SMA,          # 200
    "rsi_period": slg.SLG_RSI_PERIOD,        # 2
    "rsi_entry_max": slg.SLG_RSI_ENTRY_MAX,  # 10
    "band_mode": slg.SLG_BAND_MODE,          # "stretch_atr"
    "boll_period": slg.SLG_BOLL_PERIOD,      # 20
    "boll_mult": slg.SLG_BOLL_MULT,          # 1.5
    "atr_period": slg.SLG_ATR_PERIOD,        # 14
    "stretch_atr_mult": slg.SLG_STRETCH_ATR_MULT,  # 1.5
    "base_notional": slg.SLG_BASE_NOTIONAL,  # 1000
    "t1_mult": slg.SLG_T1_MULT,              # 1.0
    "t2_step_pct": slg.SLG_T2_STEP_PCT,      # 0.02
    "t2_mult": slg.SLG_T2_MULT,              # 1.25
    "t3_step_pct": slg.SLG_T3_STEP_PCT,      # 0.04
    "t3_mult": slg.SLG_T3_MULT,              # 1.50
    "max_tranches": slg.SLG_MAX_TRANCHES,    # 3
    "tp_sma": slg.SLG_TP_SMA,                # 5
    "tp_blended_pct": slg.SLG_TP_BLENDED_PCT,  # 0.02
    "max_hold_days": slg.SLG_MAX_HOLD_DAYS,  # 10
    "stop_pct": slg.SLG_STOP_PCT,            # 0.06
    "slippage": 0.05,                        # $ per share at open
    "equity": 10000.0,
}

# --- Coarse grid (Phase 1b: RSI-2 thresholds x band modes x exits) ---
COARSE_GRID = {
    "rsi_entry_max": [10.0, 15.0, 17.0],
    "band_mode": ["none", "stretch_atr"],
    "stretch_atr_mult": [1.0, 1.5],
    "t2_step_pct": [0.015, 0.02, 0.025],
    "tp_sma": [5, 10],
    "tp_blended_pct": [0.02, 0.025],
    "stop_pct": [0.06],
}

# --- Fine grid: denser values around the coarse winners ---
FINE_GRID = {
    "rsi_entry_max": [15.0, 16.0, 17.0, 18.0],
    "band_mode": ["none", "stretch_atr"],
    "stretch_atr_mult": [1.0, 1.5],
    "t2_step_pct": [0.015, 0.02, 0.025],
    "tp_sma": [8, 10],
    "tp_blended_pct": [0.02, 0.025, 0.03],
    "stop_pct": [0.06],
}


def _grid_size(grid: dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_daily(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch daily OHLCV bars, return a frame with a tz-aware ET DatetimeIndex."""
    df = client.get_historical_bars(symbol, limit=days_back, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI over `period` days."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range over `period` days."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Add all signal columns. All are computed at the CURRENT bar's close.

    The caller shifts them by 1 before evaluating the entry, so the setup at
    day t-1's close is known before day t's open (no lookahead).
    """
    df = df.copy()
    df["rsi"] = _rsi(df["close"], int(cfg["rsi_period"]))
    # Bollinger lower band: SMA20 - mult * std20.
    boll_period = int(cfg.get("boll_period", SPEC_CFG["boll_period"]))
    boll_mult = float(cfg.get("boll_mult", SPEC_CFG["boll_mult"]))
    sma_b = df["close"].rolling(boll_period).mean()
    std_b = df["close"].rolling(boll_period).std()
    df["boll_lower"] = sma_b - boll_mult * std_b
    # SMA20 for the stretch-ATR band reference.
    df["sma20"] = df["close"].rolling(20).mean()
    # ATR for the stretch-ATR band.
    atr_period = int(cfg.get("atr_period", SPEC_CFG["atr_period"]))
    df["atr"] = _atr(df, atr_period)
    # Exit SMA (tp_sma) for take-profit mean-reversion.
    df["sma_exit"] = df["close"].rolling(int(cfg.get("tp_sma", SPEC_CFG["tp_sma"]))).mean()
    # Trend SMA200 for the macro regime gate.
    df["sma_trend"] = df["close"].rolling(int(cfg["trend_sma"])).mean()
    return df


def _signal_met(row: pd.Series, cfg: dict) -> bool:
    """Evaluate the Tranche-1 setup at day t-1's close (row is the t-1 bar).

    Macro long gate: Close > SMA200.
    Short-term stretch: RSI(2) < rsi_entry_max.
    Band decoupling (optional confirmation of the oversold stretch):
      "none"        : no band requirement (pure RSI-2).
      "stretch_atr" : Close < SMA(20) - (k * ATR(14)).
      "boll"        : Close < Lower_BB(20, mult).
    """
    macro = row["close"] > row["sma_trend"]
    oversold_rsi = row["rsi"] < float(cfg["rsi_entry_max"])
    band_mode = cfg.get("band_mode", "none")
    if band_mode == "stretch_atr":
        k = float(cfg.get("stretch_atr_mult", SPEC_CFG["stretch_atr_mult"]))
        band_ok = row["close"] < (row["sma20"] - k * row["atr"])
    elif band_mode == "boll":
        band_ok = row["close"] < row["boll_lower"]
    else:
        band_ok = True
    return bool(macro and oversold_rsi and band_ok)


def simulate(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """Simulate the bounded mean-reversion ladder strategy.

    Leakage-free: entry decision uses ONLY day t-1's close (shifted indicators).
    Entry fills at day t's OPEN + slippage (or same-day close for MOC mode).
    Tranche 2/3 trigger on intraday LOW <= t1_price * (1 - step). Exits on
    SMA(exit_sma) cross / blended-basis TP / hard stop / time stop.

    Execution modes (cfg):
      mode="ladder" (default): 3-tranche ladder (1.0x -> 1.25x -> 1.5x).
      mode="1shot"  : single fixed-notional entry, no tranche 2/3.
      fill_mode="open" (default): signal at t-1 close, fill at t open + slippage.
      fill_mode="moc" : signal at t-1 close, fill at t close (3:45 PM MOC proxy).

    Returns a list of trade dicts. Each trade is a full cluster (all tranches
    opened before exit), with per-tranche detail.
    """
    df = add_indicators(df, cfg)
    # Shift signal columns by 1 so the setup at t-1 is known before day t.
    sig_cols = ["close", "rsi", "boll_lower", "sma20", "atr", "sma_exit", "sma_trend"]
    sig = df[sig_cols].shift(1)

    base_notional = float(cfg["base_notional"])
    t1_mult = float(cfg.get("t1_mult", SPEC_CFG["t1_mult"]))
    t2_step = float(cfg.get("t2_step_pct", SPEC_CFG["t2_step_pct"]))
    t2_mult = float(cfg.get("t2_mult", SPEC_CFG["t2_mult"]))
    t3_step = float(cfg.get("t3_step_pct", SPEC_CFG["t3_step_pct"]))
    t3_mult = float(cfg.get("t3_mult", SPEC_CFG["t3_mult"]))
    max_tranches = int(cfg.get("max_tranches", SPEC_CFG["max_tranches"]))
    tp_blended = float(cfg.get("tp_blended_pct", SPEC_CFG["tp_blended_pct"]))
    max_hold_days = int(cfg.get("max_hold_days", SPEC_CFG["max_hold_days"]))
    stop_pct = float(cfg.get("stop_pct", SPEC_CFG["stop_pct"]))
    slippage = float(cfg.get("slippage", SPEC_CFG["slippage"]))
    mode = cfg.get("mode", "ladder")
    fill_mode = cfg.get("fill_mode", "open")

    trades = []
    i = 0
    n = len(df)
    while i < n:
        row = df.iloc[i]
        # Setup evaluated at PREVIOUS bar's close (shifted signal).
        if i >= 1 and _signal_met(sig.iloc[i], cfg):
            # Entry fill: t open + slippage (open) OR t close (MOC proxy).
            if fill_mode == "moc":
                t1_price = float(row["close"])
            else:
                t1_price = float(row["open"]) + slippage
            if t1_price <= 0:
                i += 1
                continue
            entry_ts = df.index[i]

            # Ladder thresholds.
            t2_thresh = t1_price * (1.0 - t2_step)
            t3_thresh = t1_price * (1.0 - t3_step)
            stop_price = t1_price * (1.0 - stop_pct)

            # Tranche fills (shares). T1 always fills at entry.
            tranches = [{
                "tranche_num": 1,
                "fill_price": t1_price,
                "notional": base_notional * t1_mult,
                "shares": base_notional * t1_mult / t1_price,
            }]
            total_shares = tranches[0]["shares"]
            total_notional = tranches[0]["notional"]

            # Walk forward to find tranche triggers + exit.
            exit_reason = None
            exit_px = None
            exit_ts = None
            for j in range(i + 1, n):
                bar = df.iloc[j]
                low = float(bar["low"])
                close = float(bar["close"])
                ts = df.index[j]

                # Ladder: add tranche 2/3 on intraday low touch (max 3).
                # 1-shot mode: no additional tranches.
                if mode == "ladder" and len(tranches) < max_tranches:
                    if len(tranches) == 1 and low <= t2_thresh:
                        mult = t2_mult
                        fill = min(low, t2_thresh)  # fill at threshold (limit-like)
                        tranches.append({
                            "tranche_num": 2, "fill_price": fill,
                            "notional": base_notional * mult,
                            "shares": base_notional * mult / fill,
                        })
                    elif len(tranches) == 2 and low <= t3_thresh:
                        mult = t3_mult
                        fill = min(low, t3_thresh)
                        tranches.append({
                            "tranche_num": 3, "fill_price": fill,
                            "notional": base_notional * mult,
                            "shares": base_notional * mult / fill,
                        })
                    # Recompute totals after any new tranche.
                    total_shares = sum(t["shares"] for t in tranches)
                    total_notional = sum(t["notional"] for t in tranches)

                blended_basis = total_notional / total_shares if total_shares > 0 else 0.0

                # Catastrophic stop: check intraday low first (worst case).
                if low <= stop_price:
                    exit_reason = "cat_stop"
                    exit_px = stop_price
                    exit_ts = ts
                    break
                # Take-profit: Close >= SMA(exit_sma) OR price >= blended_basis * (1+tp).
                sma_exit_now = float(sig.iloc[j]["sma_exit"]) if j < len(sig) else float(bar["close"])
                if close >= sma_exit_now or close >= blended_basis * (1.0 + tp_blended):
                    exit_reason = "sma_tp" if close >= sma_exit_now else "blended_tp"
                    exit_px = close
                    exit_ts = ts
                    break
                # Time stop: liquidate at close if held > max_hold_days.
                if max_hold_days > 0 and (ts - entry_ts).days > max_hold_days:
                    exit_reason = "time_stop"
                    exit_px = close
                    exit_ts = ts
                    break

            if exit_reason is None:
                # End of data: mark-to-market at last close.
                exit_reason = "open_end"
                exit_px = float(df.iloc[n - 1]["close"])
                exit_ts = df.index[n - 1]

            # PnL: (exit - blended_basis) * total_shares.
            pnl = (exit_px - blended_basis) * total_shares
            ret_pct = (exit_px - blended_basis) / blended_basis * 100.0 if blended_basis > 0 else 0.0
            trades.append({
                "entry_ts": str(entry_ts.date()), "exit_ts": str(exit_ts.date()),
                "t1_price": t1_price, "exit": exit_px,
                "blended_basis": blended_basis, "total_shares": total_shares,
                "n_tranches": len(tranches),
                "ret_pct": ret_pct, "pnl": pnl, "exit_reason": exit_reason,
                "hold_days": (exit_ts - entry_ts).days,
                "tranches": tranches,
            })
            i = j + 1  # no overlapping positions
        else:
            i += 1
    return trades


def _stats(trades: list[dict], cfg: dict) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "expectancy_usd": 0.0, "total_pnl_usd": 0.0}
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls > 0
    gross_win = pnls[wins].sum()
    gross_loss = pnls[~wins].sum()
    return {
        "trades": len(trades),
        "win_rate": float(wins.mean()),
        "expectancy_usd": float(pnls.mean()),
        "total_pnl_usd": float(pnls.sum()),
        "mean_ret_pct": float(np.mean([t["ret_pct"] for t in trades])),
        "median_ret_pct": float(np.median([t["ret_pct"] for t in trades])),
        "profit_factor": float(abs(gross_win / gross_loss)) if gross_loss != 0 else float("inf"),
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])),
        "avg_tranches": float(np.mean([t["n_tranches"] for t in trades])),
        "max_drawdown_pct": _max_drawdown(pnls, cfg["equity"]),
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r)
                         for r in set(t["exit_reason"] for t in trades)},
    }


def _max_drawdown(pnls: np.ndarray, equity: float) -> float:
    if len(pnls) == 0:
        return 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / equity
    return float(abs(dd.min()) * 100.0)


def run_backtest(df: pd.DataFrame, symbol: str, cfg: dict) -> dict:
    if df.empty:
        return {"error": "no data", "trades": 0}
    trades = simulate(df, cfg)
    stats = _stats(trades, cfg)
    stats.update({"symbol": symbol, "cfg": cfg,
                  "date_range": f"{df.index[0].date()} to {df.index[-1].date()}"})
    return stats


# ---------------------------------------------------------------------------
# Benchmark reporting (buy-and-hold)
# ---------------------------------------------------------------------------
def _equity_curve(trades: list[dict], cfg: dict, df: pd.DataFrame) -> pd.Series:
    """Daily equity curve from trade PnLs (applied on exit day)."""
    if not trades:
        return pd.Series(dtype=float)
    pnl_by_day = {}
    for t in trades:
        day = pd.Timestamp(t["exit_ts"]).tz_localize(None).normalize()
        pnl_by_day[day] = pnl_by_day.get(day, 0.0) + t["pnl"]
    days = df.index.tz_localize(None).normalize().unique()
    eq = np.full(len(days), cfg["equity"], dtype=float)
    for i, d in enumerate(days):
        if i > 0:
            eq[i] = eq[i - 1]
        if d in pnl_by_day:
            eq[i] += pnl_by_day[d]
    return pd.Series(eq, index=days)


def _annualized_return(eq: pd.Series, trading_days: int) -> float:
    if len(eq) < 2 or eq.iloc[0] <= 0 or eq.iloc[-1] <= 0:
        return 0.0
    total = eq.iloc[-1] / eq.iloc[0]
    years = trading_days / 252.0
    if years <= 0:
        return 0.0
    return (total ** (1.0 / years)) - 1.0


def _sharpe(eq: pd.Series) -> float:
    if len(eq) < 3:
        return 0.0
    rets = eq.pct_change().dropna()
    if rets.std() == 0:
        return 0.0
    return float(rets.mean() / rets.std() * np.sqrt(252))


def _max_dd_from_curve(eq: pd.Series) -> float:
    if len(eq) == 0:
        return 0.0
    peak = eq.cummax()
    dd = (eq - peak) / peak
    return float(abs(dd.min()) * 100.0)


def buy_hold_benchmark(df: pd.DataFrame, cfg: dict) -> dict:
    if df.empty:
        return {}
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    bh_return_pct = (last / first - 1.0) * 100.0
    bh_eq = df["close"] / first * cfg["equity"]
    bh_dd = _max_dd_from_curve(bh_eq)
    bh_ann = _annualized_return(bh_eq, len(df))
    bh_sharpe = _sharpe(bh_eq)
    bh_calmar = bh_ann / (bh_dd / 100.0) if bh_dd > 0 else 0.0
    return {
        "bh_return_pct": bh_return_pct,
        "bh_max_dd_pct": bh_dd,
        "bh_annualized_return_pct": bh_ann * 100.0,
        "bh_sharpe": bh_sharpe,
        "bh_calmar": bh_calmar,
    }


def benchmark_report(df: pd.DataFrame, trades: list[dict], cfg: dict) -> dict:
    bh = buy_hold_benchmark(df, cfg)
    eq = _equity_curve(trades, cfg, df)
    strat_ann = _annualized_return(eq, len(df))
    strat_dd = _max_dd_from_curve(eq)
    strat_sharpe = _sharpe(eq)
    strat_calmar = strat_ann / (strat_dd / 100.0) if strat_dd > 0 else 0.0
    total_hold = sum(t["hold_days"] for t in trades)
    exposure_pct = total_hold / len(df) * 100.0 if not df.empty else 0.0
    return {
        "strategy_annualized_return_pct": strat_ann * 100.0,
        "buy_hold_annualized_return_pct": bh["bh_annualized_return_pct"],
        "strategy_max_dd_pct": strat_dd,
        "buy_hold_max_dd_pct": bh["bh_max_dd_pct"],
        "exposure_pct": exposure_pct,
        "strategy_sharpe": strat_sharpe,
        "buy_hold_sharpe": bh["bh_sharpe"],
        "strategy_calmar": strat_calmar,
        "buy_hold_calmar": bh["bh_calmar"],
    }


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------
def _eval_combo(task: tuple) -> dict | None:
    """Evaluate one grid combo (top-level function for ProcessPoolExecutor)."""
    combo, keys, df = task
    cfg = dict(SPEC_CFG)
    for k, v in zip(keys, combo):
        cfg[k] = v
    try:
        trades = simulate(df, cfg)
        stats = _stats(trades, cfg)
        pnls = [t["pnl"] for t in trades]
        stats["sharpe"] = _sharpe_from_pnls(pnls)
        stats["score"] = _score_config(stats)
        stats.update(cfg)  # flatten knobs to top level for ranking/logging
        stats["cfg"] = cfg
        return stats
    except Exception as e:
        logger.warning(f"Combo {combo} failed: {e}")
        return None


def run_grid(df: pd.DataFrame, grid: dict, top_n: int = 20,
             workers: int | None = None) -> list[dict]:
    """Run the full cartesian grid and return top configs (parallelized).

    Ranks by ``score`` = Sharpe * sqrt(trade count), which rewards configs that
    combine a high risk-adjusted return with a statistically meaningful sample
    (per the Phase 1b directive), rather than raw expectancy.

    Configs passing the acceptance gates (>=60 trades, win>=70%, PF>=1.75) are
    ranked first; if any pass, only they are returned (up to ``top_n``).
    """
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    logger.info(f"Grid size: {len(combos)} combos")
    if workers is None:
        workers = max(1, os.cpu_count() or 1)
    logger.info(f"Running grid with {workers} workers...")
    tasks = [(combo, keys, df) for combo in combos]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_eval_combo, tasks, chunksize=64):
            if res is not None:
                results.append(res)
    # Acceptance gates first; fall back to all configs if none pass.
    gated = [r for r in results
             if r["trades"] >= 60 and r["win_rate"] >= 0.70
             and r["profit_factor"] >= 1.75]
    pool = gated if gated else results
    pool.sort(key=lambda r: r["score"], reverse=True)
    return pool[:top_n]


def walk_forward_validate_multifold(df: pd.DataFrame, configs: list[dict],
                                    n_folds: int = 5,
                                    min_oos_trades: int = 20) -> list[dict]:
    """Multi-fold walk-forward validation (honest OOS sample).

    Splits history into ``n_folds`` chronological segments and, for each fold,
    tests on the fold itself (indicators warm up on prior bars). OOS trades are
    AGGREGATED across folds, so a config must produce at least
    ``min_oos_trades`` out-of-sample trades to be considered.
    """
    validated = []
    for cfg in configs:
        n = len(df)
        if n < 100:
            continue
        fold_edges = [int(n * (i + 1) / n_folds) for i in range(n_folds)]
        fold_stats = []
        agg_trades = 0
        agg_pnl = 0.0
        agg_wins = 0
        for fold_idx, end in enumerate(fold_edges):
            start = 0 if fold_idx == 0 else fold_edges[fold_idx - 1]
            test_df = df.iloc[start:end]
            if len(test_df) < 60:
                continue
            test_stats = _stats(simulate(test_df, cfg), cfg)
            fold_stats.append({
                "fold": fold_idx,
                "start": str(test_df.index[0].date()),
                "end": str(test_df.index[-1].date()),
                "test_trades": test_stats["trades"],
                "test_pnl": test_stats["total_pnl_usd"],
                "test_win_rate": test_stats["win_rate"],
                "test_expectancy": test_stats["expectancy_usd"],
            })
            agg_trades += test_stats["trades"]
            agg_pnl += test_stats["total_pnl_usd"]
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
                 if v["oos_win_rate"] >= slg.SLG_MIN_WIN_RATE
                 and v["oos_expectancy"] >= slg.SLG_MIN_EXPECTANCY_USD]
    validated.sort(key=lambda v: v["oos_expectancy"], reverse=True)
    return validated


def _score_config(stats: dict) -> float:
    """Ranking score: Sharpe * sqrt(trade count).

    Rewards configs that combine a high risk-adjusted return with a large
    (statistically meaningful) sample. Falls back to expectancy if no equity
    curve is available.
    """
    trades = stats.get("trades", 0) or stats.get("oos_trades", 0)
    sharpe = stats.get("sharpe", 0.0)
    if sharpe <= 0:
        sharpe = stats.get("expectancy_usd", 0.0) / max(1.0, stats.get("max_drawdown_pct", 1.0))
    return float(sharpe * (trades ** 0.5))


def _sharpe_from_pnls(pnls: list[float]) -> float:
    """Annualized Sharpe from a list of per-trade PnLs (approx, per-trade basis)."""
    if len(pnls) < 3:
        return 0.0
    arr = np.array(pnls, dtype=float)
    if arr.std() == 0:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(252))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_benchmark(r: dict) -> None:
    b = r.get("benchmark", {})
    if not b:
        return
    print("  --- Benchmark (strategy vs buy-and-hold) ---")
    print(f"  annualized return:  {b['strategy_annualized_return_pct']:>7.1f}%  vs B&H {b['buy_hold_annualized_return_pct']:>7.1f}%")
    print(f"  max drawdown:       {b['strategy_max_dd_pct']:>7.1f}%  vs B&H {b['buy_hold_max_dd_pct']:>7.1f}%")
    print(f"  exposure:           {b['exposure_pct']:>7.1f}% of days in market")
    print(f"  Sharpe:             {b['strategy_sharpe']:>7.2f}  vs B&H {b['buy_hold_sharpe']:>7.2f}")
    print(f"  Calmar:             {b['strategy_calmar']:>7.2f}  vs B&H {b['buy_hold_calmar']:>7.2f}")


def print_results(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    print(f"\n=== GLD Ladder — {r['symbol']} ===")
    print(f"  range: {r['date_range']}")
    print(f"  trades: {r['trades']} | win rate: {r['win_rate']:.1%}")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print(f"  mean ret: {r['mean_ret_pct']:.3f}% | median: {r['median_ret_pct']:.3f}%")
    print(f"  profit factor: {r['profit_factor']:.2f} | avg hold: {r['avg_hold_days']:.1f}d | avg tranches: {r['avg_tranches']:.1f}")
    print(f"  max DD: {r['max_drawdown_pct']:.1f}%")
    print(f"  exits: {r['exit_reasons']}")


def print_walk_forward(validated: list[dict]) -> None:
    if not validated:
        print("\n=== Walk-Forward OOS — no shippable configs ===")
        return
    print(f"\n=== Walk-Forward OOS — {len(validated)} shippable configs ===")
    for v in validated:
        print(f"  rsi<={v['rsi_entry_max']} band={v['band_mode']} "
              f"stretch_k={v.get('stretch_atr_mult', '-')} "
              f"t2={v['t2_step_pct']} tp_sma={v['tp_sma']} "
              f"tp={v['tp_blended_pct']} stop={v['stop_pct']}")
        print(f"    oos_trades={v['oos_trades']} oos_win={v['oos_win_rate']:.1%} "
              f"oos_exp=${v['oos_expectancy']:.2f} folds={v['n_folds']}")


def _notify_discord(pass_name: str, results: list[dict]) -> None:
    try:
        if not results:
            send_discord_message(f"GLD ladder backtest ({pass_name}) done: no configs met the trade threshold.")
            return
        best = results[0]
        lines = [
            f"GLD ladder backtest ({pass_name}) done — {len(results)} configs.",
            f"Best: RSI entry<={best.get('rsi_entry_max')}, "
            f"expectancy ${best.get('expectancy_usd', 0.0):.2f}/trade, "
            f"win {best.get('win_rate', 0.0):.0%}, {best.get('trades', 0)} trades.",
        ]
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GLD bounded mean-reversion ladder backtest")
    parser.add_argument("--single", action="store_true", help="Run the spec config")
    parser.add_argument("--coarse", action="store_true", help="Run coarse grid")
    parser.add_argument("--fine", action="store_true", help="Run fine grid")
    parser.add_argument("--walkforward", action="store_true", help="Walk-forward validate")
    parser.add_argument("--top5", action="store_true", help="Top 5 configs by Sharpe*sqrt(trades)")
    parser.add_argument("--all", action="store_true", help="Run all passes")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--folds", type=int, default=5, help="Walk-forward folds")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-gold-ladder")
    client = AlpacaClient()
    symbol = slg.SLG_SYMBOL

    try:
        df = load_daily(client, symbol, args.days)
        if df.empty:
            logger.error("No daily data.")
            return
        logger.info(f"Loaded {len(df)} daily bars for {symbol} ({df.index[0].date()} to {df.index[-1].date()})")

        if args.single or (not args.coarse and not args.fine and not args.walkforward and not args.all):
            r = run_backtest(df, symbol, dict(SPEC_CFG))
            r["benchmark"] = benchmark_report(df, simulate(df, dict(SPEC_CFG)), dict(SPEC_CFG))
            print_results(r)
            print_benchmark(r)
            path = os.path.join(DATA_DIR, "gold_ladder_spec.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(r, fh, indent=2, default=str)
            logger.info(f"Wrote {path}")

        if args.all or args.coarse:
            logger.info("=== COARSE GRID ===")
            coarse_winners = run_grid(df, COARSE_GRID, top_n=slg.SLG_TOP_N_CONFIGS)
            logger.info(f"Coarse winners: {len(coarse_winners)}")
            for w in coarse_winners:
                logger.info(f"  rsi<={w['rsi_entry_max']} exp=${w['expectancy_usd']:.2f} "
                            f"win={w['win_rate']:.0%} trades={w['trades']}")
            with open(os.path.join(OUT_DIR, "backtest_gold_ladder_coarse.json"), "w") as f:
                json.dump(coarse_winners, f, indent=2, default=str)
            if not args.no_discord:
                _notify_discord("coarse", coarse_winners)

        if args.all or args.fine:
            logger.info("=== FINE GRID ===")
            fine_winners = run_grid(df, FINE_GRID, top_n=slg.SLG_TOP_N_CONFIGS)
            logger.info(f"Fine winners: {len(fine_winners)}")
            for w in fine_winners:
                logger.info(f"  rsi<={w['rsi_entry_max']} exp=${w['expectancy_usd']:.2f} "
                            f"win={w['win_rate']:.0%} trades={w['trades']}")
            with open(os.path.join(OUT_DIR, "backtest_gold_ladder_fine.json"), "w") as f:
                json.dump(fine_winners, f, indent=2, default=str)
            if not args.no_discord:
                _notify_discord("fine", fine_winners)

        if args.all or args.walkforward:
            logger.info("=== WALK-FORWARD VALIDATION ===")
            candidates = []
            try:
                with open(os.path.join(OUT_DIR, "backtest_gold_ladder_fine.json")) as f:
                    candidates = json.load(f)
            except Exception:
                candidates = []
            if not candidates:
                try:
                    with open(os.path.join(OUT_DIR, "backtest_gold_ladder_coarse.json")) as f:
                        candidates = json.load(f)
                except Exception:
                    candidates = []
            if not candidates:
                candidates = run_grid(df, COARSE_GRID, top_n=slg.SLG_TOP_N_CONFIGS)
            validated = walk_forward_validate_multifold(
                df, candidates, n_folds=args.folds, min_oos_trades=slg.SLG_MIN_OOS_TRADES)
            logger.info(f"Multi-fold walk-forward shippable configs: {len(validated)}")
            for v in validated:
                logger.info(f"  rsi<={v['rsi_entry_max']} oos_exp=${v['oos_expectancy']:.2f} "
                            f"oos_win={v['oos_win_rate']:.0%} oos_trades={v['oos_trades']} folds={v['n_folds']}")
            with open(os.path.join(OUT_DIR, "backtest_gold_ladder_validated.json"), "w") as f:
                json.dump(validated, f, indent=2, default=str)
            if not args.no_discord:
                _notify_discord("walk-forward", validated)

        if args.all or args.top5:
            logger.info("=== TOP 5 CONFIGS (Sharpe * sqrt(trades)) ===")
            # Merge coarse + fine candidates so high-trade-count band=none configs
            # are considered alongside the high-sharpe stretch_atr configs.
            candidates = []
            for fname in ("backtest_gold_ladder_fine.json", "backtest_gold_ladder_coarse.json"):
                try:
                    with open(os.path.join(OUT_DIR, fname)) as f:
                        candidates.extend(json.load(f))
                except Exception:
                    pass
            if not candidates:
                candidates = run_grid(df, COARSE_GRID, top_n=slg.SLG_TOP_N_CONFIGS)
            # Deduplicate by knob signature.
            seen = set()
            uniq = []
            for c in candidates:
                sig = (c.get("rsi_entry_max"), c.get("band_mode"), c.get("stretch_atr_mult"),
                       c.get("t2_step_pct"), c.get("tp_sma"), c.get("tp_blended_pct"),
                       c.get("stop_pct"))
                if sig not in seen:
                    seen.add(sig)
                    uniq.append(c)
            candidates = uniq
            # In-sample score for each candidate.
            scored = []
            for c in candidates:
                cfg = dict(SPEC_CFG)
                for k in ("rsi_entry_max", "band_mode", "stretch_atr_mult", "t2_step_pct",
                          "tp_sma", "tp_blended_pct", "stop_pct"):
                    if k in c:
                        cfg[k] = c[k]
                trades = simulate(df, cfg)
                stats = _stats(trades, cfg)
                pnls = [t["pnl"] for t in trades]
                stats["sharpe"] = _sharpe_from_pnls(pnls)
                stats["score"] = _score_config(stats)
                stats.update(cfg)
                scored.append(stats)
            # Apply acceptance gates: >=60 trades, win>=70%, PF>=1.75.
            gated = [s for s in scored
                     if s["trades"] >= 60 and s["win_rate"] >= 0.70
                     and s["profit_factor"] >= 1.75]
            pool = gated if gated else scored
            pool.sort(key=lambda s: s["score"], reverse=True)
            top5 = pool[:5]
            print(f"  (configs passing gates: {len(gated)}; showing top 5 by score)")
            for i, s in enumerate(top5, 1):
                print(f"  #{i} rsi<={s['rsi_entry_max']} band={s['band_mode']} "
                      f"stretch_k={s.get('stretch_atr_mult', '-')} t2={s['t2_step_pct']} "
                      f"tp_sma={s['tp_sma']} tp={s['tp_blended_pct']} stop={s['stop_pct']}")
                print(f"      trades={s['trades']} win={s['win_rate']:.1%} "
                      f"exp=${s['expectancy_usd']:.2f} PF={s['profit_factor']:.2f} "
                      f"sharpe={s['sharpe']:.2f} score={s['score']:.2f}")
            with open(os.path.join(OUT_DIR, "backtest_gold_ladder_top5.json"), "w") as f:
                json.dump(top5, f, indent=2, default=str)
            if not args.no_discord:
                _notify_discord("top5", top5)
    except Exception as e:
        logger.critical(f"GLD ladder backtest failed: {e}")
        log_exception_to_jira(e, "GLD Ladder Backtest Failure")
        raise


if __name__ == "__main__":
    main()