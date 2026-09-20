#!/usr/bin/env python3
"""SPY/AMD/TSLA stock backtest — 3-bucket entry rule, short holds.

Phase B: trade the UNDERLYING STOCK directly (no option premium/cost). The
3-bucket rule's directional edge (~56% win) was real but too small to clear
option round-trip costs. On the stock there's no premium — you just buy/sell
shares — so the edge may be profitable.

Entry rule (3-bucket framework):
  - LONG  when price at 10:00 ET > prior day's HIGH   (strong bucket)
  - SHORT when price at 10:00 ET < prior day's LOW    (bad bucket)
  - SKIP  otherwise (weak-normal bucket, ~coin flip)

Exit model (short holds — "we don't stay long"):
  - Sweep target% / stop% / trailing-giveback% / time-cap.
  - Whichever fires first closes the position.

PnL model (stock, no option costs):
  - PnL = (exit_price - entry_price) * qty for LONG
  - PnL = (entry_price - exit_price) * qty for SHORT
  - qty sized by a fixed allocation % of equity (default 10%).

Usage:
    python -m sideload.backtest_anchor_stock --symbol SPY --days 730
    python -m sideload.backtest_anchor_stock --symbol SPY --days 730 --no-discord
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from sideload.anchor_intraday_dataset import (
    load_daily, load_intraday, _price_at_10am, ET,
)
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestAnchorStock")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
SUMMARY_PATH = os.path.join(OUT_DIR, "anchor_stock_backtest.json")

# Sizing: % of equity per position.
DEFAULT_ALLOC_PCT = 0.10
DEFAULT_EQUITY = 10000.0

# Exit grid to sweep.
TARGET_PCTS = [0.001, 0.002, 0.004, 0.006, 0.008, 0.010]   # 0.1% .. 1.0%
STOP_PCTS = [0.001, 0.002, 0.004, 0.006, 0.008, 0.010]
GIVEBACK_PCTS = [0.0, 0.001, 0.002, 0.003, 0.004, 0.005]   # 0 = disabled
TIME_CAPS = ["10:30", "11:00", "12:00", "13:00"]  # ET exit-by times


def _time_to_ts(day: pd.Timestamp, hhmm: str) -> pd.Timestamp:
    h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def build_daily_frame(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Build a per-day frame with prev high/low/close, open, 10:00 price, and
    the full intraday series for exit simulation."""
    daily = load_daily(client, symbol, limit=days_back)
    if len(daily) < 30:
        return pd.DataFrame()
    daily_et = daily.copy()
    daily_et.index = pd.to_datetime(daily_et.index)
    if daily_et.index.tzinfo is None:
        daily_et.index = daily_et.index.tz_localize("UTC")
    daily_et.index = daily_et.index.tz_convert(ET)
    daily_et["et_date"] = daily_et.index.date

    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        return pd.DataFrame()

    rows = []
    dates = daily_et["et_date"].tolist()
    for i in range(1, len(dates)):
        cur_date = dates[i]
        prev_high = float(daily_et["high"].iloc[i - 1])
        prev_low = float(daily_et["low"].iloc[i - 1])
        prev_close = float(daily_et["close"].iloc[i - 1])
        cur_open = float(daily_et["open"].iloc[i])
        cur_ts = daily_et.index[i]
        p10 = _price_at_10am(intraday, cur_ts)
        if p10 is None:
            continue
        rows.append({
            "date": str(cur_date),
            "prev_high": prev_high,
            "prev_low": prev_low,
            "prev_close": prev_close,
            "open": cur_open,
            "p10": p10,
            "day_ts": cur_ts,
        })
    return pd.DataFrame(rows)


def _entry_side(row: pd.Series) -> str | None:
    """Return 'long' / 'short' / None based on the 3-bucket rule."""
    if row["p10"] > row["prev_high"]:
        return "long"
    if row["p10"] < row["prev_low"]:
        return "short"
    return None


