import sys
import os

# Isolated DB.
os.environ["DATABASE_FILENAME"] = "test_ticker_roster.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.ticker_promotion_relegation import (
    classify_ticker, build_roster, apply_roster,
    MIN_TRADES, MIN_WIN_RATE, MIN_EXPECTANCY,
)


def _stats(n, wr, exp, total_pnl):
    return {
        "n_trades": n,
        "win_rate": wr,
        "expectancy": exp,
        "total_pnl": total_pnl,
    }


def test_relegate_chronic_loser():
    # >=5 RTs, WR < 25% -> RELEGATE
    assert classify_ticker("GOOG", _stats(14, 0.0, -140.0, -1967.0), in_pool=True, has_open=False) == "RELEGATE"


def test_relegate_bad_expectancy():
    # >=5 RTs, WR ok but expectancy < -50 -> RELEGATE
    assert classify_ticker("AMD", _stats(10, 30.0, -57.0, -573.0), in_pool=True, has_open=False) == "RELEGATE"


def test_keep_held_position_never_relegated():
    # Even a chronic loser with an open position must be KEPT (safety rail).
    assert classify_ticker("KO", _stats(45, 0.0, -8.6, -389.0), in_pool=True, has_open=True) == "KEEP"


def test_promote_proven_winner_not_in_pool():
    # Not in pool, >=5 RTs, WR >=60%, positive expectancy -> PROMOTE
    assert classify_ticker("JNJ", _stats(44, 98.0, 18.8, 829.0), in_pool=False, has_open=False) == "PROMOTE"


def test_keep_in_pool_winner():
    # In pool and performing well -> KEEP
    assert classify_ticker("BTC/USD", _stats(25, 96.0, 196.0, 4909.0), in_pool=True, has_open=False) == "KEEP"


def test_watch_asymmetry():
    # High WR but net negative PnL, expectancy above relegation bar -> WATCH
    assert classify_ticker("NVDA", _stats(49, 67.0, -20.5, -1006.0), in_pool=True, has_open=False) == "WATCH"
    # A high-WR negative-PnL name that doesn't meet relegation expectancy bar -> WATCH
    assert classify_ticker("XYZ", _stats(10, 60.0, -10.0, -50.0), in_pool=True, has_open=False) == "WATCH"


def test_insufficient_sample_no_action():
    # <5 RTs -> KEEP regardless of performance (no knee-jerk).
    assert classify_ticker("LTC/USD", _stats(1, 100.0, 349.0, 349.0), in_pool=False, has_open=False) == "KEEP"


def test_apply_roster_adds_and_removes():
    pool = ["AAPL", "GOOG", "MSFT"]
    new_pool = apply_roster(pool, to_add=["JNJ"], to_remove=["GOOG"])
    assert "GOOG" not in new_pool
    assert "JNJ" in new_pool
    assert new_pool == ["AAPL", "MSFT", "JNJ"]


def test_build_roster_tiers_and_diff():
    stats = {
        "GOOG": _stats(14, 0.0, -140.0, -1967.0),
        "JNJ": _stats(44, 98.0, 18.8, 829.0),
        "MSFT": _stats(9, 100.0, 28.6, 257.0),
    }
    pool = ["GOOG", "MSFT"]
    roster = build_roster(stats, pool, open_positions=set())
    assert "GOOG" in roster["to_remove"]
    assert "JNJ" in roster["to_add"]
    assert roster["tiers"]["RELEGATE"] == ["GOOG"]
    assert roster["tiers"]["PROMOTE"] == ["JNJ"]
    assert roster["tiers"]["KEEP"] == ["MSFT"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))