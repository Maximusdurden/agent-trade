#!/usr/bin/env python3
"""Multi-day swing mean-reversion backtest — leakage-free from day one.

Pivots away from intraday breakouts (killed: confirmed-close fill friction +
wider stops consumed the edge). At a 2-10 day holding period, slippage shrinks
to <1% of the target move and intraday bar-construction boundaries vanish.

CORE LEAKAGE-FREE BOUNDARY (per blueprint):
    [ Day t-1 Close ] --( Compute indicators: RSI, Bollinger, Stretch )
            |
            v (Condition met at 4:00 PM close)
    [ Day t Open ]    --( Execution: market fill at open + gap slippage )
            |
            v (Holding 2-5 trading days OR mean-reversion target)
    [ Day t+N Close ] --( Exit: SMA20 cross, time-stop, or trailing stop )

All signal columns are computed with .shift(1) relative to the entry trade day,
so no lookahead: the setup at day t-1's close is known before day t's open.

Usage:
    python -m sideload.backtest_swing_mean_reversion --all --symbol AMD
    python -m sideload.backtest_swing_mean_reversion --all --symbol QQQ
    python -m sideload.backtest_swing_mean_reversion --all --symbol SMH
    python -m sideload.backtest_swing_mean_reversion --baseline --symbol AMD
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestSwingMR")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

# Lookback: 5-10 years to cover both bear and bull regimes.
LOOKBACK_DAYS = 365 * 8

# --- Full mean-reversion config (blueprint) ---
DEFAULT_CFG = {
    # Trend filter (macro long gate): Close_{t-1} > SMA200_{t-1}
    "trend_sma": 200,
    # Stretch / oversold conditions (all at t-1 close)
    "rsi_period": 2,          # short-period RSI (RSI_2)
    "rsi_buy_below": 10.0,    # RSI_2 < 10
    "boll_period": 20,        # Bollinger band period
    "boll_mult": 2.5,         # lower band = SMA20 - 2.5*std20
    "atr_period": 14,         # ATR for stretch + catastrophic stop
    "stretch_atr_mult": 2.0,  # Close < SMA20 - 2.0*ATR14
    # Execution
    "slippage": 0.05,         # $ per share at open (opening auction spread)
    # Exit rules
    "profit_sma": 5,          # exit on first touch of SMA5 (or SMA20 return)
    "max_hold_days": 5,       # time stop
    "catastrophic_atr_mult": 2.0,  # hard stop at entry - 2.0*ATR14
    # Sizing
    "size_pct": 0.10,
    "equity": 10000.0,
}

# --- Connors RSI-2 baseline (simpler, per blueprint step 2) ---
BASELINE_CFG = {
    "trend_sma": 200,
    "rsi_period": 2,
    "rsi_buy_below": 10.0,
    "profit_sma": 5,          # exit on Close > SMA5
    "max_hold_days": 5,
    "slippage": 0.05,
    "size_pct": 0.10,
    "equity": 10000.0,
}


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


def load_earnings_dates(symbol: str, days_back: int) -> set:
    """Fetch HISTORICAL earnings dates for a symbol via yfinance.

    Returns a set of naive date objects. Fails open (empty set) if yfinance is
    unavailable, so the blackout filter degrades gracefully.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; earnings blackout disabled (fail-open).")
        return set()
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=max(40, days_back // 90))
        if df is None or df.empty:
            return set()
        dates = set()
        for idx in df.index:
            d = pd.Timestamp(idx)
            dates.add(d.tz_localize(None).date() if d.tzinfo is not None else d.date())
        return dates
    except Exception as e:
        logger.warning(f"Could not fetch earnings for {symbol}: {e} (fail-open).")
        return set()


def _near_earnings(entry_date, earnings_dates: set, window_days: int = 2) -> bool:
    """True if entry_date is within window_days of any earnings date."""
    if not earnings_dates:
        return False
    from datetime import timedelta
    ed = pd.Timestamp(entry_date).date()
    for e in earnings_dates:
        if abs((ed - e).days) <= window_days:
            return True
    return False


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
    # Bollinger lower band: SMA20 - mult * std20
    boll_period = int(cfg.get("boll_period", DEFAULT_CFG["boll_period"]))
    boll_mult = float(cfg.get("boll_mult", DEFAULT_CFG["boll_mult"]))
    sma_b = df["close"].rolling(boll_period).mean()
    std_b = df["close"].rolling(boll_period).std()
    df["boll_lower"] = sma_b - boll_mult * std_b
    # SMA20 for stretch distance
    df["sma20"] = df["close"].rolling(20).mean()
    # ATR14
    atr_period = int(cfg.get("atr_period", DEFAULT_CFG["atr_period"]))
    df["atr"] = _atr(df, atr_period)
    # Trend SMA200
    df["sma_trend"] = df["close"].rolling(int(cfg["trend_sma"])).mean()
    # SMA50 for intermediate health gate (Option C: dual trend gate)
    df["sma50"] = df["close"].rolling(50).mean()
    # SMA200 slope (Option B): sma_trend(t-1) >= sma_trend(t-5)
    df["sma_trend_5ago"] = df["sma_trend"].shift(4)
    # Profit SMA5
    df["sma5"] = df["close"].rolling(int(cfg["profit_sma"])).mean()
    return df


def _signal_met(row: pd.Series, cfg: dict, baseline: bool) -> bool:
    """Evaluate the setup at day t-1's close (row is the t-1 bar).

    gate_mode (in cfg) adds an intermediate health gate on top of the macro
    SMA200 gate:
      None            : macro gate only (Close > SMA200)
      'dual_sma50'    : ALSO require Close > SMA50 (Option C)
      'sma200_slope'  : ALSO require SMA200(t-1) >= SMA200(t-5) (Option B)
      'sector'        : ALSO require sector (QQQ/SMH) > its SMA200 (Option A)
    """
    gate_mode = cfg.get("gate_mode")
    if baseline:
        # Connors RSI-2: RSI_2 < 10 AND Close > SMA200
        macro = row["close"] > row["sma_trend"]
        if not macro:
            return False
        if gate_mode == "dual_sma50":
            if not (row["close"] > row["sma50"]):
                return False
        elif gate_mode == "sma200_slope":
            if not (row["sma_trend"] >= row["sma_trend_5ago"]):
                return False
        elif gate_mode == "sector":
            if not (row["sector_close"] > row["sector_sma_trend"]):
                return False
        return bool(row["rsi"] < cfg["rsi_buy_below"])
    # Full mean-reversion setup:
    #   Macro long gate: Close > SMA200
    #   Oversold: RSI_2 < 10 AND Close < Bollinger lower AND Close < SMA20 - 2*ATR
    macro = row["close"] > row["sma_trend"]
    oversold_rsi = row["rsi"] < cfg["rsi_buy_below"]
    oversold_boll = row["close"] < row["boll_lower"]
    stretch = row["close"] < (row["sma20"] - float(cfg["stretch_atr_mult"]) * row["atr"])
    return bool(macro and oversold_rsi and oversold_boll and stretch)


def simulate(df: pd.DataFrame, cfg: dict, baseline: bool = False,
             earnings_dates: set | None = None,
             earnings_window: int = 2) -> list[dict]:
    """Simulate the multi-day swing mean-reversion strategy.

    Leakage-free: entry decision uses ONLY day t-1's close (shifted indicators).
    Entry fills at day t's OPEN + slippage. Exit on SMA5 touch, time-stop, or
    catastrophic stop.

    earnings_dates: optional set of earnings dates. If provided, entries within
    ``earnings_window`` days of an earnings date are BLOCKED (earnings blackout
    — avoids overnight negative earnings gaps that gap through the stop).
    """
    df = add_indicators(df, cfg)
    # Shift signal columns by 1 so the setup at t-1 is known before day t.
    # Include 'close' (shifted = prior day's close) for the macro gate.
    sig_cols = ["close", "rsi", "boll_lower", "sma20", "atr", "sma_trend",
                "sma50", "sma_trend_5ago", "sma5"]
    sig = df[sig_cols].shift(1)

    # Sector gate (Option A): merge sector close/SMA200 into the signal frame.
    sector_df = cfg.get("sector_df")
    if sector_df is not None and not sector_df.empty:
        sdf = add_indicators(sector_df, cfg)
        s_close = sdf["close"].reindex(df.index).ffill()
        s_trend = sdf["sma_trend"].reindex(df.index).ffill()
        sig["sector_close"] = s_close.shift(1)
        sig["sector_sma_trend"] = s_trend.shift(1)
    else:
        sig["sector_close"] = np.nan
        sig["sector_sma_trend"] = np.nan

    trades = []
    i = 0
    n = len(df)
    while i < n:
        ts = df.index[i]
        row = df.iloc[i]
        # Setup evaluated at PREVIOUS bar's close (shifted signal).
        if i >= 1 and _signal_met(sig.iloc[i], cfg, baseline):
            # Earnings blackout: block entry if within window of an earnings date.
            if earnings_dates and _near_earnings(ts, earnings_dates, earnings_window):
                i += 1
                continue
            # Entry at THIS bar's open + slippage.
            entry = float(row["open"]) + float(cfg["slippage"])
            if entry <= 0:
                i += 1
                continue
            entry_ts = ts
            entry_atr = float(sig.iloc[i]["atr"])
            # Catastrophic stop: entry - 2.0*ATR14 (structural, fixed).
            cat_mult = float(cfg.get("catastrophic_atr_mult", DEFAULT_CFG["catastrophic_atr_mult"]))
            cat_stop = entry - cat_mult * entry_atr
            # Walk forward up to max_hold_days to find the exit.
            exit_reason = None
            exit_px = None
            exit_ts = None
            for j in range(i + 1, min(i + 1 + int(cfg["max_hold_days"]), n)):
                bar = df.iloc[j]
                # Profit target: first touch of SMA5 (close > SMA5).
                if float(bar["close"]) > float(sig.iloc[j]["sma5"]):
                    exit_reason = "sma5_touch"
                    exit_px = float(bar["close"])
                    exit_ts = df.index[j]
                    break
                # Catastrophic stop.
                if float(bar["low"]) <= cat_stop:
                    exit_reason = "cat_stop"
                    exit_px = cat_stop
                    exit_ts = df.index[j]
                    break
            if exit_reason is None:
                # Time stop: exit at the close of the max-hold day.
                j = min(i + int(cfg["max_hold_days"]), n - 1)
                exit_reason = "time_stop"
                exit_px = float(df.iloc[j]["close"])
                exit_ts = df.index[j]
            ret = (exit_px - entry) / entry
            trades.append({
                "entry_ts": str(entry_ts.date()), "exit_ts": str(exit_ts.date()),
                "entry": entry, "exit": exit_px,
                "ret_pct": ret * 100.0, "exit_reason": exit_reason,
                "hold_days": (exit_ts - entry_ts).days,
            })
            i = j + 1  # no overlapping positions
        else:
            i += 1
    return trades


def _stats(trades: list[dict], cfg: dict) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "expectancy_usd": 0.0, "total_pnl_usd": 0.0}
    rets = np.array([t["ret_pct"] for t in trades])
    size_usd = cfg["size_pct"] * cfg["equity"]
    pnls = rets / 100.0 * size_usd
    wins = rets > 0
    gross_win = pnls[wins].sum()
    gross_loss = pnls[~wins].sum()
    return {
        "trades": len(trades),
        "win_rate": float(wins.mean()),
        "expectancy_usd": float(pnls.mean()),
        "total_pnl_usd": float(pnls.sum()),
        "mean_ret_pct": float(rets.mean()),
        "median_ret_pct": float(np.median(rets)),
        "profit_factor": float(abs(gross_win / gross_loss)) if gross_loss != 0 else float("inf"),
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])),
        "max_drawdown_pct": _max_drawdown(pnls, cfg["equity"]),
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in set(t["exit_reason"] for t in trades)},
    }


