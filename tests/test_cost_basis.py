import sys
import os

# Set isolated test database filename before any core modules are imported
os.environ["DATABASE_FILENAME"] = "test_cost_basis.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import init_db, log_trade
from core.feedback import compute_open_position_cost_basis, _memo


def _clean_trades():
    from core.database import get_db_connection
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


def _clear_memo():
    _memo.clear()


def test_open_position_cost_basis_simple():
    init_db()
    _clean_trades()
    _clear_memo()
    # Buy 10 @ $100, buy 10 @ $110 -> open 20 @ avg $105
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=110.0, status="filled")
    basis = compute_open_position_cost_basis()
    b = basis["SOL/USD"]
    assert abs(b["qty"] - 20.0) < 1e-6
    assert abs(b["avg_entry_price"] - 105.0) < 1e-6
    assert abs(b["cost_basis"] - 2100.0) < 1e-6
    _clean_trades()


def test_open_position_cost_basis_fifo_partial_sell():
    init_db()
    _clean_trades()
    _clear_memo()
    # Buy 10 @ $100, buy 10 @ $110, sell 5 @ $120 (consumes 5 of first lot)
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=110.0, status="filled")
    log_trade(decision_id=3, alpaca_order_id="a3", symbol="SOL/USD",
              side="sell", qty=5.0, filled_avg_price=120.0, status="filled")
    basis = compute_open_position_cost_basis()
    b = basis["SOL/USD"]
    # Remaining: 5 @ $100 + 10 @ $110 = 15 @ avg $106.6667
    assert abs(b["qty"] - 15.0) < 1e-6
    assert abs(b["avg_entry_price"] - (5 * 100 + 10 * 110) / 15.0) < 1e-6
    assert abs(b["cost_basis"] - (5 * 100 + 10 * 110)) < 1e-6
    _clean_trades()


def test_open_position_cost_basis_full_close_absent():
    init_db()
    _clean_trades()
    _clear_memo()
    # Buy 10 @ $100, sell 10 @ $120 -> fully closed, no open position
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="SOL/USD",
              side="sell", qty=10.0, filled_avg_price=120.0, status="filled")
    basis = compute_open_position_cost_basis()
    assert "SOL/USD" not in basis
    _clean_trades()


def test_open_position_cost_basis_multiple_symbols():
    init_db()
    _clean_trades()
    _clear_memo()
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=10.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="BTC/USD",
              side="buy", qty=1.0, filled_avg_price=50000.0, status="filled")
    basis = compute_open_position_cost_basis()
    assert "SOL/USD" in basis
    assert "BTC/USD" in basis
    assert abs(basis["BTC/USD"]["avg_entry_price"] - 50000.0) < 1e-6
    _clean_trades()


def test_override_recomputes_unrealized_pnl():
    """Verify the override logic in alpaca_client corrects avg_entry + unrealized."""
    from core.alpaca_client import AlpacaClient
    init_db()
    _clean_trades()
    _clear_memo()
    # Simulate the SOL scenario: open lots at ~$109, broker wrongly says $46.
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=12.0, filled_avg_price=109.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="SOL/USD",
              side="buy", qty=12.0, filled_avg_price=109.0, status="filled")

    client = AlpacaClient.__new__(AlpacaClient)  # bypass __init__ (no network)
    positions = {
        "SOL/USD": {
            "qty": 24.0,
            "qty_available": 24.0,
            "market_value": 24.0 * 108.0,  # current price $108
            "avg_entry_price": 46.0,       # broker's wrong value
            "unrealized_pnl": (108.0 - 46.0) * 24.0,  # broker's inflated value
        }
    }
    client._apply_fifo_cost_basis_override(positions)
    pos = positions["SOL/USD"]
    # Correct avg entry = $109, correct unrealized = (108-109)*24 = -24
    assert abs(pos["avg_entry_price"] - 109.0) < 1e-6
    assert abs(pos["unrealized_pnl"] - (-24.0)) < 1e-6
    _clean_trades()


if __name__ == "__main__":
    tests = [
        test_open_position_cost_basis_simple,
        test_open_position_cost_basis_fifo_partial_sell,
        test_open_position_cost_basis_full_close_absent,
        test_open_position_cost_basis_multiple_symbols,
        test_override_recomputes_unrealized_pnl,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed.")