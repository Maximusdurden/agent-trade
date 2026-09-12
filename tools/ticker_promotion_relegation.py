#!/usr/bin/env python3
"""Ticker Promotion / Relegation Engine for agent-trade.

Classifies every ticker in the trading universe into PROMOTE / KEEP / WATCH /
RELEGATE based on decay-weighted performance from the authoritative cloud DB,
and (optionally) edits ``screener_pool.json`` to apply the roster changes.

Tiers (Balanced policy, per plan):
  - PROMOTE : proven winner, not currently in the pool.
  - KEEP    : in pool, performing acceptably.
  - WATCH   : flagged (win/loss asymmetry or marginal), kept in pool.
  - RELEGATE: >= MIN_TRADES RTs AND (win_rate < MIN_WIN_RATE OR expectancy < MIN_EXPECTANCY).

Safety rails:
  - Never relegate a ticker with an open position or currently held.
  - Min sample size prevents knee-jerk removal.
  - ``--dry-run`` (default) prints the proposed diff without writing.

Usage:
  python tools/ticker_promotion_relegation.py            # dry-run, print tiers + diff
  python tools/ticker_promotion_relegation.py --apply    # write screener_pool.json
  python tools/ticker_promotion_relegation.py --json     # machine-readable output
"""
import argparse
import json
import logging
import os
import sys
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

# The roster engine MUST read from the authoritative cloud snapshot
# (cloud_downloaded_trading_agent.db), NOT the stale local trading_agent.db.
# Set DATABASE_FILENAME BEFORE importing core so config.DATABASE_PATH and
# core.database.DATABASE_PATH both resolve to the cloud snapshot.
_CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
os.environ["DATABASE_FILENAME"] = _CLOUD_DB

from core import config  # noqa: E402
from core.feedback import compute_closed_round_trips, symbol_stats  # noqa: E402
from core.screener import load_screener_pool, _filter_supported  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("TickerRoster")

# ---------------------------------------------------------------------------
# Policy knobs (Balanced)
# ---------------------------------------------------------------------------
MIN_TRADES = 5                 # min closed round-trips before any action
MIN_WIN_RATE = 25.0            # below this (decayed) -> relegation candidate
MIN_EXPECTANCY = -50.0         # below this (decayed $) -> relegation candidate
PROMOTE_WIN_RATE = 60.0        # promotion requires >= this win rate
PROMOTE_MIN_TRADES = 5         # promotion requires >= this many RTs
WATCH_ASYMMETRY_WR = 50.0      # WR >= this but negative PnL -> asymmetry watch


def classify_ticker(symbol: str, stats: dict, in_pool: bool, has_open: bool) -> str:
    """Return the tier for one ticker given its decayed stats.

    ``stats`` is the dict from ``core.feedback.symbol_stats`` (or a compatible
    shape with n_trades, win_rate, expectancy, total_pnl).
    """
    n = stats.get("n_trades", 0)
    wr = stats.get("win_rate", 0.0)
    exp = stats.get("expectancy", 0.0)
    total_pnl = stats.get("total_pnl", 0.0)

    # Safety: never relegate a ticker with an open position.
    if has_open:
        return "KEEP"

    # Relegation (Balanced): enough sample AND (low WR OR bad expectancy).
    if n >= MIN_TRADES and (wr < MIN_WIN_RATE or exp < MIN_EXPECTANCY):
        return "RELEGATE"

    # Promotion: proven winner not in the pool.
    if not in_pool and n >= PROMOTE_MIN_TRADES and wr >= PROMOTE_WIN_RATE and exp > 0:
        return "PROMOTE"

    # Watch: win/loss asymmetry (high WR but net negative) or marginal.
    if n >= MIN_TRADES and wr >= WATCH_ASYMMETRY_WR and total_pnl < 0:
        return "WATCH"
    if n >= MIN_TRADES and 25.0 <= wr < 40.0 and exp < 0:
        return "WATCH"

    return "KEEP"


def get_open_positions() -> set:
    """Return the set of symbols with open positions.

    Positions are not stored in a dedicated table; they live in the JSON
    ``portfolio_state`` column of the most recent ``decisions`` row. We parse
    that to find currently-held symbols so we never relegate a held name.

    Reads from the authoritative cloud snapshot (``cloud_downloaded_trading_agent.db``)
    so the roster reflects live cloud state, not the stale local DB.
    """
    db_path = _cloud_db_path()
    if not os.path.exists(db_path):
        return set()
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT portfolio_state FROM decisions "
            "WHERE portfolio_state IS NOT NULL AND portfolio_state != '' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return set()
        state = json.loads(row["portfolio_state"])
        # portfolio_state is typically {"cash":..., "equity":..., "positions": {sym: {qty:...}}}.
        # It may also be a flat dict of {symbol: {qty,...}} or a list.
        if isinstance(state, dict) and isinstance(state.get("positions"), dict):
            return {k for k, v in state["positions"].items() if _qty_positive(v)}
        if isinstance(state, dict):
            return {k for k, v in state.items() if k != "cash" and k != "equity" and _qty_positive(v)}
        if isinstance(state, list):
            return {item.get("symbol") for item in state if _qty_positive(item)}
        return set()
    except Exception as e:
        logger.warning(f"Could not read open positions: {e}")
        return set()


def _qty_positive(v) -> bool:
    """Return True if a position entry has a positive quantity."""
    if isinstance(v, dict):
        qty = v.get("qty", v.get("quantity", v.get("position_qty", 0)))
        try:
            return float(qty) > 0
        except (TypeError, ValueError):
            return False
    return False


