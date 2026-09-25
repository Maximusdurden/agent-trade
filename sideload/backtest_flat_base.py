#!/usr/bin/env python3
"""Non-tech Flat Base Breakout backtest — leakage-free from day one.

Implements the adapted O'Neil/Minervini CAN SLIM Stage 2 Flat Base Breakout
strategy, recalibrated for non-tech / old-economy / commodity names.

CORE LEAKAGE-FREE BOUNDARY:
    [ Day t-1 Close ] --( detect base: prior uptrend, consolidation, tightness )
            |
            v (Breakout: Close[t-1] > Base_High AND Vol[t-1] >= 1.35*SMA20vol)
    [ Day t Open ]    --( Execution: market fill at open + slippage )
            |
            v (Holding until 2.5R scale-out, SMA20 trail / 5R, or stop)
    [ Day t+N Close ] --( Exit )

All signal columns are computed with .shift(1) relative to the entry trade day,
so no lookahead: the setup at day t-1's close is known before day t's open.

Usage:
    python -m sideload.backtest_flat_base --symbols DIA,CAT,XOM,JPM,UNH,FCX,GLD,USO
    python -m sideload.backtest_flat_base --symbols AMD,NVDA   # tech control
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

logger = logging.getLogger("BacktestFlatBase")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

START_DATE = "2021-01-01"
END_DATE = "2026-09-24"

# Non-tech / commodity pool + tech control.
NONTECH_SYMBOLS = ["DIA", "CAT", "XOM", "JPM", "UNH", "FCX", "GLD", "USO"]
TECH_SYMBOLS = ["AMD", "NVDA"]
# High-beta tech/growth momentum universe (Phase 1c).
GROWTH_SYMBOLS = ["NVDA", "AMD", "AVGO", "DELL", "HPE", "MU", "META", "TSLA",
                  "PLTR", "ARM", "SMH", "QQQ"]

# --- Default config (directive section 2) ---
DEFAULT_CFG = {
    # Setup phase (lookback 60 days)
    "uptrend_gain_pct": 15.0,     # prior uptrend: +15% within last 60 bars
    "uptrend_lookback": 60,
    "base_min_bars": 15,          # base duration 15-35 bars
    "base_max_bars": 35,
    "base_tightness": 0.12,       # (HH-LL)/LL <= 12%
    "base_sma": 50,               # base lows above SMA(50)
    # Execution trigger
    "breakout_rvol": 1.35,        # Volume >= 1.35 * SMA(Vol,20)
    "macro_sma": 20,              # SPY Close > SPY SMA(20)
    "slippage": 0.05,             # $ per share at open
    # Risk & exits
    "atr_stop_mult": 1.5,         # stop = breakout - 1.5*ATR14
    "tp1_r": 2.5,                 # 50% scale-out at +2.5R
    "tp2_r": 5.0,                 # remaining 50% at +5R or SMA20 trail
    "trail_sma": 20,              # trail remainder with SMA(20)
    "max_hold_days": 45,          # time stop (trading days)
    "risk_pct": 0.01,             # 1% equity risked per trade
    "equity": 10000.0,
}

# --- Growth momentum config (Phase 1c: restored O'Neil/Minervini spec) ---
GROWTH_CFG = {
    "uptrend_gain_pct": 30.0,     # prior uptrend: +30% within last 60 bars
    "uptrend_lookback": 60,
    "base_min_bars": 15,          # base duration 15-40 bars
    "base_max_bars": 40,
    "base_tightness": 0.20,       # (HH-LL)/LL <= 20%
    "base_sma": 50,               # base lows above SMA(50)
    "breakout_rvol": 1.5,         # Volume >= 1.5 * SMA(Vol,20)
    "macro_sma": 20,              # QQQ Close > QQQ SMA(20)
    "slippage": 0.05,
    "atr_stop_mult": 1.5,         # stop = breakout - 1.5*ATR14
    "tp1_r": 3.0,                 # 50% scale-out at +3R
    "tp2_r": 6.0,                 # remaining 50% at +6R or SMA20 trail
    "trail_sma": 20,
    "max_hold_days": 45,
    "risk_usd": 150.0,            # fixed risk $150 per trade (1R = $150)
    "equity": 10000.0,
}


def load_daily(client: AlpacaClient, symbol: str, days_back: int = 365 * 8) -> pd.DataFrame:
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


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Add daily signal columns (computed at current bar close, shifted later)."""
    df = df.copy()
    df["sma20"] = _sma(df["close"], 20)
    df["sma_vol20"] = _sma(df["volume"], 20)
    df["sma50"] = _sma(df["close"], int(cfg["base_sma"]))
    df["atr14"] = _atr(df, 14)
    df["trail_sma"] = _sma(df["close"], int(cfg["trail_sma"]))
    return df


