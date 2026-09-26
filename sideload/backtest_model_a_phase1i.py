#!/usr/bin/env python3
"""Model A TSLA-only holdout validation (Phase 1i).

Confirm that TSLA's Model A performance (PF 1.82, +45% target, -22% stop) is
statistically robust and not an artifact of in-sample overfitting. Test across
a dedicated out-of-sample holdout window.

Dataset split (TSLA only):
  In-Sample:        Prior 252 to 127 trading days (~Months 6-12 back).
  Out-of-Sample:    Most recent 126 trading days (~Last 6 months).
  Full Combined:    252 trading days.

Locked parameters:
  Entry premium:    $3.50 base, Delta 0.45.
  PM volatility:    (PMH - PML) / PML >= 0.0035.
  Setup trigger:    Sweep of PMH/PML with 1-min reversal candle crossing VWAP.
  Profit target:    Single limit target at +45.0% premium gain.
  Stop loss:        Hard cap at -22.0% (intrabar adverse extreme).
  Time exit:        30 minutes maximum hold.

Usage:
    python -m sideload.backtest_model_a_phase1i --days 252
    python -m sideload.backtest_model_a_phase1i --days 252 --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from sideload.backtest_model_a_phase1h import (
    _load_daily,
    _load_intraday,
    _vwap_series,
    _vwap_at,
    _premium_at,
    _model_a_setup,
    _simulate_single,
    PM_VOL_MIN,
    HARD_STOP_CAP,
    MAX_HOLD_MINUTES,
)
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestModelAPhase1i")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")

# Locked Phase 1i parameters.
TICKER = "TSLA"
ENTRY_PREMIUM = 3.50
DELTA = 0.45
TP_PCT = 0.45  # +45% single target.

# Split windows (trading days).
FULL_DAYS = 252
HOLDOUT_DAYS = 126  # most recent 126 trading days (out-of-sample).
IN_SAMPLE_DAYS = 126  # prior 252-127 (months 6-12).

# Acceptance gate for production staging.
GATE_HOLDOUT_PF = 1.60
GATE_HOLDOUT_WIN = 42.0
GATE_HOLDOUT_TRADES = 15


def _run_trades(client: AlpacaClient, symbol: str, days_back: int) -> tuple[list[dict], list[str]]:
    """Run Model A (locked params) and return (trades, trading_days).

    trading_days is the full list of trading-day dates (str) in the window,
    used to split the dataset into in-sample vs holdout by calendar day.
    """
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return [], []

    days = sorted(intraday.index.normalize().unique())
    trading_days = [str(d.date()) for d in days]
    results = []
    for day in days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
        if day_bars.empty:
            continue

        day_naive = day.tz_localize(None) if day.tzinfo is not None else day
        prior = daily[daily.index < day_naive]
        if prior.empty:
            continue
        prior_row = prior.iloc[-1]
        pdh, pdl = float(prior_row["high"]), float(prior_row["low"])
        pm_start = day_start.replace(hour=4, minute=0)
        pm_end = day_start.replace(hour=9, minute=29)
        pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
        pmh = float(pm_bars["high"].max()) if not pm_bars.empty else None
        pml = float(pm_bars["low"].min()) if not pm_bars.empty else None
        anchors = {"pdh": pdh, "pdl": pdl, "pmh": pmh, "pml": pml}

        # Pre-market volatility filter.
        if pmh is None or pml is None or pml <= 0:
            continue
        if (pmh - pml) / pml < PM_VOL_MIN:
            continue

        vwap_series = _vwap_series(day_bars, day)
        setup = _model_a_setup(day_bars, day, anchors, vwap_series,
                               use_vwap_slope=False)
        if setup is None:
            continue

        sim = _simulate_single(day_bars, setup["entry_ts"], setup["direction"],
                               setup["entry_price"], tp_pct=TP_PCT,
                               entry_premium=ENTRY_PREMIUM, delta=DELTA)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    return results, trading_days


def _summarize(trades: list[dict]) -> dict:
    """Compute summary stats for a list of trades."""
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "total_pnl_pct": 0.0,
                "profit_factor": 0.0, "max_dd_pct": 0.0}
    df = pd.DataFrame(trades)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses["pnl_pct"].sum())) if len(losses) else 0.0
    total_pnl = float(df["pnl_pct"].sum())
    cum = df["pnl_pct"].cumsum()
    peak = cum.cummax()
    max_dd = float((cum - peak).min())
    return {
        "trades": len(df),
        "win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_pct": round(max_dd, 2),
    }


def run_holdout(client: AlpacaClient, days_back: int = FULL_DAYS) -> dict:
    """Run the full 252-day TSLA backtest and split into in-sample/holdout."""
    all_trades, trading_days = _run_trades(client, TICKER, days_back)
    all_trades.sort(key=lambda t: t["date"])

    # Split by trading calendar: most recent HOLDOUT_DAYS trading days are OOS.
    holdout_dates = set(trading_days[-HOLDOUT_DAYS:])
    holdout = [t for t in all_trades if t["date"] in holdout_dates]
    in_sample = [t for t in all_trades if t["date"] not in holdout_dates]

    return {
        "ticker": TICKER,
        "trading_days": len(trading_days),
        "full": _summarize(all_trades),
        "in_sample": _summarize(in_sample),
        "holdout": _summarize(holdout),
        "full_trades": all_trades,
        "in_sample_trades": in_sample,
        "holdout_trades": holdout,
    }


def _print_table(rows: list[tuple]) -> None:
    header = ("Split", "Trading Days", "Trades", "Win Rate %", "Net PnL (%)",
              "Profit Factor", "Max DD (%)")
    print(f"{header[0]:<22} {header[1]:<14} {header[2]:<8} {header[3]:<12} "
          f"{header[4]:<12} {header[5]:<14} {header[6]:<10}")
    for r in rows:
        print(f"{r[0]:<22} {r[1]:<14} {r[2]:<8} {r[3]:<12} {r[4]:<12} "
              f"{r[5]:<14} {r[6]:<10}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Model A TSLA holdout validation (Phase 1i)")
    parser.add_argument("--days", type=int, default=FULL_DAYS,
                        help="Days of history to backtest (default 252).")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        result = run_holdout(client, args.days)

        out_path = os.path.join(OUT_DIR, "backtest_model_a_phase1i.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"Wrote phase1i holdout validation to {out_path}")

        full = result["full"]
        ins = result["in_sample"]
        hold = result["holdout"]

        rows = [
            ("In-Sample (Months 6-12)", IN_SAMPLE_DAYS,
             ins["trades"], ins["win_rate"], ins["total_pnl_pct"],
             ins["profit_factor"], ins["max_dd_pct"]),
            ("Out-of-Sample (Holdout)", HOLDOUT_DAYS,
             hold["trades"], hold["win_rate"], hold["total_pnl_pct"],
             hold["profit_factor"], hold["max_dd_pct"]),
            ("Full 252-Day Combined", args.days,
             full["trades"], full["win_rate"], full["total_pnl_pct"],
             full["profit_factor"], full["max_dd_pct"]),
        ]
        _print_table(rows)

        # Acceptance gate evaluation.
        print("\nAcceptance Gate (Holdout):")
        checks = [
            ("Holdout Profit Factor >= 1.60", hold["profit_factor"], GATE_HOLDOUT_PF),
            ("Holdout Win Rate >= 42.0%", hold["win_rate"], GATE_HOLDOUT_WIN),
            ("Holdout Trades >= 15", hold["trades"], GATE_HOLDOUT_TRADES),
            ("Positive Net PnL (In-Sample)", ins["total_pnl_pct"], 0.0),
            ("Positive Net PnL (Holdout)", hold["total_pnl_pct"], 0.0),
        ]
        all_pass = True
        for label, actual, threshold in checks:
            met = actual >= threshold
            all_pass = all_pass and met
            print(f"  [{'PASS' if met else 'FAIL'}] {label}: {actual} (>= {threshold})")

        verdict = "PASS - freeze parameters and generate deployment architecture" if all_pass \
            else "FAIL - do not stage to production"
        print(f"\nVerdict: {verdict}")

        if not args.no_discord:
            try:
                lines = [
                    f"**Model A Phase 1i TSLA Holdout ({args.days}d)**",
                    f"`In-Sample` trades={ins['trades']} Win={ins['win_rate']}% "
                    f"PnL={ins['total_pnl_pct']}% PF={ins['profit_factor']}",
                    f"`Holdout` trades={hold['trades']} Win={hold['win_rate']}% "
                    f"PnL={hold['total_pnl_pct']}% PF={hold['profit_factor']}",
                    f"`Full` trades={full['trades']} Win={full['win_rate']}% "
                    f"PnL={full['total_pnl_pct']}% PF={full['profit_factor']}",
                    f"Verdict: {verdict}",
                ]
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_model_a_phase1i", {"days": args.days})
        logger.exception("backtest_model_a_phase1i failed")
        raise


if __name__ == "__main__":
    main()
