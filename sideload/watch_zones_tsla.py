#!/usr/bin/env python3
"""Watch for the TSLA zone backtest to finish, then notify Discord.

Polls for the backtest output file (zones_tsla_backtest.json). When it appears
(and is non-empty), reads the results, formats the top configs, and sends a
Discord message with the initial findings.

Usage:
    python -m sideload.watch_zones_tsla
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.discord_notifier import send_discord_message

logger = logging.getLogger("WatchZonesTSLA")

SUMMARY_PATH = os.path.join(PROJECT_ROOT, "sideload", "zones_tsla_backtest.json")
POLL_SECONDS = 15
MAX_WAIT_SECONDS = 60 * 60  # 1 hour cap


def _read_summary() -> dict | None:
    if not os.path.exists(SUMMARY_PATH):
        return None
    try:
        with open(SUMMARY_PATH) as f:
            data = json.load(f)
        if not data or not data.get("configs"):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _format_results(summary: dict) -> str:
    configs = summary["configs"]
    ranked = sorted(configs.values(), key=lambda c: c["expectancy"], reverse=True)
    best = ranked[0]
    lines = [
        f"**TSLA ZONE-BREAK BACKTEST COMPLETE — {summary['n_days']} days**",
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
        "**Top 5 by expectancy:**",
    ]
    for c in ranked[:5]:
        lines.append(
            f"  `v{c['vol_min']:.1f} w{c['zone_window_days']} f{c['zone_min_vol_frac']:.0%} "
            f"s{c['zone_min_sep_pct']:.1%} g{c['giveback_pct']:.1%} {c['time_cap']}` "
            f"→ ${c['expectancy']:.2f}/trade, {c['win_rate']:.0%} win, {c['trades']} trades"
        )
    lines.append("")
    lines.append("**Bottom line:** " + (
        "Zone-break edge IS profitable on the stock. Promising — next: walk-forward "
        "validation + a TSLA sideload lane."
        if best["expectancy"] > 0 else
        "Zone-break edge is negative on the stock. Revisit zone detection or the "
        "entry/exit model."
    ))
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info(f"Watching for {SUMMARY_PATH} ...")
    waited = 0
    while waited < MAX_WAIT_SECONDS:
        summary = _read_summary()
        if summary:
            msg = _format_results(summary)
            ok = send_discord_message(msg)
            logger.info(f"Discord notify {'sent' if ok else 'FAILED'}.")
            print(msg)
            return
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    logger.warning("Timed out waiting for the backtest output.")
    send_discord_message("TSLA zone backtest still not done after 1h — check the terminal.")


if __name__ == "__main__":
    main()