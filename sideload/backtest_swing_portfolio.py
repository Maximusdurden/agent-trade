#!/usr/bin/env python3
"""Expanded swing mean-reversion universe + 3-slot priority portfolio backtest.

Resolves the idle-capital problem: 6.2% exposure on a 3-name basket leaves ~94%
of buying power idle. Expanding to an 8-ticker tech/growth universe lifts
exposure toward 20-35% while capping single-slot risk at 33%.

PRIORITY CONFLICT RESOLUTION (when >3 symbols fire RSI_2<10 on the same close):
  Rank contenders by OVERSOLD DEPTH (RSI_2 lowest first). The lowest 3 get the
  capital slots; excess signals are skipped. Tie-break by distance below SMA20
  in ATR units: (Close - SMA20) / ATR14 (most stretched first).

SECTOR FILTER MAPPING:
  - Semiconductor/hardware (AMD, NVDA, TSLA, SMH): SMH > SMA200
  - Broad tech/megacap (MSFT, AAPL, AMZN, GOOGL, META, QQQ): QQQ > SMA200

Usage:
    python -m sideload.backtest_swing_portfolio --no-discord
"""

from __future__ import annotations

import argparse
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
from sideload.backtest_swing_mean_reversion import (
    load_daily, add_indicators, _signal_met, _stats, BASELINE_CFG,
)

logger = logging.getLogger("BacktestSwingPortfolio")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

LOOKBACK_DAYS = 365 * 8

# Expanded universe: (symbol, sector_filter)
# AMZN dropped: 20:1 split on 2022-06-06 is UNADJUSTED in Alpaca data (price
#   $2446->$125, ratio 19.6, volume x11) -> phantom cat_stop losses. Also weak
#   post-COVID drift. QQQ dropped as tradeable (low beta, $2.10/trade) but kept
#   as the sector gate for non-semiconductor tech.
UNIVERSE = [
    ("AMD", "SMH"),
    ("NVDA", "SMH"),
    ("TSLA", "SMH"),
    ("SMH", "SMH"),
    ("MSFT", "QQQ"),
    ("AAPL", "QQQ"),
    ("GOOGL", "QQQ"),
    ("META", "QQQ"),
]

# Max concurrent slots (positions).
MAX_SLOTS = 3
# Per-slot sizing (33% of equity).
SLOT_SIZE_PCT = 0.33
EQUITY = 10000.0