def _prior_uptrend_met(df: pd.DataFrame, i: int, cfg: dict) -> bool:
    """Prior uptrend: price gained >= uptrend_gain_pct within last `uptrend_lookback` bars."""
    lookback = int(cfg["uptrend_lookback"])
    gain = float(cfg["uptrend_gain_pct"]) / 100.0
    start = max(0, i - lookback)
    low_start = df["close"].iloc[start:i].min()
    close_now = df["close"].iloc[i]
    if low_start <= 0:
        return False
    return (close_now / low_start - 1.0) >= gain


def _base_detected(df: pd.DataFrame, i: int, cfg: dict) -> int:
    """Detect a flat base ending at bar i-1 (the bar BEFORE the breakout bar).

    The base's last bar is i-1, so the breakout bar (i) can close above the
    base high. Base = the last `base_len` bars (15-35) ending at i-1 form a
    tight consolidation:
      - (HH - LL) / LL <= base_tightness
      - base lows remain above SMA(50)
    Returns the base length (int) if a valid base exists, else 0.
    """
    tightness = float(cfg["base_tightness"])
    min_bars = int(cfg["base_min_bars"])
    max_bars = int(cfg["base_max_bars"])
    sma50 = df["sma50"].iloc[i]
    if pd.isna(sma50):
        return 0
    for length in range(min_bars, max_bars + 1):
        start = i - length  # base ends at i-1
        if start < 0:
            continue
        seg = df.iloc[start:i]
        hh = seg["high"].max()
        ll = seg["low"].min()
        if ll <= 0:
            continue
        tight = (hh - ll) / ll <= tightness
        lows_above = seg["low"].min() > sma50
        if tight and lows_above:
            return length
    return 0


def _base_high(df: pd.DataFrame, i: int, base_len: int) -> float:
    """Highest high of the base of length `base_len` ending at bar i-1."""
    start = i - base_len
    if start < 0:
        return float(df["high"].iloc[i])
    return float(df["high"].iloc[start:i].max())


def _macro_ok(spy_sig: pd.DataFrame, i: int, cfg: dict) -> bool:
    """Macro filter: SPY Close[t-1] > SPY SMA(20)[t-1]."""
    if spy_sig is None or i >= len(spy_sig):
        return True  # fail-open if no SPY data
    close = spy_sig["close"].iloc[i]
    sma20 = spy_sig["sma20"].iloc[i]
    if pd.isna(close) or pd.isna(sma20):
        return True
    return close > sma20