def simulate_day(row: pd.Series, intraday: pd.DataFrame, side: str,
                 target_pct: float, stop_pct: float, cap_hhmm: str,
                 giveback_pct: float, alloc_pct: float, equity: float) -> dict:
    """Simulate one stock round-trip for a day. Returns stats dict.

    Exit model: enter at 10:00, LET IT RUN, exit on a DIP (trailing-stop
    giveback from the peak/trough), with hard target/stop floors and a safety
    time-cap. Whichever fires first closes.
    """
    entry_price = row["p10"]
    entry_ts = row["day_ts"].replace(hour=10, minute=0, second=0, microsecond=0)
    cap_ts = _time_to_ts(row["day_ts"], cap_hhmm)

    window = intraday[(intraday.index >= entry_ts) & (intraday.index <= cap_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    exit_price = None
    exit_reason = "time_cap"
    extreme = entry_price
    for ts, bar in window.iterrows():
        px = float(bar["close"])
        if side == "long":
            extreme = max(extreme, px)
            if target_pct > 0 and (px - entry_price) / entry_price >= target_pct:
                exit_price, exit_reason = px, "target"
                break
            if stop_pct > 0 and (entry_price - px) / entry_price >= stop_pct:
                exit_price, exit_reason = px, "stop"
                break
            if giveback_pct > 0 and extreme > entry_price:
                if (extreme - px) / extreme >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        else:  # short
            extreme = min(extreme, px)
            if target_pct > 0 and (entry_price - px) / entry_price >= target_pct:
                exit_price, exit_reason = px, "target"
                break
            if stop_pct > 0 and (px - entry_price) / entry_price >= stop_pct:
                exit_price, exit_reason = px, "stop"
                break
            if giveback_pct > 0 and extreme < entry_price:
                if (px - extreme) / extreme >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        exit_price = px
    if exit_price is None:
        return {"traded": False, "reason": "no_data"}

    # Stock PnL (no option costs).
    qty = (alloc_pct * equity) / entry_price
    if side == "long":
        pnl = (exit_price - entry_price) * qty
        move = (exit_price - entry_price) / entry_price
    else:
        pnl = (entry_price - exit_price) * qty
        move = (entry_price - exit_price) / entry_price

    return {
        "traded": True,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "move_pct": move * 100.0,
        "pnl": pnl,
    }


def run_backtest(client: AlpacaClient, symbol: str, days_back: int,
                 alloc_pct: float, equity: float) -> dict:
    """Run the full grid sweep. Returns {config_key: stats}."""
    daily = build_daily_frame(client, symbol, days_back)
    if daily.empty:
        logger.error(f"No daily frame for {symbol}.")
        return {}

    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.error(f"No intraday bars for {symbol}.")
        return {}

    daily["side"] = daily.apply(_entry_side, axis=1)
    n_long = int((daily["side"] == "long").sum())
    n_short = int((daily["side"] == "short").sum())
    n_skip = int((daily["side"].isna()).sum())

    results = {}
    for target_pct, stop_pct, cap, giveback in itertools.product(
            TARGET_PCTS, STOP_PCTS, TIME_CAPS, GIVEBACK_PCTS):
        trades = []
        for _, row in daily.iterrows():
            side = row["side"]
            if side is None or (isinstance(side, float) and np.isnan(side)):
                continue
            res = simulate_day(row, intraday, side, target_pct, stop_pct, cap,
                               giveback, alloc_pct, equity)
            if res.get("traded"):
                trades.append(res)
        if not trades:
            continue
        pnls = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = float(sum(pnls))
        results[f"t{target_pct:.3f}_s{stop_pct:.3f}_g{giveback:.3f}_{cap}"] = {
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "giveback_pct": giveback,
            "time_cap": cap,
            "trades": len(trades),
            "longs": sum(1 for t in trades if t["side"] == "long"),
            "shorts": sum(1 for t in trades if t["side"] == "short"),
            "win_rate": wins / len(trades),
            "total_pnl": total_pnl,
            "expectancy": total_pnl / len(trades),
            "exit_reasons": {
                r: sum(1 for t in trades if t["exit_reason"] == r)
                for r in set(t["exit_reason"] for t in trades)
            },
        }

    return {
        "symbol": symbol,
        "n_days": int(len(daily)),
        "n_long": n_long,
        "n_short": n_short,
        "n_skip": n_skip,
        "alloc_pct": alloc_pct,
        "equity": equity,
        "configs": results,
    }


def _notify_discord(summary: dict) -> None:
    try:
        configs = summary["configs"]
        if not configs:
            send_discord_message(f"{summary['symbol']} stock backtest: no configs.")
            return
        best = max(configs.values(), key=lambda c: c["expectancy"])
        lines = [
            f"**{summary['symbol']} STOCK BACKTEST — {summary['n_days']} days**",
            f"Entry rule (3-bucket): {summary['n_long']} longs (10:00>prevH), "
            f"{summary['n_short']} shorts (10:00<prevL), {summary['n_skip']} skips.",
            f"Sizing: {summary['alloc_pct']:.0%} of ${summary['equity']:.0f} per position.",
            "",
            f"**Best config:** {best['time_cap']} cap, target {best['target_pct']:.1%}, "
            f"stop {best['stop_pct']:.1%}, giveback {best['giveback_pct']:.1%}",
            f"  → expectancy **${best['expectancy']:.2f}/trade**, "
            f"win {best['win_rate']:.0%}, {best['trades']} trades, "
            f"total ${best['total_pnl']:.0f}",
            f"  → exits: {best['exit_reasons']}",
            "",
            "**Bottom line:** " + (
                "The 3-bucket rule IS profitable on the stock (no option cost "
                "hurdle). This is the vehicle to pursue."
                if best["expectancy"] > 0 else
                "Still negative on the stock — the directional edge is too weak "
                "even without option costs. Revisit the entry rule."
            ),
        ]
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/AMD/TSLA stock backtest")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--alloc-pct", type=float, default=DEFAULT_ALLOC_PCT)
    parser.add_argument("--equity", type=float, default=DEFAULT_EQUITY)
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-anchor-stock")
    client = AlpacaClient()

    try:
        summary = run_backtest(client, args.symbol.upper(), args.days,
                               args.alloc_pct, args.equity)
        if not summary or not summary.get("configs"):
            logger.error("No configs produced.")
            return
        with open(SUMMARY_PATH, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info(f"Wrote {SUMMARY_PATH}")

        print(f"\n=== {summary['symbol']} STOCK BACKTEST ({summary['n_days']} days) ===")
        print(f"Entry: {summary['n_long']} longs, {summary['n_short']} shorts, "
              f"{summary['n_skip']} skips | alloc={summary['alloc_pct']:.0%} "
              f"equity=${summary['equity']:.0f}")
        print(f"{'time_cap':<9}{'target':>8}{'stop':>8}{'giveback':>9}{'trades':>7}"
              f"{'win':>7}{'expectancy':>11}{'total_pnl':>12}")
        print("-" * 72)
        ranked = sorted(summary["configs"].values(), key=lambda c: c["expectancy"], reverse=True)
        for c in ranked[:15]:
            print(f"{c['time_cap']:<9}{c['target_pct']:>7.1%}{c['stop_pct']:>7.1%}"
                  f"{c['giveback_pct']:>8.1%}{c['trades']:>7}{c['win_rate']:>6.0%}"
                  f"{c['expectancy']:>10.2f}{c['total_pnl']:>11.2f}")
        if not args.no_discord:
            _notify_discord(summary)
    except Exception as e:
        logger.critical(f"Stock backtest failed: {e}")
        log_exception_to_jira(e, "Anchor Stock Backtest Failure")
        raise


if __name__ == "__main__":
    main()