def _rsi2(close: pd.Series) -> pd.Series:
    """RSI-2 (Wilder) for oversold depth ranking."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 2, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _stretch_units(df: pd.DataFrame, ts) -> float:
    """(Close - SMA20) / ATR14 at time ts (negative = stretched below SMA20)."""
    try:
        loc = df.index.get_loc(ts)
    except Exception:
        return 0.0
    if loc < 20:
        return 0.0
    row = df.iloc[loc]
    return float((row["close"] - row["sma20"]) / row["atr"]) if row["atr"] > 0 else 0.0


def run_portfolio(client: AlpacaClient, symbols: list[tuple[str, str]],
                  max_slots: int = MAX_SLOTS, slot_size_pct: float = SLOT_SIZE_PCT,
                  equity: float = EQUITY) -> dict:
    """Run the 3-slot priority portfolio across the universe.

    Each day: compute signals at t-1 close for all symbols. If >max_slots fire,
    rank by RSI_2 (lowest first), tie-break by stretch units. Fill the top
    max_slots. Track positions with exits (SMA5 touch, cat stop, day-5 time).
    """
    # Load all data + indicators once.
    data = {}
    sector_data = {}
    for sym, sector in symbols:
        df = load_daily(client, sym, LOOKBACK_DAYS)
        if df.empty:
            continue
        df = add_indicators(df, dict(BASELINE_CFG))
        data[sym] = df
        if sector not in sector_data:
            sdf = load_daily(client, sector, LOOKBACK_DAYS)
            if not sdf.empty:
                sector_data[sector] = add_indicators(sdf, dict(BASELINE_CFG))

    # Build a unified trading-day index (union of all symbols' dates).
    all_days = sorted(set().union(*[set(df.index.normalize()) for df in data.values()]))
    all_days = [pd.Timestamp(d) for d in all_days]

    # Position tracking: {symbol: {'entry_ts','entry','cat_stop','day0'}}
    positions = {}
    trades = []
    slot_days = 0  # total slot-days in market

    for i, day in enumerate(all_days):
        day_naive = day.tz_localize(None).normalize()
        # 1. Check exits for open positions (using today's bars).
        for sym in list(positions.keys()):
            df = data[sym]
            day_bars = df[df.index.normalize() == day]
            if day_bars.empty:
                continue
            pos = positions[sym]
            # Exit checks on today's close.
            last = day_bars.iloc[-1]
            close = float(last["close"])
            sma5 = float(last["sma5"])
            low = float(last["low"])
            exit_reason = None
            exit_px = None
            if close > sma5:
                exit_reason = "sma5_touch"
                exit_px = close
            elif low <= pos["cat_stop"]:
                exit_reason = "cat_stop"
                exit_px = pos["cat_stop"]
            elif (day_naive - pos["day0"]).days >= 5:
                exit_reason = "time_stop"
                exit_px = close
            if exit_reason:
                ret = (exit_px - pos["entry"]) / pos["entry"]
                trades.append({
                    "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
                    "exit_ts": str(day_naive.date()), "entry": pos["entry"],
                    "exit": exit_px, "ret_pct": ret * 100.0,
                    "exit_reason": exit_reason,
                    "hold_days": (day_naive - pos["day0"]).days,
                })
                slot_days += (day_naive - pos["day0"]).days
                del positions[sym]

        # 2. Entry scan: compute signals at t-1 close for all symbols.
        open_slots = max_slots - len(positions)
        if open_slots <= 0:
            continue
        contenders = []
        for sym in data:
            if sym in positions:
                continue
            df = data[sym]
            # Signal at t-1 close (shifted).
            sig = df[["close", "rsi", "boll_lower", "sma20", "atr", "sma_trend",
                      "sma50", "sma_trend_5ago", "sma5"]].shift(1)
            # Find the row for the previous trading day.
            prev_idx = all_days[i - 1] if i > 0 else None
            if prev_idx is None:
                continue
            prev_naive = prev_idx.tz_localize(None).normalize()
            sig_idx = sig.index.tz_localize(None).normalize()
            sig_row = sig[sig_idx == prev_naive]
            if sig_row.empty:
                continue
            row = sig_row.iloc[-1]
            # Sector gate.
            sector = dict(UNIVERSE)[sym] if sym in dict(UNIVERSE) else "QQQ"
            sdf = sector_data.get(sector)
            if sdf is None:
                continue
            s_sig = sdf[["close", "sma_trend"]].shift(1)
            s_idx = s_sig.index.tz_localize(None).normalize()
            s_row = s_sig[s_idx == prev_naive]
            if s_row.empty:
                continue
            sector_ok = float(s_row.iloc[-1]["close"]) > float(s_row.iloc[-1]["sma_trend"])
            if not sector_ok:
                continue
            # Oversold trigger.
            if not (row["rsi"] < BASELINE_CFG["rsi_buy_below"]):
                continue
            # Macro gate.
            if not (row["close"] > row["sma_trend"]):
                continue
            # Rank by oversold depth (RSI2 lowest first), tie-break by stretch.
            contenders.append({
                "symbol": sym, "rsi": float(row["rsi"]),
                "stretch": _stretch_units(df, prev_naive),
                "entry_open": float(df[df.index.normalize() == day]["open"].iloc[0])
                              if not df[df.index.normalize() == day].empty else None,
            })

        # 3. Priority resolution: lowest RSI_2 first, tie-break by stretch.
        contenders.sort(key=lambda c: (c["rsi"], c["stretch"]))
        for c in contenders[:open_slots]:
            if c["entry_open"] is None or c["entry_open"] <= 0:
                continue
            entry = c["entry_open"] + BASELINE_CFG["slippage"]
            df = data[c["symbol"]]
            prev_naive = all_days[i - 1].tz_localize(None).normalize()
            atr_row = df[df.index.tz_localize(None).normalize() == prev_naive]
            atr = float(atr_row["atr"].iloc[-1]) if not atr_row.empty else 0.0
            positions[c["symbol"]] = {
                "entry_ts": day, "entry": entry,
                "cat_stop": entry - 2.0 * atr, "day0": day_naive,
            }

    # Close any open positions at end of data.
    for sym, pos in positions.items():
        df = data[sym]
        last = float(df["close"].iloc[-1])
        ret = (last - pos["entry"]) / pos["entry"]
        trades.append({
            "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
            "exit_ts": str(all_days[-1].tz_localize(None).date()),
            "entry": pos["entry"], "exit": last, "ret_pct": ret * 100.0,
            "exit_reason": "end_of_data",
            "hold_days": (all_days[-1].tz_localize(None) - pos["day0"]).days,
        })
        slot_days += (all_days[-1].tz_localize(None) - pos["day0"]).days

    # Aggregate.
    if not trades:
        return {"error": "no trades", "trades": 0}
    rets = np.array([t["ret_pct"] for t in trades])
    size_usd = slot_size_pct * equity
    pnls = rets / 100.0 * size_usd
    wins = rets > 0
    # Portfolio equity curve (daily).
    pnl_by_day = {}
    for t in trades:
        d = pd.Timestamp(t["exit_ts"]).tz_localize(None).normalize()
        pnl_by_day[d] = pnl_by_day.get(d, 0.0) + t["ret_pct"] / 100.0 * size_usd
    days_arr = [d.tz_localize(None).normalize() for d in all_days]
    eq = np.full(len(days_arr), equity, dtype=float)
    for k, d in enumerate(days_arr):
        if k > 0:
            eq[k] = eq[k - 1]
        if d in pnl_by_day:
            eq[k] += pnl_by_day[d]
    eq_s = pd.Series(eq, index=days_arr)
    peak = eq_s.cummax()
    dd = (eq_s - peak) / peak
    max_dd = float(abs(dd.min()) * 100.0)
    ann = (eq_s.iloc[-1] / eq_s.iloc[0]) ** (1.0 / (len(days_arr) / 252.0)) - 1.0
    rets_d = eq_s.pct_change().dropna()
    sharpe = float(rets_d.mean() / rets_d.std() * np.sqrt(252)) if rets_d.std() > 0 else 0.0
    calmar = ann / (max_dd / 100.0) if max_dd > 0 else 0.0
    total_days = len(days_arr)
    exposure = slot_days / (total_days * max_slots) * 100.0

    # Per-symbol breakdown.
    by_sym = {}
    for sym in dict.fromkeys(t["symbol"] for t in trades):
        st = [t for t in trades if t["symbol"] == sym]
        sr = np.array([t["ret_pct"] for t in st])
        by_sym[sym] = {
            "trades": len(st), "win": float((sr > 0).mean()),
            "exp": float((sr / 100 * size_usd).mean()),
            "total": float((sr / 100 * size_usd).sum()),
        }

    return {
        "universe": [s for s, _ in symbols],
        "max_slots": max_slots, "slot_size_pct": slot_size_pct,
        "total_trades": len(trades), "win_rate": float(wins.mean()),
        "expectancy_usd": float(pnls.mean()), "total_pnl_usd": float(pnls.sum()),
        "max_drawdown_pct": max_dd, "annualized_return_pct": ann * 100.0,
        "sharpe": sharpe, "calmar": calmar,
        "exposure_pct": exposure, "total_days": total_days,
        "by_symbol": by_sym,
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in set(t["exit_reason"] for t in trades)},
    }


def print_portfolio(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    print(f"\n=== Swing Portfolio — {len(r['universe'])}-ticker universe, {r['max_slots']} slots ===")
    print(f"  universe: {', '.join(r['universe'])}")
    print(f"  total trades: {r['total_trades']} | win rate: {r['win_rate']:.1%}")
    print(f"  expectancy: ${r['expectancy_usd']:.2f}/trade | total: ${r['total_pnl_usd']:.2f}")
    print(f"  max drawdown: {r['max_drawdown_pct']:.2f}% | annualized return: {r['annualized_return_pct']:.2f}%")
    print(f"  Sharpe: {r['sharpe']:.2f} | Calmar: {r['calmar']:.2f}")
    print(f"  exposure: {r['exposure_pct']:.1f}% of slot-days in market")
    print(f"  exits: {r['exit_reasons']}")
    print("  per-symbol:")
    for sym, s in r["by_symbol"].items():
        print(f"    {sym}: trades={s['trades']} win={s['win']:.1%} exp=${s['exp']:.2f} total=${s['total']:.0f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Expanded swing mean-reversion portfolio")
    parser.add_argument("--slots", type=int, default=MAX_SLOTS)
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-swing-portfolio")
    client = AlpacaClient()

    try:
        r = run_portfolio(client, UNIVERSE, max_slots=args.slots)
        print_portfolio(r)
        path = os.path.join(DATA_DIR, "swing_portfolio.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
        if not args.no_discord and "error" not in r:
            try:
                send_discord_message(
                    f"Swing portfolio: {r['total_trades']} trades, exp=${r['expectancy_usd']:.2f}, "
                    f"Sharpe={r['sharpe']:.2f}, maxDD={r['max_drawdown_pct']:.2f}%, "
                    f"exposure={r['exposure_pct']:.1f}%")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Swing portfolio backtest failed: {e}")
        log_exception_to_jira(e, "Swing Portfolio Backtest Failure")
        raise


if __name__ == "__main__":
    main()