def simulate(df: pd.DataFrame, cfg: dict, spy_sig: pd.DataFrame | None = None) -> list[dict]:
    """Simulate the flat base breakout strategy on one symbol's daily frame.

    Leakage-free: all signals use data up to t-1 close (shifted). Entry fills at
    t open + slippage. Exit on 2.5R scale-out / SMA20 trail / 5R / stop / time stop.
    """
    df = add_indicators(df, cfg)
    # Shift signal columns by 1 so the setup at t-1 is known before day t.
    sig_cols = ["close", "high", "low", "open", "volume", "sma20", "sma_vol20",
                "sma50", "atr14", "trail_sma"]
    sig = df[sig_cols].shift(1)

    risk_pct = float(cfg.get("risk_pct", 0.0))
    risk_usd = float(cfg.get("risk_usd", 0.0))
    equity = float(cfg["equity"])
    tp1_r = float(cfg["tp1_r"])
    tp2_r = float(cfg["tp2_r"])
    max_hold = int(cfg["max_hold_days"])
    slippage = float(cfg["slippage"])
    atr_mult = float(cfg["atr_stop_mult"])

    trades = []
    i = 1
    n = len(df)
    while i < n:
        row = sig.iloc[i]
        # Prior uptrend gate (using t-1 and prior).
        if not _prior_uptrend_met(sig, i, cfg):
            i += 1
            continue
        # Base consolidation ending at t-1.
        base_len = _base_detected(sig, i, cfg)
        if not base_len:
            i += 1
            continue
        # Breakout: Close[t-1] > Base_High.
        base_high = _base_high(sig, i, base_len)
        if not (row["close"] > base_high):
            i += 1
            continue
        # Volume confirmation: Vol[t-1] >= 1.35 * SMA(Vol,20)[t-1].
        vol20 = row["sma_vol20"]
        if pd.isna(vol20) or vol20 <= 0 or not (row["volume"] >= float(cfg["breakout_rvol"]) * vol20):
            i += 1
            continue
        # Macro filter: SPY Close[t-1] > SPY SMA(20)[t-1].
        if not _macro_ok(spy_sig, i, cfg):
            i += 1
            continue

        # Entry at THIS bar's open + slippage.
        entry = float(df["open"].iloc[i]) + slippage
        if entry <= 0:
            i += 1
            continue
        entry_ts = df.index[i]
        # Stop: breakout_level - 1.5*ATR14 (or base lower boundary).
        atr = sig["atr14"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            i += 1
            continue
        stop = base_high - atr_mult * atr
        if stop >= entry:
            i += 1
            continue
        risk_per_share = entry - stop
        r = risk_per_share
        # Sizing: fixed risk USD (risk_usd) OR % of equity (risk_pct).
        if risk_usd > 0:
            qty = risk_usd / risk_per_share
        else:
            qty = (risk_pct * equity) / risk_per_share
        if qty <= 0:
            i += 1
            continue

        # Walk forward to find exit.
        exit_reason = None
        exit_px = None
        exit_ts = None
        half_taken = False
        for j in range(i + 1, n):
            bar = df.iloc[j]
            low = float(bar["low"])
            high = float(bar["high"])
            close = float(bar["close"])
            ts = df.index[j]
            # Stop hit (intraday low).
            if low <= stop:
                exit_reason = "stop"
                exit_px = stop
                exit_ts = ts
                break
            # 2.5R scale-out: take 50% off.
            if not half_taken and high >= entry + tp1_r * r:
                half_taken = True
            # Remainder: exit at +5R OR trail with SMA20.
            if half_taken:
                if high >= entry + tp2_r * r:
                    exit_reason = "tp2"
                    exit_px = entry + tp2_r * r
                    exit_ts = ts
                    break
                trail = float(sig["trail_sma"].iloc[j]) if j < len(sig) else close
                if low <= trail:
                    exit_reason = "trail_sma"
                    exit_px = trail
                    exit_ts = ts
                    break
            # Time stop (trading days).
            if (ts - entry_ts).days > max_hold:
                exit_reason = "time_stop"
                exit_px = close
                exit_ts = ts
                break

        if exit_reason is None:
            exit_reason = "open_end"
            exit_px = float(df["close"].iloc[n - 1])
            exit_ts = df.index[n - 1]

        # PnL: half at tp1 (if taken) + remainder at exit.
        pnl = 0.0
        if half_taken:
            pnl += 0.5 * qty * (entry + tp1_r * r - entry)
            pnl += 0.5 * qty * (exit_px - entry)
        else:
            pnl = qty * (exit_px - entry)
        ret_pct = (exit_px - entry) / entry * 100.0
        r_mult = (exit_px - entry) / r if r > 0 else 0.0
        trades.append({
            "entry_ts": str(entry_ts.date()), "exit_ts": str(exit_ts.date()),
            "entry": entry, "exit": exit_px, "stop": stop, "base_high": base_high,
            "qty": qty, "r": r, "r_mult": r_mult,
            "ret_pct": ret_pct, "pnl": pnl, "exit_reason": exit_reason,
            "hold_days": (exit_ts - entry_ts).days, "half_taken": half_taken,
        })
        i = j + 1  # no overlapping positions
    return trades


def _stats(trades: list[dict], cfg: dict) -> dict:
    base = {
        "trades": len(trades),
        "win_rate": 0.0,
        "expectancy_usd": 0.0,
        "total_pnl_usd": 0.0,
        "avg_r_mult": 0.0,
        "median_r_mult": 0.0,
        "profit_factor": 0.0,
        "avg_hold_days": 0.0,
        "avg_stop_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "exit_reasons": {},
    }
    if not trades:
        return base
    pnls = np.array([t["pnl"] for t in trades])
    r_mults = np.array([t["r_mult"] for t in trades])
    wins = pnls > 0
    gross_win = pnls[wins].sum()
    gross_loss = pnls[~wins].sum()
    base.update({
        "win_rate": float(wins.mean()),
        "expectancy_usd": float(pnls.mean()),
        "total_pnl_usd": float(pnls.sum()),
        "avg_r_mult": float(r_mults.mean()),
        "median_r_mult": float(np.median(r_mults)),
        "profit_factor": float(abs(gross_win / gross_loss)) if gross_loss != 0 else float("inf"),
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])),
        "avg_stop_pct": float(np.mean([(t["entry"] - t["stop"]) / t["entry"] * 100 for t in trades])),
        "max_drawdown_pct": _max_drawdown(pnls, cfg["equity"]),
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r)
                         for r in set(t["exit_reason"] for t in trades)},
    })
    return base