def _max_drawdown(pnls: np.ndarray, equity: float) -> float:
    if len(pnls) == 0:
        return 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / equity
    return float(abs(dd.min()) * 100.0)


def run_backtest(df: pd.DataFrame, symbol: str, cfg: dict, baseline: bool = False,
                 earnings_dates: set | None = None, earnings_window: int = 2,
                 sector_df: pd.DataFrame | None = None) -> dict:
    if df.empty:
        return {"error": "no data", "trades": 0}
    if sector_df is not None:
        cfg = dict(cfg)
        cfg["sector_df"] = sector_df
    trades = simulate(df, cfg, baseline, earnings_dates=earnings_dates,
                      earnings_window=earnings_window)
    stats = _stats(trades, cfg)
    stats.update({"symbol": symbol, "cfg": cfg, "baseline": baseline,
                  "date_range": f"{df.index[0].date()} to {df.index[-1].date()}"})
    return stats


# ---------------------------------------------------------------------------
# Benchmark reporting (buy-and-hold, exposure, Sharpe/Calmar)
# ---------------------------------------------------------------------------

def _equity_curve(trades: list[dict], cfg: dict, df: pd.DataFrame) -> pd.Series:
    """Daily equity curve from trade PnLs (applied on exit day)."""
    if not trades:
        return pd.Series(dtype=float)
    size_usd = cfg["size_pct"] * cfg["equity"]
    pnl_by_day = {}
    for t in trades:
        day = pd.Timestamp(t["exit_ts"]).tz_localize(None).normalize()
        pnl_by_day[day] = pnl_by_day.get(day, 0.0) + t["ret_pct"] / 100.0 * size_usd
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
    """Buy-and-hold benchmark: return, max DD, exposure, Sharpe/Calmar."""
    if df.empty:
        return {}
    first = float(df["close"].iloc[0])
    last = float(df["close"].iloc[-1])
    bh_return_pct = (last / first - 1.0) * 100.0
    # Buy-and-hold equity curve (100% invested, no sizing).
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
    """Side-by-side strategy vs buy-and-hold benchmark metrics."""
    bh = buy_hold_benchmark(df, cfg)
    eq = _equity_curve(trades, cfg, df)
    strat_ann = _annualized_return(eq, len(df))
    strat_dd = _max_dd_from_curve(eq)
    strat_sharpe = _sharpe(eq)
    strat_calmar = strat_ann / (strat_dd / 100.0) if strat_dd > 0 else 0.0
    # Exposure: fraction of trading days holding a position.
    total_hold = sum(t["hold_days"] for t in trades)
    exposure_pct = total_hold / len(df) * 100.0 if not df.empty else 0.0
    return {
        "strategy_return_pct": strat_ann * 100.0,
        "buy_hold_return_pct": bh["bh_return_pct"],
        "strategy_max_dd_pct": strat_dd,
        "buy_hold_max_dd_pct": bh["bh_max_dd_pct"],
        "exposure_pct": exposure_pct,
        "strategy_sharpe": strat_sharpe,
        "buy_hold_sharpe": bh["bh_sharpe"],
        "strategy_calmar": strat_calmar,
        "buy_hold_calmar": bh["bh_calmar"],
        "strategy_annualized_return_pct": strat_ann * 100.0,
        "buy_hold_annualized_return_pct": bh["bh_annualized_return_pct"],
    }


