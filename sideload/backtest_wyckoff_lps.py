#!/usr/bin/env python3
"""Wyckoff LPS/LPSY swing backtest — leakage-free from day one.

Implements the Wyckoff Last Point of Support (LPS) multi-timeframe confluence
setup as a deterministic, backtestable rule set. The strategy finds a weekly
accumulation/markup bias, a daily Sign of Strength (SOS) breakout on expanding
volume, then a daily LPS pullback on diminishing volume, and enters on a break
above the prior candle's high with a tight stop below the LPS swing low.

CORE LEAKAGE-FREE BOUNDARY (per blueprint):
    [ Weekly/Daily t-1 Close ] --( Compute indicators: EMA21, MACD, SOS, LPS )
            |
            v (Setup met at 4:00 PM close)
    [ Daily t Open ]    --( Execution: market fill at open + slippage )
            |
            v (Holding until 2R/4R target, trailing SMA10, or stop)
    [ Daily t+N Close ] --( Exit )

All signal columns are computed with .shift(1) relative to the entry trade day,
so no lookahead: the setup at day t-1's close is known before day t's open.
Weekly indicators are computed on resampled weekly bars and forward-filled onto
the daily frame WITHOUT lookahead (only completed weeks are used).

Usage:
    python -m sideload.backtest_wyckoff_lps --symbols ABBV,HCA,UNG,CAT,JPM,XOM,DIA,NOK
    python -m sideload.backtest_wyckoff_lps --symbols ABBV --verbose
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

logger = logging.getLogger("BacktestWyckoffLPS")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

# Testing window per directive.
START_DATE = "2021-01-01"
END_DATE = "2026-09-24"

# Default candidate pool (directive section 1).
DEFAULT_SYMBOLS = ["ABBV", "HCA", "UNG", "NOK", "CAT", "JPM", "XOM", "DIA"]

# --- Default config (directive section 2) ---
DEFAULT_CFG = {
    # Weekly bias
    "weekly_ema": 21,          # Weekly Close > Weekly EMA(21)
    "weekly_slope_bars": 4,    # Weekly Close > Close 4 weeks ago
    # Daily SOS
    "sos_lookback": 15,        # SOS within last 15 bars
    "sos_high_lookback": 20,   # 20-day high
    "sos_rvol": 1.25,          # Volume > 1.25 * SMA(Volume,20)
    # Daily LPS pullback
    "lps_band_pct": 0.015,     # within 1.5% band of SMA20 / breakout level
    "lps_vol_dryup": 0.85,     # avg vol last 3 pullback bars < 0.85 * SMA(Vol,20)
    "lps_pullback_bars": 8,    # retrace 3-8 bars into EMA21/SMA20 zone
    # Execution
    "slippage": 0.05,          # $ per share at open
    # Risk & sizing
    "stop_lookback": 3,        # stop = lowest low of last 3 pullback bars
    "risk_pct": 0.01,          # 1% equity risked per trade
    "equity": 10000.0,
    # Exits
    "tp_r_mult": 2.0,          # take 50% off at +2R
    "trail_sma": 10,           # trail remainder with SMA(10)
    "max_hold_days": 60,       # time stop
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


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    macd = _ema(close, fast) - _ema(close, slow)
    sig = _ema(macd, signal)
    return macd - sig


def add_weekly_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Compute weekly EMA21 / MACD / slope and forward-fill onto daily bars.

    Leakage-free: weekly bars are resampled from completed weeks only. The
    weekly indicator value for a given daily bar uses the most recent COMPLETED
    week (shifted by 1 week), so no lookahead into the current in-progress week.
    """
    df = df.copy()
    # Resample to weekly (W-FRI) using only completed weeks.
    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    weekly["ema21"] = _ema(weekly["close"], int(cfg["weekly_ema"]))
    weekly["macd_hist"] = _macd_hist(weekly["close"])
    # Slope: close vs close 4 weeks ago.
    weekly["close_4ago"] = weekly["close"].shift(int(cfg["weekly_slope_bars"]))
    weekly["bias"] = (weekly["close"] > weekly["ema21"]) & \
                     (weekly["close"] > weekly["close_4ago"])
    # Shift by 1 week so only completed weeks are used (no lookahead).
    weekly_shifted = weekly.shift(1)
    # Map weekly values onto daily bars by week label (drop tz to avoid warning).
    # Convert weekly_shifted to a PeriodIndex so reindex matches daily periods.
    weekly_shifted.index = weekly_shifted.index.tz_localize(None).to_period("W-FRI")
    week_labels = df.index.tz_localize(None).to_period("W-FRI")
    w_ema = weekly_shifted["ema21"].reindex(week_labels)
    w_macd = weekly_shifted["macd_hist"].reindex(week_labels)
    w_bias = weekly_shifted["bias"].reindex(week_labels)
    df["weekly_ema21"] = w_ema.to_numpy()
    df["weekly_macd_hist"] = w_macd.to_numpy()
    df["weekly_bias"] = w_bias.to_numpy()
    return df


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range over `period` days."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def add_daily_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Add daily signal columns (computed at current bar close, shifted later)."""
    df = df.copy()
    df["sma20"] = _sma(df["close"], 20)
    df["sma_vol20"] = _sma(df["volume"], 20)
    df["ema21"] = _ema(df["close"], 21)
    df["trail_sma"] = _sma(df["close"], int(cfg["trail_sma"]))
    df["atr14"] = _atr(df, 14)
    # 20-day high (for SOS detection).
    df["high_20"] = df["high"].rolling(int(cfg["sos_high_lookback"])).max()
    # Relative volume.
    df["rvol"] = df["volume"] / df["sma_vol20"].replace(0, np.nan)
    return df