def _max_drawdown(pnls: np.ndarray, equity: float) -> float:
    if len(pnls) == 0:
        return 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / equity
    return float(abs(dd.min()) * 100.0)


def print_results(r: dict) -> None:
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        return
    print(f"  {r['symbol']}: trades={r['trades']} win={r['win_rate']:.1%} "
          f"exp=${r['expectancy_usd']:.2f} total=${r['total_pnl_usd']:.2f} "
          f"avgR={r['avg_r_mult']:.2f} PF={r['profit_factor']:.2f} "
          f"avgStop={r['avg_stop_pct']:.2f}% maxDD={r['max_drawdown_pct']:.1f}% "
          f"avgHold={r['avg_hold_days']:.1f}d")


def run_pool(client: AlpacaClient, symbols: list[str], cfg: dict,
             spy_sig: pd.DataFrame | None) -> list[dict]:
    results = []
    for symbol in symbols:
        df = load_daily(client, symbol)
        if df.empty:
            logger.warning(f"No data for {symbol}")
            continue
        df = df[(df.index >= START_DATE) & (df.index <= END_DATE)]
        if df.empty:
            logger.warning(f"No data in window for {symbol}")
            continue
        logger.info(f"{symbol}: {len(df)} bars ({df.index[0].date()} to {df.index[-1].date()})")
        trades = simulate(df, cfg, spy_sig)
        stats = _stats(trades, cfg)
        stats["symbol"] = symbol
        stats["date_range"] = f"{df.index[0].date()} to {df.index[-1].date()}"
        results.append(stats)
        print_results(stats)
    return results