# ---------------------------------------------------------------------------
# Anchored / rolling walk-forward OOS
# ---------------------------------------------------------------------------

def walk_forward(df: pd.DataFrame, cfg: dict, baseline: bool = False,
                 train_years: int = 2, test_years: int = 1,
                 earnings_dates: set | None = None,
                 earnings_window: int = 2,
                 sector_df: pd.DataFrame | None = None) -> dict:
    """Expanding walk-forward OOS: 2yr train (warm-up), 1yr test roll.

    Fixed-param strategy (no fitting), so 'train' is indicator warm-up. We run
    the strategy on data up to each test-window end and keep only trades that
    ENTER within the test window (no lookahead).
    """
    if df.empty:
        return {"error": "no data", "trades": 0}
    if sector_df is not None:
        cfg = dict(cfg)
        cfg["sector_df"] = sector_df
    days = df.index.normalize().unique()
    n = len(days)
    train_n = int(train_years * 252)
    test_n = int(test_years * 252)
    if n <= train_n + test_n:
        return {"error": "not enough data for walk-forward", "trades": 0}

    folds = []
    all_trades = []
    start = train_n
    while start < n:
        end = min(start + test_n, n)
        test_start_day = days[start]
        test_end_day = days[end - 1]
        # Run on data up to end of test window.
        sim_df = df[df.index <= test_end_day.replace(hour=23, minute=59)]
        trades = simulate(sim_df, cfg, baseline, earnings_dates=earnings_dates,
                          earnings_window=earnings_window)
        # Keep trades entering within the test window.
        fold_trades = []
        for t in trades:
            et = pd.Timestamp(t["entry_ts"]).normalize()
            s = pd.Timestamp(test_start_day).tz_localize(None).normalize()
            e = pd.Timestamp(test_end_day).tz_localize(None).normalize()
            if s <= et <= e:
                fold_trades.append(t)
        folds.append({"fold": len(folds), "start": str(test_start_day.date()),
                      "end": str(test_end_day.date()), "n": len(fold_trades),
                      **{k: v for k, v in _stats(fold_trades, cfg).items()
                         if k not in ("exit_reasons",)}})
        all_trades.extend(fold_trades)
        start = end

    agg = _stats(all_trades, cfg)
    agg["folds"] = folds
    agg["cfg"] = cfg
    agg["baseline"] = baseline
    agg["benchmark"] = benchmark_report(df, all_trades, cfg)
    return agg