def _cloud_db_path() -> str:
    """Return the path to the authoritative cloud snapshot DB.

    The roster engine must read from ``cloud_downloaded_trading_agent.db`` (the
    GCS snapshot), NOT the local ``trading_agent.db`` which is stale/partial.
    """
    return _CLOUD_DB


def compute_ticker_stats() -> dict:
    """Compute decayed per-ticker stats + total PnL from closed round-trips.

    Reads from the authoritative cloud snapshot (DATABASE_FILENAME is set to it
    at import time, so ``compute_closed_round_trips``/``symbol_stats`` already
    read the right DB).
    """
    if not os.path.exists(_CLOUD_DB):
        logger.warning(f"Cloud DB not found at {_CLOUD_DB}. Run tools/pull_cloud_db.py first.")
        return {}

    # Clear the process-wide FIFO memo cache so it doesn't return stale
    # round-trips computed against a different DB file.
    import core.feedback as _fb
    _fb._memo.clear()

    trips = compute_closed_round_trips()
    by_symbol = defaultdict(list)
    for t in trips:
        by_symbol[t["symbol"]].append(t)

    out = {}
    for sym, sym_trips in by_symbol.items():
        st = symbol_stats(sym)
        st["total_pnl"] = sum(t["pnl"] for t in sym_trips)
        out[sym] = st
    return out


def build_roster(stats: dict, pool: list[str], open_positions: set) -> dict:
    """Classify every ticker and return {tier: [symbols]} plus a diff."""
    pool_set = set(pool)
    tiers = defaultdict(list)
    for sym, st in stats.items():
        tier = classify_ticker(sym, st, sym in pool_set, sym in open_positions)
        tiers[tier].append(sym)

    # Sort each tier by total PnL desc for readability.
    for tier in tiers:
        tiers[tier].sort(key=lambda s: stats[s]["total_pnl"], reverse=True)

    # Diff: what to add / remove from the pool.
    to_add = [s for s in tiers["PROMOTE"] if s not in pool_set]
    to_remove = [s for s in tiers["RELEGATE"] if s in pool_set]

    return {
        "tiers": dict(tiers),
        "to_add": to_add,
        "to_remove": to_remove,
        "stats": stats,
    }


def apply_roster(pool: list[str], to_add: list[str], to_remove: list[str]) -> list[str]:
    """Return a new pool list with additions/removals applied (preserving order)."""
    remove_set = set(to_remove)
    new_pool = [s for s in pool if s not in remove_set]
    for s in to_add:
        if s not in new_pool:
            new_pool.append(s)
    return new_pool


def write_pool(pool: list[str]) -> None:
    """Write the pool to screener_pool.json (pretty-printed)."""
    path = config.SCREENER_POOL_PATH
    with open(path, "w") as f:
        json.dump(pool, f, indent=4)
    logger.info(f"Wrote {len(pool)} tickers to {path}")


def format_summary(roster: dict) -> str:
    """Human-readable Discord-style summary of the roster changes."""
    tiers = roster["tiers"]
    lines = []
    if roster["to_add"]:
        lines.append(f"🔺 Promoted: {', '.join(roster['to_add'])}")
    if roster["to_remove"]:
        lines.append(f"🔻 Relegated: {', '.join(roster['to_remove'])}")
    watch = [s for s in tiers.get("WATCH", [])]
    if watch:
        lines.append(f"⚠️ Watch: {', '.join(watch)}")
    if not lines:
        lines.append("No roster changes.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Ticker promotion/relegation engine")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to screener_pool.json (default: dry-run)")
    parser.add_argument("--upload-gcs", action="store_true",
                        help="Also upload the edited pool to GCS (runtime pool)")
    parser.add_argument("--discord", action="store_true",
                        help="Send a Discord roster summary")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON to stdout")
    args = parser.parse_args()

    pool = load_screener_pool()
    stats = compute_ticker_stats()
    open_positions = get_open_positions()
    roster = build_roster(stats, pool, open_positions)

    if args.json:
        print(json.dumps(roster, indent=2, default=str))
        return 0

    print("=== Ticker Roster (Balanced policy) ===")
    for tier in ("PROMOTE", "KEEP", "WATCH", "RELEGATE"):
        syms = roster["tiers"].get(tier, [])
        print(f"\n[{tier}] ({len(syms)})")
        for s in syms:
            st = roster["stats"][s]
            print(f"  {s:<12} n={st['n_trades']:<4} WR={st['win_rate']:>5.1f}% "
                  f"exp=${st['expectancy']:>+9.2f} pnl=${st['total_pnl']:>+10.2f}")

    print("\n=== Proposed pool diff ===")
    print(f"  Add:    {roster['to_add']}")
    print(f"  Remove: {roster['to_remove']}")

    if args.apply:
        new_pool = apply_roster(pool, roster["to_add"], roster["to_remove"])
        write_pool(new_pool)
        print(f"\nApplied: pool {len(pool)} -> {len(new_pool)} tickers.")
        if args.upload_gcs:
            try:
                from core.gcs_sync import upload_screener_pool
                if upload_screener_pool(new_pool):
                    print("Uploaded updated pool to GCS.")
                else:
                    print("WARNING: GCS upload failed; local pool updated only.")
            except Exception as e:
                print(f"WARNING: GCS upload failed: {e}")
    else:
        print("\nDry-run: no changes written. Use --apply to commit.")

    if args.discord:
        try:
            from core.discord_notifier import send_ticker_roster_summary
            send_ticker_roster_summary(roster)
        except Exception as e:
            logger.error(f"Discord summary failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())