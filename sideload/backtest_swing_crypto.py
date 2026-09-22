#!/usr/bin/env python3
"""Crypto swing mean-reversion backtest — adapted from the equity version.

Adapts the validated equity swing RSI-2 strategy to crypto. Key differences:
  1. NO market-hours filter (crypto trades 24/7).
  2. Slippage is PERCENTAGE-based (not $0.05 fixed) — crypto prices vary wildly
     (BTC ~$60k vs a $0.50 alt). Use 0.05% (5 bps) as the fill penalty.
  3. Sector gate uses BTC as the crypto "macro" gate (BTC > SMA200) instead of
     SMH/QQQ — BTC is the highest-liquidity crypto and leads the market.
  4. Catastrophic stop at Entry - 2.0*ATR14 (same as equity).

Usage:
    python -m sideload.backtest_swing_crypto --no-discord
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
from sideload.backtest_swing_mean_reversion import (
    load_daily, add_indicators, BASELINE_CFG,
)

logger = logging.getLogger("BacktestSwingCrypto")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

LOOKBACK_DAYS = 365 * 4  # crypto data may be shorter; 4yr

# Crypto universe: (symbol, sector_gate). BTC is the macro gate for all.
# Refined to the strong performers (SOL/LINK/DOT) after the fee analysis.
CRYPTO_UNIVERSE = [
    ("SOL/USD", "BTC/USD"),
    ("LINK/USD", "BTC/USD"),
    ("DOT/USD", "BTC/USD"),
]

MAX_SLOTS = 3
SLOT_SIZE_PCT = 0.33
EQUITY = 10000.0
# Percentage slippage (5 bps) instead of $0.05 fixed.
SLIPPAGE_PCT = 0.0005
# REAL Alpaca crypto taker fee: 25 bps per leg (50 bps round-trip).
# Applied on BOTH entry and exit (unlike US equities which are $0 commission).
FEE_PCT = 0.0025


def load_crypto_daily(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch crypto daily bars. NO market-hours filter (24/7)."""
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


