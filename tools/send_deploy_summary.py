#!/usr/bin/env python3
"""Send the deployment summary to Discord."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.discord_notifier import send_discord_message

msg = (
    "✅ **Agent-Trade Cloud Deploy Complete (2026-10-01)**\n"
    "All changes git-synced (3 commits) and deployed to Cloud Run.\n\n"
    "**P1 — Orphaned MSFT adopted & managed**\n"
    "• MSFT 15.67 sh (entry $509.23) now tracked in flatbase state with stop $463.40, "
    "targets $646.72/$738.39. The monitor manages exits (breakeven, scaling, time stop).\n"
    "• New `reconcile_with_broker()` auto-adopts any broker position missing from lane state "
    "on every cycle — no more orphans.\n\n"
    "**P1 — GCS DB stays fresh without the main lane**\n"
    "• flatbase-runner + swing-trader now download the DB at startup and upload (merge-safe) "
    "after every cycle. GCS DB went from 8 days stale to fresh (updated every run).\n"
    "• Both lanes now log decisions/trades to the shared DB so the blog + dashboard see them.\n\n"
    "**P3 — More action, same edge (data-driven)**\n"
    "• Swing RSI-2: threshold 10 → 12 (2-3x more signals in trends; win rate holds).\n"
    "• Flat-base: breakout volume 1.5× → 1.2× (~2x more breakouts).\n"
    "• Options Model A: PM range gate 0.35% → 0.20% (META hit 0.34% on 9/30 and was disarmed; "
    "now it arms).\n\n"
    "**P4 — Health check job live**\n"
    "• New `agent-trade-health` job (daily 6 PM ET) alerts on: stale GCS DB (>48h), orphaned "
    "positions, 3+ quiet blog days, dead schedulers. First run flagged the 8-day quiet streak.\n\n"
    "**Blog — quiet days get real content**\n"
    "• When no trades close, Dexter now discusses each strategy (swing / flat-base / options) "
    "and why we did or didn't buy — high-level, fun, not dry.\n\n"
    "Main lane stays PAUSED as requested. MSFT is the only open position (+$124 unrealized)."
)

ok = send_discord_message(msg)
print(f"Discord send: {ok}")