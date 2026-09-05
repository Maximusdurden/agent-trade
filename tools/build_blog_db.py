#!/usr/bin/env python3
"""Build the blog's ``realized_trades`` mirror from agent-trade data.

This is the **bridge / adapter** between agent-trade and the dexter blog layer.

It maps the canonical closed round-trips (from ``core.feedback``) into the exact
``realized_trades`` schema the blog's report code understands (performance card,
calendar, sidebar, trade grader), then writes them into agent-trade's own DB as a
``realized_trades`` table. The blog Cloud Run job reads this mirror to publish in
Dexter's voice.

Mapping (agent-trade -> dexter contract):
    symbol        -> ticker
    open_ts       -> entry_date  (America/New_York date)
    close_ts      -> exit_date   (America/New_York date)
    qty           -> qty
    entry_price   -> entry_price
    exit_price    -> exit_price
    pnl           -> pnl_dollar
    pnl_pct       -> pnl_percent
    holding_hours -> hold_time_minutes (x60)
    (derived)     -> strategy_name  = get_strategy_before(ticker, open_ts)
    (n/a)         -> chart_filename, parameters, alpaca_order_id (None)

Usage:
    python -m tools.build_blog_db            # build mirror from local DB (dry-run if --dry)
    python -m tools.build_blog_db --dry      # print what would be written, don't touch DB
    python -m tools.build_blog_db --db PATH  # use a specific DB file (e.g. cloud pull)
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.config import DATABASE_PATH
from core import feedback as fb
from core import database as db  # noqa: F401  (for get_strategy_before)


logger = logging.getLogger("BuildBlogDb")

# Mirrors dexter's realized_trades schema (the columns the blog report code reads).
REALIZED_COLUMNS = [
    "ticker", "entry_date", "exit_date", "qty", "entry_price", "exit_price",
    "pnl_dollar", "pnl_percent", "hold_time_minutes", "chart_filename",
    "strategy_name", "parameters", "alpaca_order_id",
]

EASTER_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def _to_et(iso_str: str) -> pd.Timestamp | None:
    if not iso_str:
        return None
    try:
        ts = pd.Timestamp(iso_str)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(EASTER_TZ)
    except (ValueError, TypeError):
        return None


def _to_et_date(iso_str: str) -> str | None:
    ts = _to_et(iso_str)
    return ts.strftime("%Y-%m-%d") if ts is not None else None


def round_trip_to_row(rt: dict) -> dict:
    """Map one closed round-trip dict to a realized_trades row dict."""
    entry_ts = _to_et(rt.get("open_ts"))
    close_ts = _to_et(rt.get("close_ts"))
    symbol = rt.get("symbol")

    # Attach the strategy that governed the entry, if available.
    strategy = None
    if symbol and entry_ts is not None:
        try:
            strategy = db.get_strategy_before(symbol, entry_ts.isoformat())
        except Exception as e:  # defensive: never let attribution break the bridge
            logger.debug("strategy attribution failed for %s: %s", symbol, e)
            strategy = None

    return {
        "ticker": symbol,
        "entry_date": entry_ts.strftime("%Y-%m-%d") if entry_ts is not None else None,
        "exit_date": close_ts.strftime("%Y-%m-%d") if close_ts is not None else None,
        "qty": rt.get("qty"),
        "entry_price": rt.get("entry_price"),
        "exit_price": rt.get("exit_price"),
        "pnl_dollar": rt.get("pnl"),
        "pnl_percent": rt.get("pnl_pct"),
        "hold_time_minutes": (rt.get("holding_hours") or 0.0) * 60.0,
        "chart_filename": None,
        "strategy_name": strategy,
        "parameters": None,
        "alpaca_order_id": None,
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS realized_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    entry_date TEXT,
    exit_date TEXT,
    qty REAL,
    entry_price REAL,
    exit_price REAL,
    pnl_dollar REAL,
    pnl_percent REAL,
    hold_time_minutes REAL,
    chart_filename TEXT,
    strategy_name TEXT,
    parameters TEXT,
    alpaca_order_id TEXT,
    UNIQUE(ticker, entry_date, exit_date)
);
"""


def write_mirror(round_trips: list[dict], db_path: str, dry_run: bool = False) -> int:
    """Replace the realized_trades mirror with the given round-trips.

    Returns the number of rows written (or would-be written if dry_run).
    """
    rows = [round_trip_to_row(rt) for rt in round_trips]
    # De-dupe on (ticker, entry_date, exit_date) keeping the last occurrence.
    seen: dict = {}
    for r in rows:
        if r["ticker"] is None:
            continue
        key = (r["ticker"], r["entry_date"], r["exit_date"])
        seen[key] = r
    rows = list(seen.values())

    if dry_run:
        print(f"[dry-run] would write {len(rows)} realized_trades rows to {db_path}")
        for r in rows[:5]:
            print("  ", r)
        return len(rows)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(_SCHEMA)

        # IMPORTANT: upsert on (ticker, entry_date, exit_date) and preserve the
        # existing row `id` so any grade in realized_trade_grades.trade_id stays
        # linked. A DELETE+re-insert would reassign ids and orphan grades.
        for r in rows:
            cur.execute("""
                INSERT INTO realized_trades
                    (ticker, entry_date, exit_date, qty, entry_price, exit_price,
                     pnl_dollar, pnl_percent, hold_time_minutes, chart_filename,
                     strategy_name, parameters, alpaca_order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker, entry_date, exit_date) DO UPDATE SET
                    qty=excluded.qty, entry_price=excluded.entry_price,
                    exit_price=excluded.exit_price, pnl_dollar=excluded.pnl_dollar,
                    pnl_percent=excluded.pnl_percent,
                    hold_time_minutes=excluded.hold_time_minutes,
                    strategy_name=excluded.strategy_name,
                    parameters=excluded.parameters,
                    alpaca_order_id=COALESCE(excluded.alpaca_order_id,
                                             realized_trades.alpaca_order_id)
            """, (
                r["ticker"], r["entry_date"], r["exit_date"], r["qty"],
                r["entry_price"], r["exit_price"], r["pnl_dollar"],
                r["pnl_percent"], r["hold_time_minutes"], r["chart_filename"],
                r["strategy_name"], r["parameters"], r["alpaca_order_id"],
            ))

        # NOTE: we intentionally do NOT prune old rows. Realized trades are a
        # growing history with grades keyed by trade_id; deleting rows would
        # orphan grades and break historical reporting. Upsert-only keeps ids
        # stable so grades stay linked.
        conn.commit()
        print(f"[ok] upserted {len(rows)} realized_trades rows to {db_path}")
        return len(rows)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the blog realized_trades mirror.")
    parser.add_argument("--dry", action="store_true", help="print plan only; don't touch DB")
    parser.add_argument("--db", default=str(DATABASE_PATH), help="path to agent-trade DB")
    parser.add_argument("--lookback", type=int, default=None,
                        help="only round-trips closed within N days")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    trips = fb.compute_closed_round_trips(lookback_days=args.lookback)
    print(f"closed round-trips: {len(trips)}")
    if not trips:
        print("[warn] no round-trips; nothing to write. (Cloud DB must be pulled first.)")

    n = write_mirror(trips, args.db, dry_run=args.dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())