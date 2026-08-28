import sys
import os

# Set isolated test database filename before any core modules are imported
os.environ["DATABASE_FILENAME"] = "test_feedback_trades.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import init_db, log_trade, log_strategy_history, get_strategy_before
from core.feedback import (
    compute_closed_round_trips,
    holding_bucket,
    symbol_stats,
    format_symbol_feedback,
    next_strategy_version,
)


def _clean_trades():
    from core.database import get_db_connection
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


def test_fifo_round_trips_and_pnl():
    init_db()
    _clean_trades()
    # Buy 10 @ $10, Buy 5 @ $12, Sell 8 @ $14 (consumes oldest 8 of first buy first).
    log_trade(decision_id=1, alpaca_order_id="a1", symbol="TEST",
              side="buy", qty=10.0, filled_avg_price=10.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="a2", symbol="TEST",
              side="buy", qty=5.0, filled_avg_price=12.0, status="filled")
    log_trade(decision_id=3, alpaca_order_id="a3", symbol="TEST",
              side="sell", qty=8.0, filled_avg_price=14.0, status="filled")

    trips = compute_closed_round_trips()
    test_trips = [t for t in trips if t["symbol"] == "TEST"]
    assert len(test_trips) == 1
    rt = test_trips[0]
    # 8 shares bought @ $10, sold @ $14 -> PnL = 8 * (14-10) = 32
    assert rt["qty"] == 8.0
    assert rt["entry_price"] == 10.0
    assert abs(rt["pnl"] - 32.0) < 1e-6
    assert abs(rt["pnl_pct"] - 40.0) < 1e-6
    assert rt["win"] is True
    _clean_trades()


def test_holding_bucket_partitioning():
    assert holding_bucket(0.5) == "under_4h_whipsaw"
    assert holding_bucket(3.9) == "under_4h_whipsaw"
    assert holding_bucket(4.0) == "4h_to_1d"
    assert holding_bucket(20.0) == "4h_to_1d"
    assert holding_bucket(72.0) == "1d_to_7d"
    assert holding_bucket(8 * 24) == "over_7d"


def test_symbol_stats_winrate_and_buckets():
    init_db()
    _clean_trades()
    # Two whipsaw losers + one longer winner.
    # fill timestamps default to now; manual holding times aren't set by log_trade,
    # so we can't control buckets precisely here. Just assert stats don't crash and
    # reflect a winner.
    log_trade(decision_id=1, alpaca_order_id="w1", symbol="TEST",
              side="buy", qty=1.0, filled_avg_price=100.0, status="filled")
    log_trade(decision_id=2, alpaca_order_id="w2", symbol="TEST",
              side="sell", qty=1.0, filled_avg_price=110.0, status="filled")
    st = symbol_stats("TEST")
    assert st["symbol"] == "TEST"
    assert st["n_trades"] == 1
    assert st["win_rate"] == 100.0
    assert st["expectancy"] > 0
    text = format_symbol_feedback(st)
    assert "Symbol: TEST" in text
    assert "Profit factor" in text
    _clean_trades()


def test_get_strategy_before_returns_prior_rule():
    init_db()
    # Log an old rule, then confirm get_strategy_before returns it for a later ts.
    # Clean up strategy_history for TEST ticker to keep test isolated.
    from core.database import get_db_connection
    with get_db_connection() as conn:
        conn.execute("DELETE FROM strategy_history WHERE ticker='TEST'")
        conn.commit()

    log_strategy_history(
        ticker="TEST",
        yesterdays_rules="OLD",
        todays_rules="RULE-A",
        meta_reasoning="first",
        strategy_version="v1",
    )
    # timestamps auto-set to now; query with a slightly future ts to guarantee <= match.
    rule = get_strategy_before("TEST", "2999-01-01T00:00:00")
    assert rule == "RULE-A"
    with get_db_connection() as conn:
        conn.execute("DELETE FROM strategy_history WHERE ticker='TEST'")
        conn.commit()


def test_next_strategy_version_format():
    v = next_strategy_version()
    assert v.startswith("v")
    assert len(v) >= 8


if __name__ == "__main__":
    tests = [
        test_fifo_round_trips_and_pnl,
        test_holding_bucket_partitioning,
        test_symbol_stats_winrate_and_buckets,
        test_get_strategy_before_returns_prior_rule,
        test_next_strategy_version_format,
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

    # Clean up the isolated test database to avoid leaking into the project root.
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