#!/usr/bin/env python3
"""Wyckoff LPS exit & stop architecture ablation (Phase 1b).

Tests 4 configurations across the 8-symbol pool (2021-01-01 to 2026-09-24):
  Config 0 (Baseline): 3-bar swing-low stop, 50%@2R + trail SMA10.
  Config A (ATR Stop): Entry - 1.5*ATR14 stop, 50%@2R + trail SMA10.
  Config B (Wide Exit): 3-bar swing-low stop, 50%@2.5R + 50%@4.5R, 40d time stop.
  Config C (ATR+Wide): Entry - 1.5*ATR14 stop, 50%@2.5R + 50%@4.5R, 40d time stop.

Usage:
    python -m sideload.ablate_wyckoff_lps
"""

from __future__ import annotations

import json
import logging
import os
import sys

import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.backtest_wyckoff_lps import (
    load_daily, simulate, _stats, DEFAULT_CFG, DEFAULT_SYMBOLS,
)
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient

logger = logging.getLogger("AblateWyckoffLPS")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")

START = "2021-01-01"
END = "2026-09-24"

# --- 4 configurations ---
def _base_cfg() -> dict:
    return dict(DEFAULT_CFG)

CONFIGS = {
    "Config 0 (Baseline)": {
        "stop_mode": "swing_low",
        "exit_mode": "trail_sma",
        "tp_r_mult": 2.0,
        "max_hold_days": 60,
    },
    "Config A (ATR Stop)": {
        "stop_mode": "atr",
        "atr_stop_mult": 1.5,
        "exit_mode": "trail_sma",
        "tp_r_mult": 2.0,
        "max_hold_days": 60,
    },
    "Config B (Wide Exit)": {
        "stop_mode": "swing_low",
        "exit_mode": "asymmetric",
        "tp_r_mult": 2.5,
        "tp2_r_mult": 4.5,
        "max_hold_days": 40,
    },
    "Config C (ATR + Wide Exit)": {
        "stop_mode": "atr",
        "atr_stop_mult": 1.5,
        "exit_mode": "asymmetric",
        "tp_r_mult": 2.5,
        "tp2_r_mult": 4.5,
        "max_hold_days": 40,
    },
}


def run_pool(client: AlpacaClient, symbols: list[str], cfg_overrides: dict) -> dict:
    """Run one config across the pool, return aggregate + per-symbol stats."""
    cfg = _base_cfg()
    cfg.update(cfg_overrides)
    per_symbol = []
    agg_trades = 0
    agg_pnl = 0.0
    agg_wins = 0
    for symbol in symbols:
        df = load_daily(client, symbol)
        if df.empty:
            continue
        df = df[(df.index >= START) & (df.index <= END)]
        if df.empty:
            continue
        trades = simulate(df, cfg)
        stats = _stats(trades, cfg)
        stats["symbol"] = symbol
        per_symbol.append(stats)
        agg_trades += stats["trades"]
        agg_pnl += stats["total_pnl_usd"]
        agg_wins += int(stats["trades"] * stats["win_rate"])
    win_rate = agg_wins / agg_trades if agg_trades else 0.0
    # Aggregate avgR/PF from per-symbol stats.
    total_pnl = sum(s["total_pnl_usd"] for s in per_symbol)
    total_trades = sum(s["trades"] for s in per_symbol)
    avg_r = sum(s["avg_r_mult"] * s["trades"] for s in per_symbol) / total_trades if total_trades else 0.0
    # PF: sum gross wins / sum gross losses across symbols.
    gross_win = sum(max(0.0, s["total_pnl_usd"]) for s in per_symbol)
    gross_loss = sum(min(0.0, s["total_pnl_usd"]) for s in per_symbol)
    pf = abs(gross_win / gross_loss) if gross_loss != 0 else float("inf")
    max_dd = max((s["max_drawdown_pct"] for s in per_symbol), default=0.0)
    top = sorted(per_symbol, key=lambda s: -s["total_pnl_usd"])[:2]
    return {
        "trades": total_trades,
        "win_rate": win_rate,
        "total_pnl_usd": total_pnl,
        "avg_r_mult": avg_r,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd,
        "top_symbols": [s["symbol"] for s in top],
        "per_symbol": per_symbol,
    }


def print_table(results: dict) -> None:
    print(f"\n{'Configuration':<24} {'Trades':>7} {'Win%':>6} {'PnL$':>10} "
          f"{'AvgR':>6} {'PF':>6} {'MaxDD%':>7} {'Top Symbols':>14}")
    print("-" * 90)
    for name, r in results.items():
        print(f"{name:<24} {r['trades']:>7} {r['win_rate']*100:>5.1f}% "
              f"{r['total_pnl_usd']:>10.2f} {r['avg_r_mult']:>6.2f} "
              f"{r['profit_factor']:>6.2f} {r['max_drawdown_pct']:>7.1f}% "
              f"{', '.join(r['top_symbols']):>14}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-ablate-wyckoff-lps")
    client = AlpacaClient()
    symbols = DEFAULT_SYMBOLS

    try:
        results = {}
        for name, overrides in CONFIGS.items():
            logger.info(f"Running {name}...")
            results[name] = run_pool(client, symbols, overrides)
            r = results[name]
            logger.info(f"  {name}: trades={r['trades']} pnl=${r['total_pnl_usd']:.2f} "
                        f"win={r['win_rate']:.1%} avgR={r['avg_r_mult']:.2f} PF={r['profit_factor']:.2f}")

        print("\n" + "=" * 90)
        print("WYCKOFF LPS ABLATION — 8-symbol pool (2021-01-01 to 2026-09-24)")
        print("=" * 90)
        print_table(results)

        # Go/No-Go gate.
        print("\n=== GO / NO-GO GATE ===")
        print("Go criteria: PnL > +$400 AND PF >= 1.40 AND avgR > +0.35")
        best = max(results.values(), key=lambda r: r["total_pnl_usd"])
        passed = (best["total_pnl_usd"] > 400.0 and best["profit_factor"] >= 1.40
                  and best["avg_r_mult"] > 0.35)
        print(f"Best config: PnL=${best['total_pnl_usd']:.2f} PF={best['profit_factor']:.2f} "
              f"avgR={best['avg_r_mult']:.2f}")
        print(f"VERDICT: {'GO' if passed else 'NO-GO'}")

        path = os.path.join(DATA_DIR, "wyckoff_lps_ablation.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "verdict": "GO" if passed else "NO-GO",
                       "best": best}, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
    except Exception as e:
        logger.critical(f"Ablation failed: {e}")
        log_exception_to_jira(e, "Wyckoff LPS Ablation Failure")
        raise


if __name__ == "__main__":
    main()