def aggregate(results: list[dict]) -> dict:
    total_trades = sum(r["trades"] for r in results)
    total_pnl = sum(r["total_pnl_usd"] for r in results)
    avg_r = sum(r["avg_r_mult"] * r["trades"] for r in results) / total_trades if total_trades else 0.0
    gross_win = sum(max(0.0, r["total_pnl_usd"]) for r in results)
    gross_loss = sum(min(0.0, r["total_pnl_usd"]) for r in results)
    pf = abs(gross_win / gross_loss) if gross_loss != 0 else float("inf")
    wins = sum(int(r["trades"] * r["win_rate"]) for r in results)
    win_rate = wins / total_trades if total_trades else 0.0
    max_dd = max((r["max_drawdown_pct"] for r in results), default=0.0)
    # Annualized PnL over the window (2021-01-01 to 2026-09-24 = ~5.73 years).
    years = 5.73
    annualized = total_pnl / years
    return {
        "trades": total_trades, "win_rate": win_rate, "total_pnl_usd": total_pnl,
        "annualized_pnl_usd": annualized, "avg_r_mult": avg_r,
        "profit_factor": pf, "max_drawdown_pct": max_dd,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Flat base breakout backtest")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols (default: non-tech pool)")
    parser.add_argument("--tech", action="store_true", help="Run tech control group (AMD, NVDA)")
    parser.add_argument("--growth", action="store_true",
                        help="Run high-beta tech/growth universe with growth momentum rules")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-flat-base")
    client = AlpacaClient()

    try:
        # Choose config + macro symbol.
        if args.growth:
            cfg = dict(GROWTH_CFG)
            macro_symbol = "QQQ"
            symbols = GROWTH_SYMBOLS
        else:
            cfg = dict(DEFAULT_CFG)
            macro_symbol = "SPY"
            if args.symbols:
                symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            elif args.tech:
                symbols = TECH_SYMBOLS
            else:
                symbols = NONTECH_SYMBOLS

        # Load macro index for filter.
        macro = load_daily(client, macro_symbol)
        macro = macro[(macro.index >= START_DATE) & (macro.index <= END_DATE)]
        macro_sig = None
        if not macro.empty:
            macro = add_indicators(macro, cfg)
            macro_sig = macro[["close", "sma20"]].shift(1)
            logger.info(f"{macro_symbol} macro filter: {len(macro)} bars")

        results = run_pool(client, symbols, cfg, macro_sig)
        agg = aggregate(results)
        print("\n=== POOL AGGREGATE ===")
        print(f"  symbols: {len(results)} | trades: {agg['trades']} | win: {agg['win_rate']:.1%} "
              f"| PnL: ${agg['total_pnl_usd']:.2f} | annualized: ${agg['annualized_pnl_usd']:.2f}/yr "
              f"| avgR: {agg['avg_r_mult']:.2f} | PF: {agg['profit_factor']:.2f} "
              f"| maxDD: {agg['max_drawdown_pct']:.1f}%")

        # Go/No-Go gate.
        if args.growth:
            passed = (agg["annualized_pnl_usd"] >= 2500.0 and agg["profit_factor"] >= 2.0)
            print("\n=== PRODUCTION GATE (growth) ===")
            print("Go criteria: annualized PnL >= $2,500/yr AND PF >= 2.0")
            print(f"VERDICT: {'GO' if passed else 'NO-GO'}")
        elif not args.tech and not args.symbols:
            passed = (agg["trades"] >= 30 and agg["profit_factor"] >= 1.50
                      and agg["avg_r_mult"] > 0.25)
            print("\n=== GO / NO-GO GATE (non-tech) ===")
            print("Go criteria: >=30 trades AND PF >= 1.50 AND avgR > +0.25")
            print(f"VERDICT: {'GO' if passed else 'NO-GO'}")

        path = os.path.join(DATA_DIR, "flat_base_results.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "aggregate": agg}, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
        if not args.no_discord:
            try:
                send_discord_message(f"Flat base backtest: {len(results)} symbols, "
                                     f"{agg['trades']} trades, ${agg['total_pnl_usd']:.2f} PnL")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Flat base backtest failed: {e}")
        log_exception_to_jira(e, "Flat Base Backtest Failure")
        raise


if __name__ == "__main__":
    main()