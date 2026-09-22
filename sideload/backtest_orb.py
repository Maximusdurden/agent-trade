#!/usr/bin/env python3
"""AMD Opening Range Breakout (ORB) backtest — underlying equity, no options.

The directional (daily + intraday) hypotheses were falsified by leakage, and the
amplitude correlation (0.289) is too weak to support a long straddle/strangle
(IV crush on a long straddle is doubly exposed). This backtest tests the ORB
strategy on the UNDERLYING EQUITY, which avoids IV crush and double bid-ask
spread entirely.

ORB logic (per trading day):
  1. FILTER GATE: only arm the bracket on days where |open_to_945_pct| exceeds a
     high threshold (extreme morning momentum -> trend day?).
  2. RANGE: the 9:30-9:45 AM high/low defines the opening range.
  3. EXECUTION: place OCO (One-Cancels-Other) stop-entries slightly OUTSIDE the
     range (breakout above -> long; breakdown below -> short).
  4. RISK: hard stop-loss (standard ORB = opposite side of range; we also test a
     tighter mid-range stop for better risk/reward).
  5. EXIT: fixed reward-to-risk target (e.g. 2R) vs time-based 4:00 PM close.

If this fails to clear a meaningful expectancy threshold, the 9:45 AM intraday
window is abandoned — proving morning price action is too efficiently priced.

Usage:
    python -m sideload.backtest_orb --all
    python -m sideload.backtest_orb --all --symbol NVDA
    python -m sideload.backtest_orb --grid   # sweep filter/stop/exit params
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestORB")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

INTRADAY_INTERVAL = "5min"
# Opening range window (ET).
RANGE_START = dtime(9, 30)
RANGE_END = dtime(9, 45)
# Session close (ET).
SESSION_CLOSE = dtime(16, 0)

# Default config (overridable via --grid).
DEFAULT_CFG = {
    "vel_threshold_pct": 1.0,   # |open_to_945| must exceed this to arm
    "breakout_bps": 0.0,        # extra buffer outside the range (in %)
    "stop_mode": "range",       # 'range' (opposite side) or 'mid' (mid-range)
    "stop_bps": 0.0,            # extra stop buffer beyond the range side (in %)
    "reward_risk": 2.0,         # take-profit = reward_risk * risk
    "exit_mode": "2r_or_close", # '2r_or_close' or 'close_only'
    "size_pct": 0.10,           # position size as % of equity
    "equity": 10000.0,
    # --- Confirmed-close experiment (Drift Desk idea) ---
    "confirmed_close": False,   # wait for a bar to CLOSE outside the range
    "slippage": 0.03,           # $ per share adverse fill (AMD ~0.02-0.05)
    "vol_mult": 1.0,            # breakout bar volume must be >= vol_mult * avg range vol
    "fill_mode": "next_open",  # 'next_open' (next bar open) or 'bar_close'
}

# Grid sweep for curve-fitting guardrails.
GRID = {
    "vel_threshold_pct": [0.5, 1.0, 1.5],
    "breakout_bps": [0.0, 0.25, 0.5],
    "stop_mode": ["range", "mid"],
    "reward_risk": [1.5, 2.0, 3.0],
    "exit_mode": ["2r_or_close", "close_only"],
}


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch intraday OHLCV bars, return a frame with an ET DatetimeIndex."""
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
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _day_bars(intraday: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """Return the bars for a single ET day."""
    day_et = day.tz_convert(ET) if day.tzinfo is not None else day.tz_localize(ET)
    start = day_et.replace(hour=9, minute=0, second=0, microsecond=0)
    end = day_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return intraday[(intraday.index >= start) & (intraday.index <= end)]


def _range_high_low(day_bars: pd.DataFrame) -> tuple[float, float] | None:
    """High/low of the 9:30-9:45 opening range."""
    start = day_bars.index[0].replace(hour=RANGE_START.hour, minute=RANGE_START.minute)
    end = day_bars.index[0].replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    window = day_bars[(day_bars.index >= start) & (day_bars.index <= end)]
    if window.empty:
        return None
    return float(window["high"].max()), float(window["low"].min())


def _open_to_945(day_bars: pd.DataFrame) -> float | None:
    """Open-to-9:45 momentum % (the filter gate)."""
    if day_bars.empty:
        return None
    o = float(day_bars["open"].iloc[0])
    end = day_bars.index[0].replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    window = day_bars[day_bars.index <= end]
    if window.empty or o <= 0:
        return None
    p945 = float(window["close"].iloc[-1])
    return (p945 / o - 1.0) * 100.0


def _range_avg_volume(day_bars: pd.DataFrame) -> float | None:
    """Average volume of the 9:30-9:45 opening range bars (for the volume filter)."""
    if day_bars.empty or "volume" not in day_bars.columns:
        return None
    start = day_bars.index[0].replace(hour=RANGE_START.hour, minute=RANGE_START.minute)
    end = day_bars.index[0].replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    window = day_bars[(day_bars.index >= start) & (day_bars.index <= end)]
    if window.empty:
        return None
    return float(window["volume"].mean())


def simulate_day(day_bars: pd.DataFrame, cfg: dict) -> dict | None:
    """Simulate one ORB day. Returns trade result or None if no trade.

    Logic:
      - Filter gate: |open_to_945| must exceed vel_threshold_pct.
      - Range: 9:30-9:45 high/low.
      - OCO stop-entries just outside the range (breakout_bps buffer).
      - On trigger, enter long (above) or short (below).
      - Hard stop on the opposite side of the range (or mid-range).
      - Exit at reward_risk * risk (take-profit) or 4:00 PM close.
    """
    if day_bars.empty:
        return None
    vel = _open_to_945(day_bars)
    if vel is None or abs(vel) < cfg["vel_threshold_pct"]:
        return None  # filter gate not met

    rng = _range_high_low(day_bars)
    if rng is None:
        return None
    hi, lo = rng
    if hi <= 0 or lo <= 0:
        return None

    # OCO stop-entry levels (slightly outside the range).
    buf = cfg["breakout_bps"] / 100.0
    long_entry = hi * (1.0 + buf)
    short_entry = lo * (1.0 - buf)

    # Risk = distance from entry to the stop.
    if cfg["stop_mode"] == "mid":
        mid = (hi + lo) / 2.0
        long_stop = mid
        short_stop = mid
    else:  # 'range' — opposite side of the range
        long_stop = lo
        short_stop = hi
    stop_buf = cfg["stop_bps"] / 100.0
    long_stop = long_stop * (1.0 - stop_buf)
    short_stop = short_stop * (1.0 + stop_buf)

    # Walk the post-9:45 bars to find the trigger, then the exit.
    start = day_bars.index[0].replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    post = day_bars[day_bars.index > start]
    if post.empty:
        return None

    position = None  # {'dir','entry','risk','target'}
    for ts, bar in post.iterrows():
        px = float(bar["close"])
        if position is None:
            # OCO: first side to break triggers.
            if px >= long_entry:
                risk = long_entry - long_stop
                if risk <= 0:
                    return None
                position = {"dir": "long", "entry": long_entry, "risk": risk,
                            "target": long_entry + cfg["reward_risk"] * risk}
            elif px <= short_entry:
                risk = short_stop - short_entry
                if risk <= 0:
                    return None
                position = {"dir": "short", "entry": short_entry, "risk": risk,
                            "target": short_entry - cfg["reward_risk"] * risk}
        else:
            # Exit checks.
            if position["dir"] == "long":
                if px >= position["target"]:
                    return _result(position, "take_profit", ts)
                if px <= long_stop:
                    return _result(position, "stop_loss", ts)
            else:
                if px <= position["target"]:
                    return _result(position, "take_profit", ts)
                if px >= short_stop:
                    return _result(position, "stop_loss", ts)

    # Time exit at 4:00 PM close.
    if position is not None:
        last = float(post["close"].iloc[-1])
        if position["dir"] == "long":
            pnl = (last - position["entry"]) / position["entry"]
        else:
            pnl = (position["entry"] - last) / position["entry"]
        return {
            "direction": position["dir"],
            "entry": position["entry"],
            "exit": last,
            "exit_reason": "close",
            "ret_pct": pnl * 100.0,
            "risk_pct": position["risk"] / position["entry"] * 100.0,
        }
    return None


def simulate_day_confirmed(day_bars: pd.DataFrame, cfg: dict) -> dict | None:
    """Confirmed-close ORB day with a fresh-flips-only state machine.

    Unlike the touch model (OCO stop-entry filled at the range level), this
    waits for a bar to CLOSE outside the opening range, then enters at the
    NEXT bar's open (or the breakout bar's close) plus slippage. This models
    the worse fill on volatile breakouts.

    Fresh-flips-only: if a breakout bar closes outside the band while the
    volume filter is RED, the setup is permanently KILLED for the day. It never
    fires later just because the filter turned green while price hovered
    outside the range (blocked flips are not queued).
    """
    if day_bars.empty:
        return None
    vel = _open_to_945(day_bars)
    if vel is None or abs(vel) < cfg["vel_threshold_pct"]:
        return None

    rng = _range_high_low(day_bars)
    if rng is None:
        return None
    hi, lo = rng
    if hi <= 0 or lo <= 0:
        return None

    buf = cfg["breakout_bps"] / 100.0
    long_level = hi * (1.0 + buf)
    short_level = lo * (1.0 - buf)

    # Stop levels (opposite side or mid-range).
    if cfg["stop_mode"] == "mid":
        mid = (hi + lo) / 2.0
        long_stop = mid
        short_stop = mid
    else:
        long_stop = lo
        short_stop = hi
    stop_buf = cfg["stop_bps"] / 100.0
    long_stop = long_stop * (1.0 - stop_buf)
    short_stop = short_stop * (1.0 + stop_buf)

    # Volume filter reference (avg opening-range volume).
    avg_vol = _range_avg_volume(day_bars)
    vol_mult = cfg.get("vol_mult", 1.0)
    slip = cfg.get("slippage", 0.0)
    fill_mode = cfg.get("fill_mode", "next_open")

    start = day_bars.index[0].replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    post = day_bars[day_bars.index > start]
    if post.empty:
        return None

    bars = list(post.iterrows())
    killed = False
    position = None
    i = 0
    while i < len(bars):
        ts, bar = bars[i]
        px = float(bar["close"])
        if position is None and not killed:
            # Look for a confirmed close outside the range.
            if px >= long_level or px <= short_level:
                # Fresh-flips-only: check the volume filter at THIS bar.
                vol_ok = True
                if avg_vol is not None and avg_vol > 0 and "volume" in bar:
                    vol_ok = float(bar["volume"]) >= vol_mult * avg_vol
                if not vol_ok:
                    # Filter red -> setup permanently killed for the day.
                    killed = True
                    i += 1
                    continue
                direction = "long" if px >= long_level else "short"
                if fill_mode == "bar_close":
                    entry = px + slip if direction == "long" else px - slip
                else:  # next_open
                    if i + 1 < len(bars):
                        nbar = bars[i + 1][1]
                        entry = float(nbar["open"]) + slip if direction == "long" else float(nbar["open"]) - slip
                    else:
                        entry = px + slip if direction == "long" else px - slip
                if entry <= 0:
                    return None
                if direction == "long":
                    risk = entry - long_stop
                    if risk <= 0:
                        return None
                    target = entry + cfg["reward_risk"] * risk
                else:
                    risk = short_stop - entry
                    if risk <= 0:
                        return None
                    target = entry - cfg["reward_risk"] * risk
                position = {"dir": direction, "entry": entry, "risk": risk,
                            "target": target}
                i += 1
                continue
        elif position is not None:
            # Exit checks.
            if position["dir"] == "long":
                if px >= position["target"]:
                    return _result(position, "take_profit", ts)
                if px <= long_stop:
                    return _result(position, "stop_loss", ts)
            else:
                if px <= position["target"]:
                    return _result(position, "take_profit", ts)
                if px >= short_stop:
                    return _result(position, "stop_loss", ts)
        i += 1

    # Time exit at 4:00 PM close.
    if position is not None:
        last = float(post["close"].iloc[-1])
        if position["dir"] == "long":
            pnl = (last - position["entry"]) / position["entry"]
        else:
            pnl = (position["entry"] - last) / position["entry"]
        return {
            "direction": position["dir"],
            "entry": position["entry"],
            "exit": last,
            "exit_reason": "close",
            "ret_pct": pnl * 100.0,
            "risk_pct": position["risk"] / position["entry"] * 100.0,
        }
    return None


def _result(position: dict, reason: str, ts) -> dict:
    """Build a trade result. PnL is relative to the entry price.

    - take_profit: gain = reward_risk * risk (positive).
    - stop_loss:   loss = -risk (negative, both directions).
    """
    entry = position["entry"]
    risk = position["risk"]
    if reason == "take_profit":
        ret = position["target"] - entry
        exit_px = position["target"]
    else:  # stop_loss
        ret = -risk
        exit_px = entry - risk if position["dir"] == "long" else entry + risk
    return {
        "direction": position["dir"],
        "entry": entry,
        "exit": exit_px,
        "exit_reason": reason,
        "ret_pct": ret / entry * 100.0,
        "risk_pct": risk / entry * 100.0,
    }


def run_backtest(intraday: pd.DataFrame, symbol: str, cfg: dict) -> dict:
    """Run the ORB backtest over all days. Returns aggregate stats.

    ``intraday`` is a pre-fetched frame (ET DatetimeIndex) so the grid sweep
    fetches data ONCE instead of once per config.
    """
    if intraday.empty:
        return {"error": "no intraday data", "trades": 0}

    days = sorted(set(intraday.index.date))
    trades = []
    armed = 0
    for d in days:
        day_ts = pd.Timestamp(d).tz_localize(ET)
        day_bars = _day_bars(intraday, day_ts)
        if day_bars.empty:
            continue
        vel = _open_to_945(day_bars)
        if vel is not None and abs(vel) >= cfg["vel_threshold_pct"]:
            armed += 1
        if cfg.get("confirmed_close"):
            t = simulate_day_confirmed(day_bars, cfg)
        else:
            t = simulate_day(day_bars, cfg)
        if t is not None:
            t["date"] = str(d)
            trades.append(t)

    if not trades:
        return {"error": "no trades triggered", "trades": 0, "armed_days": armed, "total_days": len(days)}

    rets = np.array([t["ret_pct"] for t in trades])
    wins = rets > 0
    # Expectancy in USD on the configured position size.
    size_usd = cfg["size_pct"] * cfg["equity"]
    pnls = rets / 100.0 * size_usd
    return {
        "symbol": symbol,
        "total_days": len(days),
        "armed_days": armed,
        "trades": len(trades),
        "trade_rate": len(trades) / armed if armed else 0.0,
        "win_rate": float(wins.mean()) if len(wins) else 0.0,
        "mean_ret_pct": float(rets.mean()),
        "median_ret_pct": float(np.median(rets)),
        "expectancy_usd": float(pnls.mean()),
        "total_pnl_usd": float(pnls.sum()),
        "mean_risk_pct": float(np.mean([t["risk_pct"] for t in trades])),
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in set(t["exit_reason"] for t in trades)},
        "long_trades": sum(1 for t in trades if t["direction"] == "long"),
        "short_trades": sum(1 for t in trades if t["direction"] == "short"),
        "cfg": cfg,
    }


