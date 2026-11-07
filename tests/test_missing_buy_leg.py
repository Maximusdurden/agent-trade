"""Regression test: a sell with no matching BUY lot produces no round-trip.

Why this matters
----------------
The 2026-11-06 AMD BUY 19 @ 438.08 filled at the broker but never reached the
``trades`` table (Cloud Run was still running the 2026-10-14 image, so the
6b134f3 fix was not live). The blog only reports CLOSED round-trips, so the
missing buy leg is invisible *today* — but FIFO matches sells against open BUY
lots, so when AMD is eventually sold the sell would match nothing, produce no
round-trip, and vanish from the blog.

These tests pin that failure mode and prove the backfill fixes it:

  1. A sell with NO matching buy lot yields 0 round-trips.
  2. The same sell WITH the buy leg present yields exactly 1 round-trip with
     the correct PnL.
  3. FIFO is CHRONOLOGICAL: logging the buy AFTER the sell silently yields 0
     round-trips even though both rows exist. This is the trap that makes a
     naive "both rows are in the DB" check pass while PnL stays wrong.
"""

import os
import sys

# Isolate the test database BEFORE importing core modules (config reads this).
os.environ["DATABASE_FILENAME"] = "test_missing_buy_leg.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database
from core.database import init_db, log_trade
from core.feedback import compute_closed_round_trips, _memo

# The real 2026-11-06 fill, and a hypothetical exit for it.
BUY_TS = "2026-11-06T17:46:31.625829+00:00"
SELL_TS = "2026-11-06T20:15:00.000000+00:00"
BUY_PRICE = 438.08
SELL_PRICE = 450.00
QTY = 19.0


def _clean_trades():
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()
    _memo.clear()


def _log(oid, side, qty, price, ts):
    """Log a fill with an explicit timestamp (log_trade stamps 'now')."""
    log_trade(decision_id=None, alpaca_order_id=oid, symbol="AMD",
              side=side, qty=qty, filled_avg_price=price, status="filled")
    # log_trade writes datetime.utcnow(); overwrite with the intended fill time
    # so FIFO ordering is deterministic and matches the broker's real sequence.
    with database.get_db_connection() as conn:
        conn.execute("UPDATE trades SET timestamp = ? WHERE alpaca_order_id = ?",
                     (ts, oid))
        conn.commit()
    _memo.clear()


def test_sell_without_buy_leg_produces_no_round_trip():
    """The bug: a sell whose buy leg is missing matches nothing."""
    init_db()
    _clean_trades()

    _log("amd-sell-orphan", "sell", QTY, SELL_PRICE, SELL_TS)

    trips = compute_closed_round_trips()
    assert trips == [], f"orphan sell must produce 0 round-trips, got {trips}"

    _clean_trades()


def test_sell_with_buy_leg_produces_one_round_trip():
    """The fix: with the buy leg backfilled, the sell closes a round-trip."""
    init_db()
    _clean_trades()

    # Buy MUST be logged before the sell — FIFO is chronological.
    _log("amd-buy-1", "buy", QTY, BUY_PRICE, BUY_TS)
    _log("amd-sell-1", "sell", QTY, SELL_PRICE, SELL_TS)

    trips = compute_closed_round_trips()
    assert len(trips) == 1, f"expected exactly 1 round-trip, got {trips}"

    rt = trips[0]
    assert rt["symbol"] == "AMD"
    assert abs(rt["qty"] - QTY) < 1e-9
    assert abs(rt["entry_price"] - BUY_PRICE) < 1e-9
    assert abs(rt["exit_price"] - SELL_PRICE) < 1e-9
    expected_pnl = QTY * (SELL_PRICE - BUY_PRICE)
    assert abs(rt["pnl"] - expected_pnl) < 1e-6, (
        f"pnl {rt['pnl']} != {expected_pnl}"
    )
    assert rt["win"] is True

    _clean_trades()


def test_out_of_order_logging_yields_no_round_trip():
    """FIFO is chronological: a buy stamped AFTER the sell matches nothing.

    ``log_trade`` stamps ``datetime.utcnow()``, so logging the sell first gives
    it the earlier timestamp. Both rows then exist in the DB — a naive
    row-count check passes — yet FIFO processes the sell against an empty buy
    queue and produces no round-trip. This is the trap: "both rows are present"
    is NOT sufficient, the buy must carry an earlier fill timestamp.
    """
    init_db()
    _clean_trades()

    # Deliberately wrong order, using log_trade's own utcnow() stamps so the
    # sell genuinely precedes the buy in time.
    log_trade(decision_id=None, alpaca_order_id="amd-sell-1", symbol="AMD",
              side="sell", qty=QTY, filled_avg_price=SELL_PRICE, status="filled")
    log_trade(decision_id=None, alpaca_order_id="amd-buy-1", symbol="AMD",
              side="buy", qty=QTY, filled_avg_price=BUY_PRICE, status="filled")
    _memo.clear()

    with database.get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM trades")
        assert int(cur.fetchone()["n"]) == 2, "precondition: both rows exist"
        cur.execute("SELECT alpaca_order_id, timestamp FROM trades ORDER BY timestamp")
        ordered = [r["alpaca_order_id"] for r in cur.fetchall()]
        assert ordered == ["amd-sell-1", "amd-buy-1"], (
            f"precondition: sell must sort before buy, got {ordered}"
        )

    trips = compute_closed_round_trips()
    assert trips == [], (
        "a buy stamped after the sell must not retroactively match it; "
        f"got {trips}"
    )

    _clean_trades()