def _weekly_bias_ok(row: pd.Series, cfg: dict) -> bool:
    """Weekly bias gate: Close > EMA21 AND Close > Close 4 weeks ago."""
    if pd.isna(row.get("weekly_bias")):
        return False
    return bool(row["weekly_bias"])


def _sos_met(df: pd.DataFrame, i: int, cfg: dict) -> bool:
    """SOS within the last `sos_lookback` bars: a 20-day high on RVOL > threshold."""
    lookback = int(cfg["sos_lookback"])
    start = max(0, i - lookback)
    for j in range(start, i):
        if pd.isna(df["high_20"].iloc[j]):
            continue
        is_20d_high = df["high"].iloc[j] >= df["high_20"].iloc[j]
        rvol_ok = df["rvol"].iloc[j] > float(cfg["sos_rvol"])
        if is_20d_high and rvol_ok:
            return True
    return False


def _lps_pullback_met(df: pd.DataFrame, i: int, cfg: dict) -> bool:
    """LPS pullback: price within band of SMA20, volume dry-up over last 3 bars."""
    band = float(cfg["lps_band_pct"])
    dryup = float(cfg["lps_vol_dryup"])
    close = df["close"].iloc[i]
    sma20 = df["sma20"].iloc[i]
    if pd.isna(sma20):
        return False
    # Within 1.5% band of SMA20.
    in_band = abs(close - sma20) / sma20 <= band
    # Volume dry-up: avg vol last 3 bars < 0.85 * SMA(vol,20).
    vol20 = df["sma_vol20"].iloc[i]
    if pd.isna(vol20) or vol20 <= 0:
        return False
    avg3 = df["volume"].iloc[max(0, i - 2):i + 1].mean()
    dry = avg3 < dryup * vol20
    return bool(in_band and dry)