def run_grid(client: AlpacaClient, symbol: str, days_back: int) -> list[dict]:
    """Sweep the grid to find the best config (curve-fitting guardrails).

    Fetches intraday data ONCE, then runs all configs against the same frame.
    """
    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        return []
    results = []
    keys = list(GRID.keys())
    for combo in itertools.product(*[GRID[k] for k in keys]):
        cfg = dict(DEFAULT_CFG)
        cfg.update(dict(zip(keys, combo)))
        r = run_backtest(intraday, symbol, cfg)
        if "error" not in r:
            results.append(r)
    results.sort(key=lambda x: -x["expectancy_usd"])
    return results


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def print_results(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    print(f"\n=== ORB Backtest — {r['symbol']} ===")
    print(f"  total days: {r['total_days']} | armed: {r['armed_days']} | trades: {r['trades']} ({r['trade_rate']:.1%} of armed)")
    print(f"  win rate: {r['win_rate']:.1%} | mean ret: {_fmt(r['mean_ret_pct'])}% | median: {_fmt(r['median_ret_pct'])}%")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print(f"  mean risk: {_fmt(r['mean_risk_pct'])}% | long/short: {r['long_trades']}/{r['short_trades']}")
    print(f"  exits: {r['exit_reasons']}")
    print(f"  cfg: {r['cfg']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD ORB breakout-bracket backtest")
    parser.add_argument("--symbol", default="AMD")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--grid", action="store_true", help="Sweep the parameter grid")
    parser.add_argument("--all", action="store_true", help="Run default config")
    parser.add_argument("--confirmed-close", action="store_true",
                        help="Use confirmed-close entry (bar closes outside range) with realistic fill + slippage")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-orb")
    client = AlpacaClient()
    symbol = args.symbol.upper()

    try:
        intraday = load_intraday(client, symbol, args.days)
        if intraday.empty:
            logger.error("No intraday data.")
            return
        if args.grid:
            results = run_grid(client, symbol, args.days)
            path = os.path.join(DATA_DIR, f"{symbol.lower()}_orb_grid.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, default=str)
            logger.info(f"Grid: {len(results)} configs -> {path}")
            print("\n=== TOP 10 ORB CONFIGS (by expectancy) ===")
            for r in results[:10]:
                print(f"  exp=${r['expectancy_usd']:.2f} win={r['win_rate']:.1%} trades={r['trades']} "
                      f"cfg={r['cfg']}")
            if not args.no_discord:
                try:
                    send_discord_message(f"ORB grid done: {len(results)} configs, best exp=${results[0]['expectancy_usd']:.2f}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
        else:
            cfg = dict(DEFAULT_CFG)
            if args.confirmed_close:
                cfg["confirmed_close"] = True
            r = run_backtest(intraday, symbol, cfg)
            print_results(r)
            path = os.path.join(DATA_DIR, f"{symbol.lower()}_orb_default.json")
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(r, fh, indent=2, default=str)
            logger.info(f"Wrote {path}")
            if not args.no_discord:
                try:
                    send_discord_message(f"ORB default: exp=${r.get('expectancy_usd', 0):.2f} win={r.get('win_rate', 0):.1%}")
                except Exception as e:
                    logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"ORB backtest failed: {e}")
        log_exception_to_jira(e, "ORB Backtest Failure")
        raise


if __name__ == "__main__":
    main()