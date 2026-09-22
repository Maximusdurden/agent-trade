#!/usr/bin/env python3
"""TSLA supply/demand zone-break backtest (research only — no live lane yet).

Tests the NEW edge (user premise, 2026-09-20):

    1. Prev-day open/close as measured reference points.
    2. Support/resistance (supply/demand) zones for TSLA.
    3. Volume confirms the move.

The strategy under test is a ZONE-BREAK-AND-HOLD:

  LONG  when price breaks ABOVE a resistance zone on volume
        -> target = next resistance zone above, stop = back below the broken zone
  SHORT when price breaks BELOW a support zone on volume
        -> target = next support zone below, stop = back above the broken zone

This is fundamentally different from:
  - the AMD lane (RSI/VWAP mean-reversion),
  - the 3-bucket anchor rule (10:00 vs prior-day high/low).

It is a MOMENTUM/TREND-CONTINUATION edge: a zone break on volume means supply
(resistance) was absorbed or demand (support) was exhausted, so price tends to
continue to the NEXT zone.

Entry model (intraday, no lookahead):
  - At each 5-min bar, detect zones from the trailing window UP TO that bar.
  - If price crosses a resistance zone (was below, now above) AND the bar's
    volume ratio >= vol_min -> LONG entry at that bar.
  - If price crosses a support zone (was above, now below) AND volume confirms
    -> SHORT entry.
  - Only ONE position per day (first qualifying break wins).

Exit model (swept):
  - target: next zone in the direction of travel (zone-to-zone).
  - stop: back across the broken zone.
  - time-cap: force-exit by a time (ET) if neither fired.
  - giveback: trailing-stop from the peak/trough.

PnL model (stock, no option costs — the option cost hurdle killed the 3-bucket
rule, so we test the underlying directly):
  - PnL = (exit - entry) * qty for LONG; (entry - exit) * qty for SHORT.
  - qty sized by alloc_pct of equity.

Usage:
    python -m sideload.backtest_zones_tsla --symbol TSLA --days 730
    python -m sideload.backtest_zones_tsla --symbol TSLA --days 730 --no-discord
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
from sideload import zones_tsla as zones
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestZonesTSLA")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
SUMMARY_PATH = os.path.join(OUT_DIR, "zones_tsla_backtest.json")

DEFAULT_ALLOC_PCT = 0.10
DEFAULT_EQUITY = 10000.0

# Entry grid: volume-confirmation threshold + zone-detection params.
VOL_MIN_RATIOS = [1.0, 1.2, 1.5]          # bar volume / rolling avg
ZONE_WINDOW_DAYS = [15, 30, 60]            # trailing window for zone profile
ZONE_MIN_VOL_FRAC = [0.10, 0.15, 0.20]     # min bucket vol (frac of max)
ZONE_MIN_SEP_PCT = [0.005, 0.01, 0.02]     # min separation between zones

# Exit grid.
TIME_CAPS = ["11:00", "12:00", "13:00", "15:00"]  # ET force-exit
GIVEBACK_PCTS = [0.0, 0.002, 0.005]               # trailing giveback (0 = off)


def _time_to_ts(day: pd.Timestamp, hhmm: str) -> pd.Timestamp:
    h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _crossed_above(prev_price: float, price: float, zone_price: float) -> bool:
    """True if price crossed from below to above a zone between bars."""
    return prev_price <= zone_price < price


def _crossed_below(prev_price: float, price: float, zone_price: float) -> bool:
    """True if price crossed from above to below a zone between bars."""
    return prev_price >= zone_price > price


def simulate_day(day_ts: pd.Timestamp, intraday: pd.DataFrame,
                 zones_by_day: dict, vol_min: float, cap_hhmm: str,
                 giveback_pct: float, alloc_pct: float, equity: float) -> dict:
    """Simulate one day of zone-break trading. Returns stats dict.

    Zones are PRECOMPUTED once per day (at 10:00 ET, no lookahead) and reused
    for both entry and exit. This is both much faster than re-detecting at every
    bar AND more realistic: a live lane would compute zones once at the start of
    the day, not re-derive them every 5 minutes.

    Entry: first qualifying zone break (on volume) after 9:30 ET.
    Exit: next-zone target / broken-zone stop / time-cap / giveback.
    """
    day_key = day_ts.date()
    zs = zones_by_day.get(day_key, [])
    if not zs:
        return {"traded": False, "reason": "no_zones"}

    open_ts = day_ts.replace(hour=9, minute=30, second=0, microsecond=0)
    cap_ts = _time_to_ts(day_ts, cap_hhmm)
    day_bars = intraday[(intraday.index >= open_ts) & (intraday.index <= cap_ts)]
    if day_bars.empty:
        return {"traded": False, "reason": "no_data"}

    # Walk bars, look for a zone break on volume.
    entry = None
    side = None
    broken_zone = None
    prev_price = None
    for ts, bar in day_bars.iterrows():
        price = float(bar["close"])
        vol_ratio = float(bar.get("vol_ratio", 1.0))
        if prev_price is None:
            prev_price = price
            continue
        if vol_ratio < vol_min:
            prev_price = price
            continue
        for z in zs:
            if _crossed_above(prev_price, price, z["price"]):
                entry, side, broken_zone = price, "long", z
                break
            if _crossed_below(prev_price, price, z["price"]):
                entry, side, broken_zone = price, "short", z
                break
        if entry is not None:
            break
        prev_price = price

    if entry is None:
        return {"traded": False, "reason": "no_break"}

    # Exit: walk forward from entry.
    entry_ts = day_bars.index[day_bars.index.get_loc(ts)]
    exit_bars = intraday[(intraday.index > entry_ts) & (intraday.index <= cap_ts)]
    exit_price = None
    exit_reason = "time_cap"
    extreme = entry
    for _, bar in exit_bars.iterrows():
        px = float(bar["close"])
        if side == "long":
            extreme = max(extreme, px)
            # Target: next resistance zone above entry.
            tgt = zones.nearest_zone(zs, entry, "above")
            if tgt and px >= tgt["price"]:
                exit_price, exit_reason = px, "target"
                break
            # Stop: back below the broken zone.
            if broken_zone and px <= broken_zone["price"]:
                exit_price, exit_reason = px, "stop"
                break
            if giveback_pct > 0 and extreme > entry:
                if (extreme - px) / extreme >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        else:  # short
            extreme = min(extreme, px)
            tgt = zones.nearest_zone(zs, entry, "below")
            if tgt and px <= tgt["price"]:
                exit_price, exit_reason = px, "target"
                break
            if broken_zone and px >= broken_zone["price"]:
                exit_price, exit_reason = px, "stop"
                break
            if giveback_pct > 0 and extreme < entry:
                if (px - extreme) / extreme >= giveback_pct:
                    exit_price, exit_reason = px, "giveback"
                    break
        exit_price = px

    if exit_price is None:
        return {"traded": False, "reason": "no_data"}

    qty = (alloc_pct * equity) / entry
    if side == "long":
        pnl = (exit_price - entry) * qty
        move = (exit_price - entry) / entry
    else:
        pnl = (entry - exit_price) * qty
        move = (entry - exit_price) / entry

    return {
        "traded": True,
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "move_pct": move * 100.0,
        "pnl": pnl,
    }


def run_backtest(client: AlpacaClient, symbol: str, days_back: int,
                 alloc_pct: float, equity: float) -> dict:
    """Run the full grid sweep. Returns {config_key: stats}."""
    intraday = zones.load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.error(f"No intraday bars for {symbol}.")
        return {}
    intraday = zones.add_volume_confirmation(intraday)

    daily = zones.load_daily(client, symbol, limit=days_back)
    if len(daily) < 30:
        logger.error(f"Not enough daily bars for {symbol}.")
        return {}
    prev = zones.build_prev_day_frame(daily)
    day_ts_list = [pd.Timestamp(d).tz_localize(zones.ET) for d in prev["date"]]

    # Precompute zones ONCE per day per zone-config (at 10:00 ET, no lookahead).
    # This is the key optimization: the old code re-detected zones at every bar,
    # which made the grid ~100x too slow to finish. Zones are also more realistic
    # computed once at the start of the day.
    zone_configs = list(itertools.product(ZONE_WINDOW_DAYS, ZONE_MIN_VOL_FRAC, ZONE_MIN_SEP_PCT))
    zones_by_cfg = {}
    for win_days, min_vol_frac, min_sep in zone_configs:
        per_day = {}
        for day_ts in day_ts_list:
            ref_ts = day_ts.replace(hour=10, minute=0, second=0, microsecond=0)
            per_day[day_ts.date()] = zones.detect_zones(
                intraday, ref_ts, window_days=win_days,
                min_vol_frac=min_vol_frac, min_sep_pct=min_sep)
        zones_by_cfg[(win_days, min_vol_frac, min_sep)] = per_day
    logger.info(f"Precomputed zones for {len(zone_configs)} zone-configs x "
                f"{len(day_ts_list)} days.")

    results = {}
    for vol_min, (win_days, min_vol_frac, min_sep), cap, giveback in itertools.product(
            VOL_MIN_RATIOS, zone_configs, TIME_CAPS, GIVEBACK_PCTS):
        per_day = zones_by_cfg[(win_days, min_vol_frac, min_sep)]
        trades = []
        for day_ts in day_ts_list:
            res = simulate_day(day_ts, intraday, per_day, vol_min, cap, giveback,
                               alloc_pct, equity)
            if res.get("traded"):
                trades.append(res)
        if not trades:
            continue
        pnls = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = float(sum(pnls))
        key = (f"v{vol_min:.1f}_w{win_days}_f{min_vol_frac:.2f}_"
               f"s{min_sep:.3f}_g{giveback:.3f}_{cap}")
        results[key] = {
            "vol_min": vol_min,
            "zone_window_days": win_days,
            "zone_min_vol_frac": min_vol_frac,
            "zone_min_sep_pct": min_sep,
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
        "n_days": int(len(day_ts_list)),
        "alloc_pct": alloc_pct,
        "equity": equity,
        "configs": results,
    }


def _notify_discord(summary: dict) -> None:
    try:
        configs = summary["configs"]
        if not configs:
            send_discord_message(f"{summary['symbol']} zone backtest: no configs.")
            return
        best = max(configs.values(), key=lambda c: c["expectancy"])
        lines = [
            f"**{summary['symbol']} ZONE-BREAK BACKTEST — {summary['n_days']} days**",
            f"Sizing: {summary['alloc_pct']:.0%} of ${summary['equity']:.0f} per position.",
            "",
            f"**Best config:** vol>={best['vol_min']:.1f}x, window={best['zone_window_days']}d, "
            f"minvol={best['zone_min_vol_frac']:.0%}, sep={best['zone_min_sep_pct']:.1%}, "
            f"giveback={best['giveback_pct']:.1%}, cap={best['time_cap']}",
            f"  → expectancy **${best['expectancy']:.2f}/trade**, "
            f"win {best['win_rate']:.0%}, {best['trades']} trades "
            f"({best['longs']}L/{best['shorts']}S), total ${best['total_pnl']:.0f}",
            f"  → exits: {best['exit_reasons']}",
            "",
            "**Bottom line:** " + (
                "Zone-break edge IS profitable on the stock. Promising — "
                "consider walk-forward validation + a sideload lane."
                if best["expectancy"] > 0 else
                "Zone-break edge is negative on the stock. Revisit zone "
                "detection or entry/exit model."
            ),
        ]
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="TSLA zone-break backtest")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--alloc-pct", type=float, default=DEFAULT_ALLOC_PCT)
    parser.add_argument("--equity", type=float, default=DEFAULT_EQUITY)
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-zones-backtest")
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

        print(f"\n=== {summary['symbol']} ZONE-BREAK BACKTEST ({summary['n_days']} days) ===")
        print(f"alloc={summary['alloc_pct']:.0%} equity=${summary['equity']:.0f}")
        print(f"{'vol':>5}{'win':>5}{'minvol':>7}{'sep':>7}{'giveback':>9}{'cap':>6}"
              f"{'trades':>7}{'win%':>6}{'expect':>9}{'total':>10}")
        print("-" * 76)
        ranked = sorted(summary["configs"].values(), key=lambda c: c["expectancy"], reverse=True)
        for c in ranked[:20]:
            print(f"{c['vol_min']:>5.1f}{c['zone_window_days']:>5}{c['zone_min_vol_frac']:>7.0%}"
                  f"{c['zone_min_sep_pct']:>7.1%}{c['giveback_pct']:>8.1%}{c['time_cap']:>6}"
                  f"{c['trades']:>7}{c['win_rate']:>6.0%}{c['expectancy']:>9.2f}{c['total_pnl']:>10.2f}")
        if not args.no_discord:
            _notify_discord(summary)
    except Exception as e:
        logger.critical(f"Zone backtest failed: {e}")
        log_exception_to_jira(e, "TSLA Zone Backtest Failure")
        raise


if __name__ == "__main__":
    main()