#!/usr/bin/env python3
"""Model A candidate universe screening (Phase 3, Part A).

Backtest candidate high-beta tickers (AMD, COIN, AMZN) over 252 trading days
using the validated Phase 1i engine (+45% target, -22% stop, 30m hold) with
realistic pricing anchors. Establish an empirical acceptance tier (PF >= 1.50).

Candidate pricing anchors:
  AMD:  Entry Premium $2.20, Delta 0.45
  COIN: Entry Premium $3.80, Delta 0.45
  AMZN: Entry Premium $2.40, Delta 0.45

Usage:
    python -m sideload.backtest_model_a_phase3 --days 252
    python -m sideload.backtest_model_a_phase3 --days 252 --no-discord
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

logger = logging.getLogger("BacktestModelAPhase3")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")

# Locked Phase 1i parameters.
TP_PCT = 0.45  # +45% single target.

# Candidate universe with realistic pricing anchors.
TICKER_CONFIG = {
    "TSLA": {"entry_premium": 3.50, "delta": 0.45},  # Locked baseline.
    "AMD":  {"entry_premium": 2.20, "delta": 0.45},
    "COIN": {"entry_premium": 3.80, "delta": 0.45},
    "AMZN": {"entry_premium": 2.40, "delta": 0.45},
    "META": {"entry_premium": 4.50, "delta": 0.45},
    "PLTR": {"entry_premium": 1.20, "delta": 0.45},
}
CANDIDATES = ["AMD", "COIN", "AMZN", "META", "PLTR"]

# Acceptance tier.
ACCEPT_PF = 1.50


def _run_trades(client: AlpacaClient, symbol: str, days_back: int,
                entry_premium: float, delta: float) -> list[dict]:
    """Run Model A (locked params) for a symbol and return trade dicts."""
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        return []

    days = sorted(intraday.index.normalize().unique())
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
                               entry_premium=entry_premium, delta=delta)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    return results


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


def run_screen(client: AlpacaClient, days_back: int = 252) -> dict:
    """Run the 252-day screen across TSLA + candidates."""
    results = {}
    for symbol, cfg in TICKER_CONFIG.items():
        logger.info(f"Screening {symbol} over {days_back} days...")
        trades = _run_trades(client, symbol, days_back,
                             cfg["entry_premium"], cfg["delta"])
        summary = _summarize(trades)
        summary["entry_premium"] = cfg["entry_premium"]
        summary["delta"] = cfg["delta"]
        summary["accepted"] = summary["profit_factor"] >= ACCEPT_PF
        results[symbol] = {"summary": summary, "trades": trades}
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Model A candidate screen (Phase 3)")
    parser.add_argument("--days", type=int, default=252,
                        help="Days of history to backtest (default 252).")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        results = run_screen(client, args.days)

        out_path = os.path.join(OUT_DIR, "backtest_model_a_phase3.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote phase3 screen to {out_path}")

        # Comparison table.
        print(f"{'Ticker':<8} {'Days':<6} {'Trades':<8} {'Win%':<8} "
              f"{'NetPnL%':<10} {'PF':<8} {'MaxDD%':<8} {'Accept(PF>=1.50)':<18}")
        for symbol in list(TICKER_CONFIG.keys()):
            s = results[symbol]["summary"]
            print(f"{symbol:<8} {args.days:<6} {s['trades']:<8} {s['win_rate']:<8} "
                  f"{s['total_pnl_pct']:<10} {s['profit_factor']:<8} "
                  f"{s['max_dd_pct']:<8} {'PASS' if s['accepted'] else 'FAIL':<18}")

        # Approved universe.
        approved = [sym for sym in TICKER_CONFIG if results[sym]["summary"]["accepted"]]
        print(f"\nApproved universe (PF >= {ACCEPT_PF}): {approved}")

        if not args.no_discord:
            try:
                lines = [f"**Model A Phase 3 Candidate Screen ({args.days}d)**"]
                for symbol in TICKER_CONFIG:
                    s = results[symbol]["summary"]
                    lines.append(
                        f"`{symbol}` trades={s['trades']} Win={s['win_rate']}% "
                        f"PnL={s['total_pnl_pct']}% PF={s['profit_factor']} "
                        f"DD={s['max_dd_pct']}% {'PASS' if s['accepted'] else 'FAIL'}"
                    )
                lines.append(f"Approved: {approved}")
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_model_a_phase3", {"days": args.days})
        logger.exception("backtest_model_a_phase3 failed")
        raise


if __name__ == "__main__":
    main()