def _stop_price(df: pd.DataFrame, i: int, cfg: dict, entry: float) -> float:
    """Compute the stop price based on stop_mode.

    stop_mode="swing_low": lowest low of the last `stop_lookback` pullback bars.
    stop_mode="atr": entry - (atr_mult * ATR14).
    """
    mode = cfg.get("stop_mode", "swing_low")
    if mode == "atr":
        atr = df["atr14"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            return float("nan")
        return entry - float(cfg.get("atr_stop_mult", 1.5)) * atr
    lookback = int(cfg["stop_lookback"])
    return float(df["low"].iloc[max(0, i - lookback + 1):i + 1].min())


def simulate(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """Simulate the Wyckoff LPS strategy on one symbol's daily frame.

    Leakage-free: all signals use data up to t-1 close (shifted). Entry fills at
    t open + slippage. Exit on 2R partial / trailing SMA10 / stop / time stop.
    """
    df = add_weekly_indicators(df, cfg)
    df = add_daily_indicators(df, cfg)
    # Shift signal columns by 1 so the setup at t-1 is known before day t.
    sig_cols = ["close", "high", "low", "open", "volume", "sma20", "ema21", "trail_sma",
                "weekly_bias", "weekly_macd_hist", "high_20", "rvol", "sma_vol20", "atr14"]
    sig = df[sig_cols].shift(1)

    risk_pct = float(cfg["risk_pct"])
    equity = float(cfg["equity"])
    tp_r = float(cfg["tp_r_mult"])
    max_hold = int(cfg["max_hold_days"])
    slippage = float(cfg["slippage"])
    exit_mode = cfg.get("exit_mode", "trail_sma")
    tp2_r = float(cfg.get("tp2_r_mult", 4.5))  # second target for asymmetric exit

    trades = []
    i = 1
    n = len(df)
    while i < n:
        row = sig.iloc[i]
        # Weekly bias gate (from completed week).
        if not _weekly_bias_ok(row, cfg):
            i += 1
            continue
        # Daily SOS within last 15 bars (using t-1 and prior).
        if not _sos_met(sig, i, cfg):
            i += 1
            continue
        # Daily LPS pullback at t-1.
        if not _lps_pullback_met(sig, i, cfg):
            i += 1
            continue
        # Execution trigger: Close[t-1] > High[t-2] (break above prior candle high).
        if not (row["close"] > sig["high"].iloc[i - 1]):
            i += 1
            continue

        # Entry at THIS bar's open + slippage.
        entry = float(df["open"].iloc[i]) + slippage
        if entry <= 0:
            i += 1
            continue
        entry_ts = df.index[i]
        # Stop below the LPS swing low (or ATR-based).
        stop = _stop_price(sig, i, cfg, entry)
        if pd.isna(stop) or stop >= entry:
            i += 1
            continue
        risk_per_share = entry - stop
        r = risk_per_share
        # Position size: risk_pct * equity / risk_per_share.
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
            if exit_mode == "asymmetric":
                # 50% @ +tp_r, 50% @ +tp2_r (no SMA trailing).
                if not half_taken and high >= entry + tp_r * r:
                    half_taken = True
                    # Remainder held to tp2_r.
                if half_taken and high >= entry + tp2_r * r:
                    exit_reason = "tp2"
                    exit_px = entry + tp2_r * r
                    exit_ts = ts
                    break
            else:
                # trail_sma mode: 50% @ +tp_r, trail remainder with SMA.
                if not half_taken and high >= entry + tp_r * r:
                    half_taken = True
                trail = float(sig["trail_sma"].iloc[j]) if j < len(sig) else close
                if half_taken and low <= trail:
                    exit_reason = "trail_sma"
                    exit_px = trail
                    exit_ts = ts
                    break
            # Time stop.
            if (ts - entry_ts).days > max_hold:
                exit_reason = "time_stop"
                exit_px = close
                exit_ts = ts
                break

        if exit_reason is None:
            exit_reason = "open_end"
            exit_px = float(df["close"].iloc[n - 1])
            exit_ts = df.index[n - 1]

        # PnL: half at tp_r (if taken) + remainder at exit.
        pnl = 0.0
        if half_taken:
            pnl += 0.5 * qty * (entry + tp_r * r - entry)
            pnl += 0.5 * qty * (exit_px - entry)
        else:
            pnl = qty * (exit_px - entry)
        ret_pct = (exit_px - entry) / entry * 100.0
        r_mult = (exit_px - entry) / r if r > 0 else 0.0
        trades.append({
            "entry_ts": str(entry_ts.date()), "exit_ts": str(exit_ts.date()),
            "entry": entry, "exit": exit_px, "stop": stop,
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Wyckoff LPS swing backtest")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated symbols")
    parser.add_argument("--start", default=START_DATE)
    parser.add_argument("--end", default=END_DATE)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-wyckoff-lps")
    client = AlpacaClient()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        all_results = []
        for symbol in symbols:
            df = load_daily(client, symbol)
            if df.empty:
                logger.warning(f"No data for {symbol}")
                continue
            df = df[(df.index >= args.start) & (df.index <= args.end)]
            if df.empty:
                logger.warning(f"No data in window for {symbol}")
                continue
            logger.info(f"{symbol}: {len(df)} bars ({df.index[0].date()} to {df.index[-1].date()})")
            trades = simulate(df, dict(DEFAULT_CFG))
            stats = _stats(trades, dict(DEFAULT_CFG))
            stats["symbol"] = symbol
            stats["date_range"] = f"{df.index[0].date()} to {df.index[-1].date()}"
            all_results.append(stats)
            print_results(stats)
            if args.verbose:
                for t in trades:
                    print(f"    {t['entry_ts']}->{t['exit_ts']} entry={t['entry']:.2f} "
                          f"exit={t['exit']:.2f} stop={t['stop']:.2f} R={t['r_mult']:.2f} "
                          f"pnl=${t['pnl']:.2f} ({t['exit_reason']})")

        # Aggregate across pool.
        agg_trades = sum(r["trades"] for r in all_results)
        agg_pnl = sum(r["total_pnl_usd"] for r in all_results)
        print("\n=== POOL AGGREGATE ===")
        print(f"  symbols: {len(all_results)} | total trades: {agg_trades} | total PnL: ${agg_pnl:.2f}")

        path = os.path.join(DATA_DIR, "wyckoff_lps_results.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(all_results, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
        if not args.no_discord:
            try:
                send_discord_message(f"Wyckoff LPS backtest: {len(all_results)} symbols, "
                                     f"{agg_trades} trades, ${agg_pnl:.2f} total PnL")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Wyckoff LPS backtest failed: {e}")
        log_exception_to_jira(e, "Wyckoff LPS Backtest Failure")
        raise


if __name__ == "__main__":
    main()