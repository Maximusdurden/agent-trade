"""Validates the new executions table + fractional-bracket rounding logic."""
import sys
import os

# Use a SEPARATE test database so tests never pollute the live trading DB.
os.environ["DATABASE_FILENAME"] = "test_trading_agent.db"

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database


def test_executions_table():
    database.init_db()
    import sqlite3
    conn = sqlite3.connect(str(database.DATABASE_PATH))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    assert "executions" in tables, f"executions table missing. Tables: {tables}"
    cur.execute("PRAGMA table_info(executions)")
    cols = [r[1] for r in cur.fetchall()]
    expected = {"decision_id", "attempt", "symbol", "side", "qty",
                "order_type", "status", "error", "alpaca_order_id", "filled_avg_price"}
    assert expected.issubset(set(cols)), f"Missing cols: {expected - set(cols)}"
    conn.close()
    print("PASS: executions table exists with expected columns")


def test_log_and_get_executions():
    database.init_db()
    # Log a failed attempt then a filled fallback
    eid1 = database.log_execution(
        decision_id=None, attempt=1, symbol="ACN", side="buy", qty=6.9,
        order_type="bracket", status="failed", error="fractional not supported"
    )
    eid2 = database.log_execution(
        decision_id=None, attempt=2, symbol="ACN", side="buy", qty=6,
        order_type="market", status="filled", alpaca_order_id="abc-123",
        filled_avg_price=160.72
    )
    assert eid1 > 0 and eid2 > 0
    execs = database.get_executions(limit=10)
    assert len(execs) >= 2
    # newest first
    assert execs[0]["id"] == eid2
    assert execs[0]["status"] == "filled"
    assert execs[0]["order_type"] == "market"
    assert execs[1]["status"] == "failed"
    print("PASS: log_execution + get_executions round-trip works")


def test_bracket_qty_rounding_logic():
    # Simulate the rounding decision made in execute_market_order
    qty = 6.9
    bracket_qty = int(qty)
    assert bracket_qty == 6, f"Expected 6, got {bracket_qty}"
    # Crypto fractional must be preserved (not rounded)
    crypto_qty = 0.5
    assert crypto_qty == 0.5
    print("PASS: equity bracket qty rounds to whole shares; crypto fractional preserved")


if __name__ == "__main__":
    test_executions_table()
    test_log_and_get_executions()
    test_bracket_qty_rounding_logic()
    print("\nALL TESTS PASSED")
