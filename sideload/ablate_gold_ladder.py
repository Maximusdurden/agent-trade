#!/usr/bin/env python3
"""GLD ladder ablation: 1-shot vs 3-tranche ladder, and MOC vs open fill.

Phase 1c Task A + Task B. Runs the Phase 1b winning setup on GLD
(2020-07-27 to 2026-09-24) across execution modes and fill modes, and outputs
a comparative performance table.

Usage:
    python -m sideload.ablate_gold_ladder
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

from sideload.backtest_gold_ladder import (
    load_daily, simulate, _stats, SPEC_CFG,
)
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient

logger = logging.getLogger("AblateGoldLadder")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")

# Phase 1b winning setup.
BASE_CFG = dict(SPEC_CFG)
BASE_CFG.update({
    "rsi_entry_max": 17.0,
    "band_mode": "none",
    "t2_step_pct": 0.015,
    "tp_sma": 10,
    "tp_blended_pct": 0.02,
    "stop_pct": 0.06,
    "max_hold_days": 10,
})


def _sharpe_from_pnls(pnls: list[float]) -> float:
    if len(pnls) < 3:
        return 0.0
    arr = np.array(pnls, dtype=float)
    if arr.std() == 0:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(252))


def _max_dd_usd(pnls: list[float], base_equity: float = 10000.0) -> tuple[float, float]:
    """Return (max_dd_usd, max_dd_pct_of_peak_equity).

    Equity curve starts at ``base_equity`` (not 0) so the % drawdown is
    meaningful (dividing by a near-zero cumulative-PnL peak would blow up).
    """
    if not pnls:
        return 0.0, 0.0
    eq = base_equity + np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    idx = int(np.argmin(dd))
    max_dd_usd = abs(float(dd[idx]))
    peak_val = float(peak[idx]) if peak[idx] != 0 else base_equity
    return max_dd_usd, max_dd_usd / peak_val * 100.0


def _recovery_trades(pnls: list[float]) -> int:
    """Trades to recover from the max drawdown trough back to prior peak."""
    if not pnls:
        return 0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    trough = int(np.argmin(dd))
    if dd[trough] >= 0:
        return 0
    target = peak[trough]
    for k in range(trough, len(eq)):
        if eq[k] >= target:
            return k - trough
    return len(eq) - trough


def _mar_ratio(pnls: list[float], max_dd_pct: float) -> float:
    """MAR = annualized return / max drawdown %."""
    if not pnls or max_dd_pct <= 0:
        return 0.0
    total = sum(pnls)
    # Approximate annualized return on $10k base.
    ann = (total / 10000.0) / (len(pnls) / 252.0) if pnls else 0.0
    return ann / (max_dd_pct / 100.0)


def run_mode(df, cfg, mode: str, fill_mode: str) -> dict:
    c = dict(cfg)
    c["mode"] = mode
    c["fill_mode"] = fill_mode
    trades = simulate(df, c)
    stats = _stats(trades, c)
    pnls = [t["pnl"] for t in trades]
    max_dd_usd, max_dd_pct = _max_dd_usd(pnls)
    return {
        "mode": mode, "fill_mode": fill_mode,
        "trades": stats["trades"],
        "win_rate": stats["win_rate"],
        "net_pnl": stats["total_pnl_usd"],
        "profit_factor": stats["profit_factor"],
        "max_dd_usd": max_dd_usd,
        "max_dd_pct": max_dd_pct,
        "sharpe": _sharpe_from_pnls(pnls),
        "mar": _mar_ratio(pnls, max_dd_pct),
        "avg_hold_days": stats["avg_hold_days"],
        "avg_tranches": stats["avg_tranches"],
        "recovery_trades": _recovery_trades(pnls),
        "exit_reasons": stats["exit_reasons"],
    }


def print_table(rows: list[dict]) -> None:
    """Print the comparative table for Task A."""
    hdr = ("Metric", "Mode 1 (1-Shot)", "Mode 2 (3-Tranche Ladder)", "Delta / Winner")
    print(f"\n{'Metric':<28} {'1-Shot':>20} {'Ladder':>22} {'Delta / Winner':>22}")
    print("-" * 96)
    m1 = rows[0]
    m2 = rows[1]
    def row(label, k1, k2, fmt=".2f", higher_better=True):
        v1 = m1[k1]
        v2 = m2[k2]
        if isinstance(v1, float):
            s1 = f"{v1:{fmt}}"
            s2 = f"{v2:{fmt}}"
            delta = v2 - v1
            winner = "Ladder" if (delta > 0) == higher_better else "1-Shot"
            if abs(delta) < 1e-9:
                winner = "Tie"
            ds = f"{delta:{fmt}} ({winner})"
        else:
            s1 = str(v1)
            s2 = str(v2)
            ds = f"{v2 - v1} ({'Ladder' if v2 > v1 else '1-Shot' if v2 < v1 else 'Tie'})"
        print(f"{label:<28} {s1:>20} {s2:>22} {ds:>22}")
    row("Total Trades / Clusters", "trades", "trades", "d", higher_better=False)
    row("Win Rate (%)", "win_rate", "win_rate", ".1%")
    row("Net PnL ($)", "net_pnl", "net_pnl", ".2f")
    row("Profit Factor", "profit_factor", "profit_factor", ".2f")
    row("Max Drawdown ($)", "max_dd_usd", "max_dd_usd", ".2f", higher_better=False)
    row("Max Drawdown (%)", "max_dd_pct", "max_dd_pct", ".2f", higher_better=False)
    row("Sharpe", "sharpe", "sharpe", ".2f")
    row("MAR Ratio", "mar", "mar", ".2f")
    row("Avg Trade Duration (d)", "avg_hold_days", "avg_hold_days", ".1f", higher_better=False)
    row("Loss Recovery (trades)", "recovery_trades", "recovery_trades", "d", higher_better=False)
    print(f"\n  Exit reasons 1-shot: {m1['exit_reasons']}")
    print(f"  Exit reasons ladder: {m2['exit_reasons']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-ablate-gold-ladder")
    client = AlpacaClient()
    symbol = "GLD"

    try:
        df = load_daily(client, symbol, 365 * 8)
        if df.empty:
            logger.error("No daily data.")
            return
        logger.info(f"Loaded {len(df)} daily bars for {symbol} ({df.index[0].date()} to {df.index[-1].date()})")

        print("\n" + "=" * 96)
        print("TASK A: 1-Shot vs 3-Tranche Ladder (fill at next open + slippage)")
        print("=" * 96)
        m1 = run_mode(df, BASE_CFG, "1shot", "open")
        m2 = run_mode(df, BASE_CFG, "ladder", "open")
        print_table([m1, m2])

        print("\n" + "=" * 96)
        print("TASK B: Execution Timing — MOC (3:45 PM close fill) vs Next-Open")
        print("=" * 96)
        ladder_open = run_mode(df, BASE_CFG, "ladder", "open")
        ladder_moc = run_mode(df, BASE_CFG, "ladder", "moc")
        print(f"\n{'Metric':<28} {'Next-Open':>20} {'MOC (3:45)':>22} {'Delta':>22}")
        print("-" * 96)
        for label, k in [
            ("Total Trades", "trades"), ("Win Rate (%)", "win_rate"),
            ("Net PnL ($)", "net_pnl"), ("Profit Factor", "profit_factor"),
            ("Max Drawdown (%)", "max_dd_pct"), ("Sharpe", "sharpe"),
            ("Avg Hold (d)", "avg_hold_days"),
        ]:
            v1 = ladder_open[k]
            v2 = ladder_moc[k]
            if isinstance(v1, float):
                fmt = ".1%" if k == "win_rate" else ".2f"
                print(f"{label:<28} {v1:>20{fmt}} {v2:>22{fmt}} {v2 - v1:>22{fmt}}")
            else:
                print(f"{label:<28} {v1:>20} {v2:>22} {v2 - v1:>22}")

        # Save results.
        out = {
            "task_a": {"one_shot": m1, "ladder": m2},
            "task_b": {"next_open": ladder_open, "moc": ladder_moc},
        }
        path = os.path.join(OUT_DIR, "gold_ladder_ablation.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")
    except Exception as e:
        logger.critical(f"Ablation failed: {e}")
        log_exception_to_jira(e, "GLD Ladder Ablation Failure")
        raise


if __name__ == "__main__":
    main()