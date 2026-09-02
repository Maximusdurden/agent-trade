import sys
import os

# Isolated DB.
os.environ["DATABASE_FILENAME"] = "test_screener_feedback.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import init_db, get_db_connection, log_trade
from core.screener import get_symbol_feedback, get_symbol_win_rates
import core.feedback as _fb  # noqa: E402


def _clean():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM strategy_history")
        conn.commit()
    # Drop the process-wide 60s FIFO cache so a prior module's memoized round-trips
    # from a DIFFERENT database file don't leak into this test's assertions.
    _fb._memo.clear()


def test_get_symbol_feedback_profit_factor_and_expectancy():
    init_db()
    _clean()
    # One winning and one losing round-trip for the same symbol.
    log_trade(decision_id=1, alpaca_order_id="b1", symbol="AAA",
              side="buy", qty=1.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="s1", symbol="AAA",
              side="sell", qty=1.0, filled_avg_price=120.0, status="filled")
    log_trade(decision_id=3, alpaca_order_id="b2", symbol="AAA",
              side="buy", qty=1.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=4, alpaca_order_id="s2", symbol="AAA",
              side="sell", qty=1.0, filled_avg_price=95.0, status="filled")

    fb = get_symbol_feedback()
    stats = fb.get("AAA")
    assert stats is not None
    assert stats["n_trades"] == 2
    assert stats["win_rate"] == 50.0
    # Gross win = 20, gross loss = 5 -> PF = 4.0
    assert abs(stats["profit_factor"] - 4.0) < 1e-6
    assert stats["expectancy"] > 0

    # Compatibility wrapper returns win rates as fractions (0.0-1.0).
    rates = get_symbol_win_rates()
    assert abs(rates.get("AAA", 0.0) - 0.5) < 1e-6
    _clean()


def test_no_history_symbols_absent():
    init_db()
    _clean()
    fb = get_symbol_feedback()
    assert "NO_HISTORY_YET" not in fb
    _clean()


if __name__ == "__main__":
    tests = [
        test_get_symbol_feedback_profit_factor_and_expectancy,
        test_no_history_symbols_absent,
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

    import gc
    import pathlib
    from core.config import DATABASE_PATH
    gc.collect()
    p = pathlib.Path(DATABASE_PATH)
    try:
        import time
        time.sleep(0.3)
        p.unlink(missing_ok=True)
    except Exception as e:
        print(f"Warning: could not delete test database {p}: {e}")

    sys.exit(1 if failed else 0)