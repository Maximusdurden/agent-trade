#!/usr/bin/env python3
"""Self-prompting Discord notifier for the Strategist Model A/B experiment.

The A/B report file (`tools/strategist_ab_report.py`) only gets read if you
remember to open it. This script is the "throw it in front of me" layer: it
pulls the authoritative cloud DB, re-runs the A/B attribution, and pings the
agent-trade Discord webhook ONLY when there is something worth looking at:

  * **New data** — at least one round-trip has been attributed to an A/B-tagged
    rule since the last ping (high-watermark tracked in `reports/.ab_notified`).
  * **Weekly heartbeat** — if nothing new has come in for 7+ days, it sends the
    cumulative running tally anyway, so you know the harness is alive and keep
    it on your radar instead of forgetting it.

Throttled via a small JSON state file (`reports/.ab_notified.json`), so it never
spams on the 15-min scheduler. Read-only with respect to trading state.

Usage:
    python tools/strategist_ab_notify.py            # pull cloud DB, notify if new
    python tools/strategist_ab_notify.py --no-pull   # use existing snapshot
    python tools/strategist_ab_notify.py --force     # always send a summary
    python tools/strategist_ab_notify.py --dry-run   # print what it WOULD send
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

import tools.strategist_ab_report as ab     # noqa: E402
from tools.strategist_ab_report import analyze, parse_dt, REPORTS_DIR  # noqa: E402
from core.discord_notifier import send_discord_embed  # noqa: E402

STATE_FILE = os.path.join(REPORTS_DIR, ".ab_notified.json")
WEEKLY_DAYS = 7
MARKER = "`"  # Discord inline code fence


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fmt_model(model):
    """Strip provider prefix for readability, e.g. anthropic/claude-sonnet-5 -> Sonnet-5."""
    short = model.split("/")[-1]
    return short


def build_embed(res, new_trips, db_path=None):
    grouped = res["grouped"]
    lines = []
    src = os.path.basename(db_path) if db_path else "cloud_downloaded_trading_agent.db"
    lines.append(f"**Source:** `{src}`")
    lines.append(f"Total equity round-trips analyzed: **{len(res['trips'])}**")
    lines.append(f"Attributed to an A/B-tagged rule: **{len(res['attributed'])}**")
    lines.append(f"Pre-experiment / untagged: **{res['not_attributed']}**")

    if new_trips:
        lines.append(f"\n**New since last check:** {len(new_trips)} round-trip(s)")
        by_model = {}
        for t in new_trips:
            key = fmt_model(t["model"])
            by_model.setdefault(key, 0)
            by_model[key] += 1
        lines.append(" - " + "\n - ".join(f"{k}: {v}" for k, v in by_model.items()))

    if grouped is not None and len(grouped):
        lines.append("\n**Running tally by authoring model**")
        for model, row in grouped.iterrows():
            tag = fmt_model(model)
            expect = row["pnl"] / row["rt"]
            lines.append(
                f"\n**{tag}**"
                f"\n   RTs: {int(row['rt'])} | PnL: ${row['pnl']:+,.2f}"
                f" | Win%: {row['win']*100:.1f}%"
                f" | Expectancy: ${expect:+,.2f}/trade"
                f" | Avg hold: {row['avg_hold_h']:.1f}h"
            )
    else:
        lines.append("\nNo attributed trades yet. The strategist needs to run under "
                     "the A/B experiment and log rules with `|model=...` tags first.")

    lines.append("\n_Collected over 2-4 weeks before drawing conclusions. "
                 "Full detail: `reports/strategist_ab_report.md`_.")

    color = 0x5865F2  # blurple
    return {
        "title": "🧪 Strategist Model A/B — Tally",
        "description": "\n".join(lines),
        "color": color,
    }


def should_notify(new_trips, state, now, force=False):
    """Decide whether a Discord tally is due.

    Sends when (a) force, (b) there are new attributed round-trips since the last
    reported high-watermark, or (c) it has been >= WEEKLY_DAYS since the last send
    (weekly heartbeat so the user doesn't forget the harness exists).
    """
    if force or new_trips:
        return True
    last_sent_iso = state.get("last_sent_at")
    last_sent = None
    if last_sent_iso:
        try:
            last_sent = datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
        except Exception:
            last_sent = None
    return last_sent is None or ((now - last_sent) > timedelta(days=WEEKLY_DAYS))


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--no-pull", action="store_true",
                    help="Use the existing cloud_downloaded_trading_agent.db snapshot "
                         "instead of downloading a fresh one.")
    ap.add_argument("--db", default=None,
                    help="Explicit path to the trading DB to analyze (overrides the "
                         "default cloud_downloaded_trading_agent.db snapshot).")
    ap.add_argument("--force", action="store_true",
                    help="Always send a summary, ignoring throttle/heartbeat logic.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the embed and state changes without sending to Discord.")
    args = ap.parse_args(argv)

    if not args.no_pull:
        try:
            import tools.pull_cloud_db as pull
            rc = pull.main()
            if rc != 0:
                print("WARN: cloud DB pull failed; falling back to existing snapshot.")
        except Exception as e:
            print(f"WARN: could not pull cloud DB ({e}); using existing snapshot.")

    res = analyze(db_path=args.db)
    state = load_state()
    now = datetime.now(timezone.utc)

    # New attributed round-trips are those opening after the high-watermark we last reported.
    hwm = None
    if state.get("last_notified_open_ts"):
        hwm = parse_dt(state["last_notified_open_ts"])
    new_trips = []
    for t in res["attributed"]:
        t_open = parse_dt(t["open_ts"])
        if t_open and (hwm is None or t_open > hwm):
            new_trips.append(t)

    if not should_notify(new_trips, state, now, force=args.force):
        print("Nothing new and not yet due for a weekly heartbeat. Skipping.")
        return 0

    embed = build_embed(res, new_trips, db_path=args.db)

    if args.dry_run:
        def _safe_print(s):
            try:
                print(s)
            except UnicodeEncodeError:
                print(s.encode("ascii", "replace").decode("ascii"))
        _safe_print("=== DRY RUN — would send this embed ===")
        _safe_print(embed["title"])
        _safe_print(embed["description"])
        print()
        print(f"New since last check: {len(new_trips)}")
        print("State would be updated.")
        return 0

    ok = send_discord_embed(embed)
    if not ok:
        print("Failed to send Discord embed. Not updating state.")
        return 1

    # Update state high-watermark to the newest attributed open ts we've now reported.
    if new_trips:
        newest = max(parse_dt(t["open_ts"]) for t in new_trips)
        state["last_notified_open_ts"] = newest.isoformat()
    state["last_sent_at"] = now.isoformat()
    save_state(state)
    print(f"Sent A/B tally to Discord. {len(new_trips)} new round-trips reported. State updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())