# ---------------------------------------------------------------------------
# Parameter robustness scan (RSI threshold x exit SMA)
# ---------------------------------------------------------------------------

def robustness_scan(df: pd.DataFrame, symbol: str,
                    earnings_dates: set | None = None,
                    earnings_window: int = 2) -> list[dict]:
    """Sweep RSI thresholds (5,10,15) x exit SMA (3,5,8) on the baseline.

    Confirms the edge is not sitting on an isolated parameter cliff.
    """
    results = []
    for rsi_below in [5, 10, 15]:
        for profit_sma in [3, 5, 8]:
            cfg = dict(BASELINE_CFG)
            cfg["rsi_buy_below"] = rsi_below
            cfg["profit_sma"] = profit_sma
            trades = simulate(df, cfg, baseline=True, earnings_dates=earnings_dates,
                              earnings_window=earnings_window)
            stats = _stats(trades, cfg)
            stats.update({"symbol": symbol, "rsi_buy_below": rsi_below,
                          "profit_sma": profit_sma})
            results.append(stats)
    results.sort(key=lambda x: -x["expectancy_usd"])
    return results


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


def print_walk_forward(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    print(f"\n=== Walk-Forward OOS — {r.get('symbol', '?')} ===")
    print(f"  OOS trades: {r['trades']} | win rate: {r['win_rate']:.1%} | PF: {r['profit_factor']:.2f}")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print_benchmark(r)
    print("  folds:")
    for f in r.get("folds", []):
        print(f"    fold {f['fold']}: {f['start']} -> {f['end']}  n={f['n']} win={f['win_rate']:.1%} exp=${f['expectancy_usd']:.2f}")


def print_results(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    tag = "BASELINE (Connors RSI-2)" if r["baseline"] else "FULL MEAN-REVERSION"
    print(f"\n=== Swing Mean-Reversion — {r['symbol']} [{tag}] ===")
    print(f"  range: {r['date_range']}")
    print(f"  trades: {r['trades']} | win rate: {r['win_rate']:.1%}")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print(f"  mean ret: {r['mean_ret_pct']:.3f}% | median: {r['median_ret_pct']:.3f}%")
    print(f"  profit factor: {r['profit_factor']:.2f} | avg hold: {r['avg_hold_days']:.1f}d | max DD: {r['max_drawdown_pct']:.1f}%")
    print(f"  exits: {r['exit_reasons']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-day swing mean-reversion backtest")
    parser.add_argument("--symbol", default="AMD")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--all", action="store_true", help="Run full mean-reversion config")
    parser.add_argument("--baseline", action="store_true", help="Run Connors RSI-2 baseline")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run anchored/rolling walk-forward OOS on the baseline")
    parser.add_argument("--robustness", action="store_true",
                        help="Sweep RSI threshold x exit SMA (parameter robustness)")
    parser.add_argument("--earnings-blackout", action="store_true",
                        help="Block entries within +/-2 days of earnings (avoid gap-down tail risk)")
    parser.add_argument("--gate", choices=["dual_sma50", "sma200_slope", "sector"],
                        help="Intermediate health gate on top of the SMA200 macro gate")
    parser.add_argument("--sector", default=None,
                        help="Sector/index symbol for the 'sector' gate (e.g. SMH, QQQ)")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-swing-mr")
    client = AlpacaClient()
    symbol = args.symbol.upper()

    try:
        df = load_daily(client, symbol, args.days)
        if df.empty:
            logger.error("No daily data.")
            return
        logger.info(f"Loaded {len(df)} daily bars for {symbol} ({df.index[0].date()} to {df.index[-1].date()})")

        # Earnings blackout: fetch historical earnings dates if requested.
        earnings_dates = None
        if args.earnings_blackout:
            earnings_dates = load_earnings_dates(symbol, args.days)
            logger.info(f"Earnings blackout: {len(earnings_dates)} earnings dates loaded for {symbol}")

        # Sector gate: load sector/index data if requested.
        sector_df = None
        if args.gate == "sector" and args.sector:
            sector_df = load_daily(client, args.sector.upper(), args.days)
            logger.info(f"Sector gate: loaded {len(sector_df)} bars for {args.sector.upper()}")

        # Build the base config with the gate mode.
        base_cfg = dict(BASELINE_CFG)
        if args.gate:
            base_cfg["gate_mode"] = args.gate

        results = []
        if args.walk_forward:
            r = walk_forward(df, base_cfg, baseline=True,
                             earnings_dates=earnings_dates, sector_df=sector_df)
            r["symbol"] = symbol
            results.append(r)
            print_walk_forward(r)
        if args.robustness:
            scan = robustness_scan(df, symbol, earnings_dates=earnings_dates)
            results.append({"type": "robustness_scan", "symbol": symbol, "scan": scan})
            print(f"\n=== Robustness Scan — {symbol} (RSI threshold x exit SMA) ===")
            print(f"  {'RSI<':>5} {'SMA':>4} {'n':>4} {'win%':>6} {'exp$':>8} {'PF':>5} {'total$':>9}")
            for s in scan:
                print(f"  {s['rsi_buy_below']:>5} {s['profit_sma']:>4} {s['trades']:>4} "
                      f"{s['win_rate']*100:>5.1f}% {s['expectancy_usd']:>8.2f} "
                      f"{s['profit_factor']:>5.2f} {s['total_pnl_usd']:>9.2f}")
        if args.baseline:
            r = run_backtest(df, symbol, base_cfg, baseline=True,
                             earnings_dates=earnings_dates, sector_df=sector_df)
            results.append(r)
            print_results(r)
        if args.all:
            r = run_backtest(df, symbol, dict(DEFAULT_CFG), baseline=False,
                             earnings_dates=earnings_dates, sector_df=sector_df)
            results.append(r)
            print_results(r)
        if not results:
            # Default: run both.
            results.append(run_backtest(df, symbol, base_cfg, baseline=True,
                                        earnings_dates=earnings_dates, sector_df=sector_df))
            results.append(run_backtest(df, symbol, dict(DEFAULT_CFG), baseline=False,
                                        earnings_dates=earnings_dates, sector_df=sector_df))
            for r in results:
                print_results(r)

        path = os.path.join(DATA_DIR, f"{symbol.lower()}_swing_mr.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
        if not args.no_discord:
            try:
                best = max(results, key=lambda x: x.get("expectancy_usd", 0))
                send_discord_message(f"Swing MR {symbol}: {len(results)} runs, best exp=${best.get('expectancy_usd', 0):.2f}")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Swing MR backtest failed: {e}")
        log_exception_to_jira(e, "Swing Mean-Reversion Backtest Failure")
        raise


if __name__ == "__main__":
    main()