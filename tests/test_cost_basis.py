import sys
import os

# Set isolated test database filename before any core modules are imported
os.environ["DATABASE_FILENAME"] = "test_cost_basis.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import init_db, log_trade, reconcile_broker_orders
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
    """Verify the override corrects avg_entry + unrealized from broker-order basis."""
    from core.alpaca_client import AlpacaClient
    client = AlpacaClient.__new__(AlpacaClient)  # bypass __init__ (no network)
    # Stub the order-based cost basis: SOL open 24 @ $109 (correct).
    client._compute_cost_basis_from_orders = lambda: {
        "SOL/USD": {"qty": 24.0, "avg_entry_price": 109.0, "cost_basis": 24.0 * 109.0},
    }
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


def test_override_live_qty_guard():
    """When order-history FIFO qty exceeds live position, cap basis to live qty."""
    from core.alpaca_client import AlpacaClient
    client = AlpacaClient.__new__(AlpacaClient)
    # Stub: order history says 100 SOL open @ $100, but broker only holds 50.
    client._compute_cost_basis_from_orders = lambda: {
        "SOL/USD": {"qty": 100.0, "avg_entry_price": 100.0, "cost_basis": 100.0 * 100.0},
    }
    positions = {
        "SOL/USD": {
            "qty": 50.0,
            "qty_available": 50.0,
            "market_value": 50.0 * 110.0,  # current price $110
            "avg_entry_price": 46.0,
            "unrealized_pnl": (110.0 - 46.0) * 50.0,
        }
    }
    client._apply_fifo_cost_basis_override(positions)
    pos = positions["SOL/USD"]
    # avg entry stays $100, unrealized uses live 50 qty: (110-100)*50 = +500
    assert abs(pos["avg_entry_price"] - 100.0) < 1e-6
    assert abs(pos["unrealized_pnl"] - 500.0) < 1e-6


def test_cost_basis_from_orders_sorted_fifo():
    """Order history arrives newest-first; FIFO must process oldest-first."""
    from core.alpaca_client import AlpacaClient
    client = AlpacaClient.__new__(AlpacaClient)
    # get_executed_orders returns newest-first (reverse chronological).
    client.get_executed_orders = lambda limit=5000: [
        {"symbol": "SOL/USD", "side": "sell", "qty": 5.0, "filled_avg_price": 120.0, "timestamp": "2026-08-28T12:00"},
        {"symbol": "SOL/USD", "side": "buy", "qty": 10.0, "filled_avg_price": 110.0, "timestamp": "2026-08-27T12:00"},
        {"symbol": "SOL/USD", "side": "buy", "qty": 10.0, "filled_avg_price": 100.0, "timestamp": "2026-08-26T12:00"},
    ]
    basis = client._compute_cost_basis_from_orders()
    b = basis["SOL/USD"]
    # Chronological FIFO: sell 5 consumes 5 of the 10@$100 lot -> open 5@$100 + 10@$110 = 15 @ $106.67
    assert abs(b["qty"] - 15.0) < 1e-6
    assert abs(b["avg_entry_price"] - (5 * 100 + 10 * 110) / 15.0) < 1e-6


def test_reconcile_backfills_broker_sells():
    init_db()
    _clean_trades()
    _clear_memo()
    # DB has a buy, but broker also executed a sell (TP/SL fill) not in DB.
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=100.0, filled_avg_price=100.0, status="filled")
    broker_orders = [
        {
            "alpaca_order_id": "broker-sell-1",
            "timestamp": "2026-08-28T12:00:00+00:00",
            "symbol": "SOL/USD",
            "side": "sell",
            "qty": 50.0,
            "filled_avg_price": 120.0,
            "status": "filled",
        }
    ]
    inserted = reconcile_broker_orders(broker_orders)
    assert inserted == 1
    # Now FIFO open should be 50 @ $100 (100 bought - 50 sold)
    _clear_memo()
    basis = compute_open_position_cost_basis()
    b = basis["SOL/USD"]
    assert abs(b["qty"] - 50.0) < 1e-6
    assert abs(b["avg_entry_price"] - 100.0) < 1e-6
    _clean_trades()


def test_reconcile_dedupes_existing_orders():
    init_db()
    _clean_trades()
    _clear_memo()
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="SOL/USD",
              side="buy", qty=100.0, filled_avg_price=100.0, status="filled")
    broker_orders = [
        {"alpaca_order_id": "a1", "timestamp": "2026-08-28T12:00:00+00:00",
         "symbol": "SOL/USD", "side": "buy", "qty": 100.0,
         "filled_avg_price": 100.0, "status": "filled"},
        {"alpaca_order_id": "broker-sell-1", "timestamp": "2026-08-28T12:00:00+00:00",
         "symbol": "SOL/USD", "side": "sell", "qty": 50.0,
         "filled_avg_price": 120.0, "status": "filled"},
    ]
    inserted = reconcile_broker_orders(broker_orders)
    # a1 already exists (deduped), only broker-sell-1 is new
    assert inserted == 1
    _clean_trades()


if __name__ == "__main__":
    tests = [
        test_open_position_cost_basis_simple,
        test_open_position_cost_basis_fifo_partial_sell,
        test_open_position_cost_basis_full_close_absent,
        test_open_position_cost_basis_multiple_symbols,
        test_override_recomputes_unrealized_pnl,
        test_override_live_qty_guard,
        test_cost_basis_from_orders_sorted_fifo,
        test_reconcile_backfills_broker_sells,
        test_reconcile_dedupes_existing_orders,
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