def run_crypto_portfolio(client: AlpacaClient, symbols: list[tuple[str, str]],
                         max_slots: int = MAX_SLOTS,
                         slot_size_pct: float = SLOT_SIZE_PCT,
                         equity: float = EQUITY,
                         fee_pct: float = FEE_PCT) -> dict:
    """Run the 3-slot priority crypto portfolio.

    Same logic as the equity portfolio but:
      - No market-hours filter.
      - Slippage is percentage-based (SLIPPAGE_PCT).
      - Sector gate = BTC > SMA200.
      - REAL Alpaca taker fee (fee_pct per leg, applied on entry AND exit).
    """
    data = {}
    sector_data = {}
    for sym, gate in symbols:
        df = load_crypto_daily(client, sym, LOOKBACK_DAYS)
        if df.empty:
            continue
        df = add_indicators(df, dict(BASELINE_CFG))
        data[sym] = df
        if gate not in sector_data:
            gdf = load_crypto_daily(client, gate, LOOKBACK_DAYS)
            if not gdf.empty:
                sector_data[gate] = add_indicators(gdf, dict(BASELINE_CFG))

    all_days = sorted(set().union(*[set(df.index.normalize()) for df in data.values()]))
    all_days = [pd.Timestamp(d) for d in all_days]

    positions = {}
    trades = []
    slot_days = 0

    for i, day in enumerate(all_days):
        day_naive = day.tz_localize(None).normalize()
        # 1. Exits.
        for sym in list(positions.keys()):
            df = data[sym]
            day_bars = df[df.index.normalize() == day]
            if day_bars.empty:
                continue
            pos = positions[sym]
            last = day_bars.iloc[-1]
            close = float(last["close"])
            sma5 = float(last["sma5"])
            low = float(last["low"])
            exit_reason = None
            exit_px = None
            if close > sma5:
                exit_reason, exit_px = "sma5_touch", close
            elif low <= pos["cat_stop"]:
                exit_reason, exit_px = "cat_stop", pos["cat_stop"]
            elif (day_naive - pos["day0"]).days >= 5:
                exit_reason, exit_px = "time_stop", close
            if exit_reason:
                # Real Alpaca taker fee: fee_pct on entry AND exit legs.
                ret = (exit_px - pos["entry"]) / pos["entry"] - 2.0 * fee_pct
                trades.append({
                    "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
                    "exit_ts": str(day_naive.date()), "entry": pos["entry"],
                    "exit": exit_px, "ret_pct": ret * 100.0,
                    "exit_reason": exit_reason,
                    "hold_days": (day_naive - pos["day0"]).days,
                })
                slot_days += (day_naive - pos["day0"]).days
                del positions[sym]

        # 2. Entries.
        open_slots = max_slots - len(positions)
        if open_slots <= 0:
            continue
        contenders = []
        for sym in data:
            if sym in positions:
                continue
            df = data[sym]
            sig = df[["close", "rsi", "sma20", "atr", "sma_trend"]].shift(1)
            prev_idx = all_days[i - 1] if i > 0 else None
            if prev_idx is None:
                continue
            prev_naive = prev_idx.tz_localize(None).normalize()
            sig_idx = sig.index.tz_localize(None).normalize()
            sig_row = sig[sig_idx == prev_naive]
            if sig_row.empty:
                continue
            row = sig_row.iloc[-1]
            # Macro gate.
            if not (row["close"] > row["sma_trend"]):
                continue
            # Oversold.
            if not (row["rsi"] < BASELINE_CFG["rsi_buy_below"]):
                continue
            # Sector gate (BTC > SMA200).
            gate = dict(symbols)[sym]
            gdf = sector_data.get(gate)
            if gdf is None:
                continue
            g_sig = gdf[["close", "sma_trend"]].shift(1)
            g_idx = g_sig.index.tz_localize(None).normalize()
            g_row = g_sig[g_idx == prev_naive]
            if g_row.empty or not (float(g_row.iloc[-1]["close"]) > float(g_row.iloc[-1]["sma_trend"])):
                continue
            # Entry open (next day's open).
            entry_bars = df[df.index.normalize() == day]
            if entry_bars.empty:
                continue
            contenders.append({
                "symbol": sym, "rsi": float(row["rsi"]),
                "entry_open": float(entry_bars.iloc[0]["open"]),
                "atr": float(row["atr"]),
            })

        # 3. Priority: lowest RSI_2 first.
        contenders.sort(key=lambda c: c["rsi"])
        for c in contenders[:open_slots]:
            if c["entry_open"] <= 0:
                continue
            # Percentage slippage.
            entry = c["entry_open"] * (1.0 + SLIPPAGE_PCT)
            positions[c["symbol"]] = {
                "entry_ts": day, "entry": entry,
                "cat_stop": entry - 2.0 * c["atr"], "day0": day_naive,
            }

    # Close open positions.
    for sym, pos in positions.items():
        df = data[sym]
        last = float(df["close"].iloc[-1])
        ret = (last - pos["entry"]) / pos["entry"] - 2.0 * fee_pct
        trades.append({
            "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
            "exit_ts": str(all_days[-1].tz_localize(None).date()),
            "entry": pos["entry"], "exit": last, "ret_pct": ret * 100.0,
            "exit_reason": "end_of_data",
            "hold_days": (all_days[-1].tz_localize(None) - pos["day0"]).days,
        })
        slot_days += (all_days[-1].tz_localize(None) - pos["day0"]).days

    if not trades:
        return {"error": "no trades", "trades": 0}
    rets = np.array([t["ret_pct"] for t in trades])
    size_usd = slot_size_pct * equity
    pnls = rets / 100.0 * size_usd
    wins = rets > 0
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
    ann = (eq_s.iloc[-1] / eq_s.iloc[0]) ** (1.0 / (len(days_arr) / 365.0)) - 1.0
    rets_d = eq_s.pct_change().dropna()
    sharpe = float(rets_d.mean() / rets_d.std() * np.sqrt(365)) if rets_d.std() > 0 else 0.0
    calmar = ann / (max_dd / 100.0) if max_dd > 0 else 0.0
    total_days = len(days_arr)
    exposure = slot_days / (total_days * max_slots) * 100.0

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
    print(f"\n=== Crypto Swing Portfolio — {len(r['universe'])}-ticker, {r['max_slots']} slots ===")
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
    parser = argparse.ArgumentParser(description="Crypto swing mean-reversion portfolio")
    parser.add_argument("--slots", type=int, default=MAX_SLOTS)
    parser.add_argument("--fee", type=float, default=FEE_PCT,
                        help="Per-leg taker fee as fraction (default 0.0025 = 25bps). Set 0 to disable.")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-backtest-swing-crypto")
    client = AlpacaClient()

    try:
        r = run_crypto_portfolio(client, CRYPTO_UNIVERSE, max_slots=args.slots, fee_pct=args.fee)
        print_portfolio(r)
        path = os.path.join(DATA_DIR, "swing_crypto_portfolio.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
        if not args.no_discord and "error" not in r:
            try:
                send_discord_message(
                    f"Crypto swing (fee={args.fee:.4f}): {r['total_trades']} trades, exp=${r['expectancy_usd']:.2f}, "
                    f"Sharpe={r['sharpe']:.2f}, maxDD={r['max_drawdown_pct']:.2f}%, "
                    f"exposure={r['exposure_pct']:.1f}%")
            except Exception as e:
                logger.warning(f"Discord failed: {e}")
    except Exception as e:
        logger.critical(f"Crypto swing portfolio backtest failed: {e}")
        log_exception_to_jira(e, "Crypto Swing Portfolio Backtest Failure")
        raise


if __name__ == "__main__":
    main()