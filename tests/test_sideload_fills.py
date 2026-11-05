"""Regression tests: the sideload lane must not lose its own fills on upload.

The 2026-11-04 blog post showed only MSFT because ``sideload/runner_sideload.py``
re-downloaded the GCS DB *after* logging its AMD fills. That download replaces
the local DB file, so the AMD ``trades`` rows were discarded while the AMD
``decisions`` row was explicitly re-inserted — the decision survived, the fills
did not. Broker reconciliation also ran *before* the re-download, so its
backfill was thrown away too.

These tests pin the two invariants that fix it:
  1. Fills captured during a cycle survive a DB-file replacement when replayed.
  2. ``normalize_fill_status`` keeps partially-filled-then-expired orders
     visible to FIFO matching (Alpaca reports those as status=expired with
     filled_qty > 0, which FIFO would otherwise drop).
"""

import os
import sys

# Isolate the test database BEFORE importing core modules (config reads this).
os.environ["DATABASE_FILENAME"] = "test_sideload_fills.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite3

from core import database
from core.database import init_db, log_trade, normalize_fill_status


def _clean_trades():
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


def _rows():
    with database.get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT alpaca_order_id, symbol, side, qty FROM trades ORDER BY id")
        return [tuple(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1. Fills survive the pre-upload GCS re-download
# ---------------------------------------------------------------------------
def test_sideload_fills_survive_gcs_redownload():
    """Reproduce the 11-04 failure, then verify the fix restores the fills."""
    init_db()
    _clean_trades()

    # A pre-existing "normal lane" fill already in the DB.
    log_trade(decision_id=None, alpaca_order_id="pre-existing-1", symbol="MSFT",
              side="sell", qty=9, filled_avg_price=556.71, status="filled")
    assert len(_rows()) == 1

    # The sideload cycle executes an AMD round-trip and captures the rows.
    executed_trade_rows = []
    for oid, side, px in (("amd-buy-1", "buy", 479.48), ("amd-sell-1", "sell", 504.48)):
        log_trade(decision_id=None, alpaca_order_id=oid, symbol="AMD",
                  side=side, qty=17, filled_avg_price=px, status="filled")
        executed_trade_rows.append({
            "alpaca_order_id": oid, "symbol": "AMD", "side": side,
            "qty": 17.0, "filled_avg_price": px, "status": "filled",
        })
    assert len(_rows()) == 3

    # The GCS re-download replaces the DB file: this cycle's rows vanish.
    db_path = str(database.DATABASE_PATH)
    clobber = sqlite3.connect(db_path)
    clobber.execute("DELETE FROM trades WHERE alpaca_order_id LIKE 'amd-%'")
    clobber.commit()
    clobber.close()
    assert len(_rows()) == 1, "precondition: re-download must drop the sideload fills"

    # THE FIX: replay the captured rows before uploading.
    for row in executed_trade_rows:
        log_trade(decision_id=None, alpaca_order_id=row["alpaca_order_id"],
                  symbol=row["symbol"], side=row["side"], qty=row["qty"],
                  filled_avg_price=row["filled_avg_price"], status=row["status"])

    final = _rows()
    assert len(final) == 3, f"expected 3 rows after replay, got {final}"
    assert {r[1] for r in final} == {"AMD", "MSFT"}
    assert {"amd-buy-1", "amd-sell-1"} <= {r[0] for r in final}

    _clean_trades()


def test_replay_is_idempotent():
    """Replaying the same fills twice must not duplicate rows."""
    init_db()
    _clean_trades()

    row = {"alpaca_order_id": "amd-buy-1", "symbol": "AMD", "side": "buy",
           "qty": 17.0, "filled_avg_price": 479.48, "status": "filled"}
    for _ in range(3):
        log_trade(decision_id=None, alpaca_order_id=row["alpaca_order_id"],
                  symbol=row["symbol"], side=row["side"], qty=row["qty"],
                  filled_avg_price=row["filled_avg_price"], status=row["status"])

    assert len(_rows()) == 1, "log_trade must dedupe on alpaca_order_id"
    _clean_trades()


# ---------------------------------------------------------------------------
# 2. Partial fills on terminal orders stay visible to FIFO
# ---------------------------------------------------------------------------
def test_normalize_fill_status_promotes_partial_fills():
    # The real MSFT case: ordered 53, filled 9, then EXPIRED.
    assert normalize_fill_status("expired", 9, 53) == "partially_filled"
    assert normalize_fill_status("canceled", 15, 17) == "partially_filled"
    # Fully filled terminal order.
    assert normalize_fill_status("expired", 9, 9) == "filled"
    # Explicit fill statuses pass through untouched.
    assert normalize_fill_status("filled", 9, 9) == "filled"
    assert normalize_fill_status("partially_filled", 9, 53) == "partially_filled"


def test_normalize_fill_status_keeps_zero_fill_terminal():
    """An order that never traded must keep its terminal status."""
    assert normalize_fill_status("canceled", 0, 17) == "canceled"
    assert normalize_fill_status("expired", 0, 53) == "expired"
    assert normalize_fill_status("canceled", None, None) == "canceled"


def test_reconcile_keeps_partial_fill_visible_to_fifo():
    """A partially-filled-then-expired order must land as a fill status."""
    init_db()
    _clean_trades()

    database.reconcile_broker_orders([{
        "alpaca_order_id": "msft-partial-1",
        "timestamp": "2026-11-04T14:30:11.943393+00:00",
        "symbol": "MSFT",
        "side": "buy",
        "qty": 9.0,
        "ordered_qty": 53.0,
        "filled_qty": 9.0,
        "filled_avg_price": 545.66,
        "status": "expired",
    }])

    with database.get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM trades WHERE alpaca_order_id='msft-partial-1'")
        row = cur.fetchone()
    assert row is not None, "reconcile must insert the partially filled order"
    assert row["status"] in ("filled", "partially_filled"), (
        f"status {row['status']!r} would be dropped by FIFO matching"
    )

    _clean_trades()
