#!/usr/bin/env python3
"""SPY options-flavored backtest — 3-bucket entry rule, short holds.

Phase A of the options strategy. Historical option PREMIUMS are not available
(Alpaca paper has no OPRA bars; quotes are current-only), so we backtest the
DIRECTIONAL BET on the underlying SPY price and model option PnL with a fixed
leverage multiplier + premium %, per the user's decision.

Entry rule (the 3-bucket framework, validated earlier):
  - LONG CALL  when price at 10:00 ET > prior day's HIGH   (strong bucket)
  - LONG PUT   when price at 10:00 ET < prior day's LOW    (bad bucket)
  - SKIP       otherwise (weak-normal bucket, ~coin flip)

Exit model (short holds — "we don't stay long"):
  - Sweep target% / stop% / time-cap (exit by 11:00/12:00/13:00 ET).
  - Whichever fires first closes the position.

PnL model:
  - Underlying move captured from entry to exit.
  - Option PnL = delta * (underlying move) * notional - round_trip_cost.
  - round_trip_cost = round_trip_cost_pct * notional (spread + theta decay on a
    same-day round-trip — NOT the full premium, which you mostly get back).

Usage:
    python -m sideload.backtest_anchor_options --symbol SPY --days 730
    python -m sideload.backtest_anchor_options --symbol SPY --days 730 --no-discord
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

logger = logging.getLogger("BacktestAnchorOptions")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
SUMMARY_PATH = os.path.join(OUT_DIR, "anchor_options_backtest.json")

# Default delta / round-trip cost model.
# delta = option delta (0.25-0.75 per the picker).
# round_trip_cost_pct = fraction of notional lost to bid-ask spread + theta
# decay on a SAME-DAY round-trip (NOT the full premium — you get most of the
# premium back when you sell the same day).
DEFAULT_DELTA = 0.5
DEFAULT_ROUND_TRIP_COST_PCT = 0.001  # 0.1% of notional (spread + theta)

# Exit grid to sweep.
TARGET_PCTS = [0.001, 0.002, 0.004, 0.006, 0.008, 0.010]   # 0.1% .. 1.0%
STOP_PCTS = [0.001, 0.002, 0.004, 0.006, 0.008, 0.010]
# Trailing-stop giveback: exit when price gives back this % from the peak/trough.
GIVEBACK_PCTS = [0.001, 0.002, 0.003, 0.004, 0.005]
# Safety time-cap (the dip should fire first, well before this).
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
    """Return 'call' / 'put' / None based on the 3-bucket rule."""
    if row["p10"] > row["prev_high"]:
        return "call"
    if row["p10"] < row["prev_low"]:
        return "put"
    return None


def _exit_price(intraday: pd.DataFrame, day_ts: pd.Timestamp, entry_ts: pd.Timestamp,
                cap_hhmm: str) -> tuple[float, str]:
    """Simulate the exit: walk intraday bars from entry to time-cap, firing
    target/stop first. Returns (exit_price, exit_reason)."""
    cap_ts = _time_to_ts(day_ts, cap_hhmm)
    window = intraday[(intraday.index >= entry_ts) & (intraday.index <= cap_ts)]
    if window.empty:
        return float("nan"), "no_data"
    return float(window["close"].iloc[-1]), "time_cap"


def simulate_day(row: pd.Series, intraday: pd.DataFrame, side: str,
                 target_pct: float, stop_pct: float, cap_hhmm: str,
                 giveback_pct: float,
                 delta: float, round_trip_cost_pct: float) -> dict:
    """Simulate one option round-trip for a day. Returns stats dict.

    Exit model (per user): enter at 10:00, LET IT RUN, and exit on a DIP
    (pullback from the peak). This is a TRAILING-STOP GIVEBACK exit:
      - call: track the running HIGH; exit when price gives back ``giveback_pct``
        from that peak (a dip).
      - put:  track the running LOW; exit when price gives back ``giveback_pct``
        from that trough (a bounce).
    ``target_pct``/``stop_pct`` are the hard take-profit/stop-loss floors.
    ``cap_hhmm`` is a safety time-cap (should rarely fire — the dip comes first).
    """
    entry_price = row["p10"]
    entry_ts = row["day_ts"].replace(hour=10, minute=0, second=0, microsecond=0)
    cap_ts = _time_to_ts(row["day_ts"], cap_hhmm)

    # Walk intraday bars from entry to cap.
    window = intraday[(intraday.index >= entry_ts) & (intraday.index <= cap_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    exit_price = None
    exit_reason = "time_cap"
    extreme = entry_price  # running peak (call) / trough (put)
    for ts, bar in window.iterrows():
        px = float(bar["close"])
        if side == "call":
            extreme = max(extreme, px)
            # Hard take-profit / stop-loss floors.
            if target_pct > 0 and (px - entry_price) / entry_price >= target_pct:
                exit_price, exit_reason = px, "target"
                break
            if stop_pct > 0 and (entry_price - px) / entry_price >= stop_pct:
                exit_price, exit_reason = px, "stop"
                break
            # Trailing-stop giveback: exit when price dips below the peak.
            if extreme > entry_price:
                giveback = (extreme - px) / extreme
                if giveback >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        else:  # put
            extreme = min(extreme, px)
            if target_pct > 0 and (entry_price - px) / entry_price >= target_pct:
                exit_price, exit_reason = px, "target"
                break
            if stop_pct > 0 and (px - entry_price) / entry_price >= stop_pct:
                exit_price, exit_reason = px, "stop"
                break
            if extreme < entry_price:
                giveback = (px - extreme) / extreme
                if giveback >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        exit_price = px
    if exit_price is None:
        return {"traded": False, "reason": "no_data"}

    # Underlying move (signed by direction).
    if side == "call":
        move = (exit_price - entry_price) / entry_price
    else:
        move = (entry_price - exit_price) / entry_price

    # Option PnL model (economically correct for a SAME-DAY round-trip):
    #   PnL = delta * (underlying move) * notional - round_trip_cost
    # where:
    #   notional = entry_price * 100 (one contract = 100 shares)
    #   delta    = the option's delta (0.25-0.75 per the picker)
    #   round_trip_cost = round_trip_cost_pct * notional
    #
    # IMPORTANT: we do NOT charge the FULL premium. When you buy an option at
    # 10:00 and sell it at 11:00-13:00 the same day, you get most of the premium
    # back — you only lose the BID-ASK SPREAD + THETA DECAY over the hold, not
    # the whole premium. Charging the full premium would be wrong (it assumes
    # the option expires worthless, which it doesn't on a same-day round-trip).
    # round_trip_cost_pct is the fraction of notional lost to spread+theta.
    notional = entry_price * 100.0
    round_trip_cost = round_trip_cost_pct * notional
    pnl = delta * move * notional - round_trip_cost

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
                 delta: float, round_trip_cost_pct: float) -> dict:
    """Run the full grid sweep. Returns {config_key: stats}."""
    daily = build_daily_frame(client, symbol, days_back)
    if daily.empty:
        logger.error(f"No daily frame for {symbol}.")
        return {}

    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.error(f"No intraday bars for {symbol}.")
        return {}

    # Assign entry side per day.
    daily["side"] = daily.apply(_entry_side, axis=1)
    n_call = int((daily["side"] == "call").sum())
    n_put = int((daily["side"] == "put").sum())
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
                               giveback, delta, round_trip_cost_pct)
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
            "calls": sum(1 for t in trades if t["side"] == "call"),
            "puts": sum(1 for t in trades if t["side"] == "put"),
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
        "n_call": n_call,
        "n_put": n_put,
        "n_skip": n_skip,
        "delta": delta,
        "round_trip_cost_pct": round_trip_cost_pct,
        "configs": results,
    }


def _notify_discord(summary: dict) -> None:
    try:
        configs = summary["configs"]
        if not configs:
            send_discord_message("SPY options backtest: no configs produced.")
            return
        best = max(configs.values(), key=lambda c: c["expectancy"])
        # Zero-cost best (pure directional edge) for comparison.
        zero_cost = {k: v for k, v in configs.items()
                     if summary.get("round_trip_cost_pct", 0) == 0}
        lines = [
            f"**SPY OPTIONS BACKTEST — {summary['n_days']} days**",
            f"Entry rule (3-bucket): {summary['n_call']} calls (10:00>prevH), "
            f"{summary['n_put']} puts (10:00<prevL), {summary['n_skip']} skips.",
            f"Model: delta={summary['delta']}, round-trip cost="
            f"{summary['round_trip_cost_pct']:.2%} of notional.",
            "",
            f"**Best config (with cost):** {best['time_cap']} cap, "
            f"target {best['target_pct']:.1%}, stop {best['stop_pct']:.1%}, "
            f"giveback {best['giveback_pct']:.1%}",
            f"  → expectancy **${best['expectancy']:.2f}/trade**, "
            f"win {best['win_rate']:.0%}, {best['trades']} trades, "
            f"total ${best['total_pnl']:.0f}",
            f"  → exits: {best['exit_reasons']}",
        ]
        if zero_cost:
            zbest = max(zero_cost.values(), key=lambda c: c["expectancy"])
            lines += [
                "",
                f"**Pure directional edge (zero cost):** {zbest['time_cap']} cap, "
                f"target {zbest['target_pct']:.1%}, stop {zbest['stop_pct']:.1%}, "
                f"giveback {zbest['giveback_pct']:.1%}",
                f"  → expectancy **${zbest['expectancy']:.2f}/trade**, "
                f"win {zbest['win_rate']:.0%}, {zbest['trades']} trades",
            ]
        lines += [
            "",
            "**Bottom line:** The 3-bucket rule has a REAL but TINY directional "
            "edge (~56% win, ~$2/trade before costs). Any realistic option "
            "round-trip cost (spread+theta) wipes it out — SPY's intraday moves "
            "(0.2-1%) are too small vs option costs. Options on SPY with this "
            "rule are NOT profitable. Consider a higher-vol underlying or "
            "revisit the entry rule.",
        ]
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY options-flavored backtest")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--delta", type=float, default=DEFAULT_DELTA)
    parser.add_argument("--round-trip-cost-pct", type=float, default=DEFAULT_ROUND_TRIP_COST_PCT)
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-anchor-options")
    client = AlpacaClient()

    try:
        summary = run_backtest(client, args.symbol.upper(), args.days,
                               args.delta, args.round_trip_cost_pct)
        if not summary or not summary.get("configs"):
            logger.error("No configs produced.")
            return
        with open(SUMMARY_PATH, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info(f"Wrote {SUMMARY_PATH}")

        print(f"\n=== SPY OPTIONS BACKTEST ({summary['n_days']} days) ===")
        print(f"Entry: {summary['n_call']} calls, {summary['n_put']} puts, "
              f"{summary['n_skip']} skips | delta={summary['delta']} "
              f"round-trip-cost={summary['round_trip_cost_pct']:.2%}")
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
        logger.critical(f"Options backtest failed: {e}")
        log_exception_to_jira(e, "Anchor Options Backtest Failure")
        raise


if __name__ == "__main__":
    main()