import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_equity_entry_gates.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _clean():
    with get_db_connection() as conn:
        for t in ("watchlist_history", "trades"):
            try:
                conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        conn.commit()


def _set_watchlist(syms):
    import json
    with get_db_connection() as conn:
        conn.execute("INSERT INTO watchlist_history (timestamp, watchlist) VALUES (?, ?)",
                     ("2026-08-01 12:00:00", json.dumps(syms)))
        conn.commit()


def _insert_trade(symbol, side, qty, price, ts):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, filled_avg_price, status) "
            "VALUES (?, ?, ?, ?, ?, 'filled')",
            (ts, symbol, side, qty, price),
        )
        conn.commit()


def _today_ts(hours_ago: int) -> str:
    """Return a timestamp string for TODAY (UTC) that is ``hours_ago`` in the past.

    The open-buy cap guardrail counts trades whose timestamp starts with today's
    UTC date (datetime.utcnow().strftime('%Y-%m-%d')). We generate timestamps
    relative to datetime.utcnow() (naive UTC) and CLAMP to today's date so the
    date ALWAYS matches the guardrail's "today" — subtracting hours from now can
    roll to yesterday when it's early in the UTC day (e.g. now=00:14, minus 1h
    = 23:14 yesterday), which silently made the trades invisible to the cap.
    Previously the tests hardcoded 2026-09-13, which went stale and broke
    test_at_cap_blocked.
    """
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    ts = now - timedelta(hours=hours_ago)
    if ts.strftime("%Y-%m-%d") != now.strftime("%Y-%m-%d"):
        # Rolled to yesterday: clamp to just after midnight today so the trade
        # still counts toward today's cap.
        ts = datetime.strptime(now.strftime("%Y-%m-%d") + " 00:00:01",
                               "%Y-%m-%d %H:%M:%S")
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _yesterday_ts(hours_ago: int) -> str:
    """Return a timestamp string for YESTERDAY (UTC) that is ``hours_ago`` in the past."""
    from datetime import datetime, timedelta
    ts = datetime.utcnow() - timedelta(days=1, hours=hours_ago)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


class TestEquityRsiEntryGate(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        _set_watchlist(["MSFT", "NVDA", "KO"])
        import core.feedback as fb
        fb._memo.clear()

    def _buy(self, symbol, rsi=None, qty=1.0, price=100.0):
        indicators = {}
        if rsi is not None:
            indicators["rsi_14"] = rsi
        decision = {"action": "BUY", "symbol": symbol, "quantity": qty,
                    "current_price": price, "conviction": 0.8,
                    "direction": "bullish", "instrument": "stock",
                    "indicators": indicators}
        guardrails = RiskGuardrails()
        guardrails.is_market_open_check = lambda: (True, "open")
        return guardrails.validate_and_adjust_decision(
            decision,
            {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0},
            {},
        )

    def test_low_rsi_buy_allowed(self):
        # RSI 40 (pullback-to-support) is the documented edge -> allowed
        ok, msg, adj = self._buy("MSFT", rsi=40.0)
        self.assertTrue(ok, f"RSI 40 should be allowed: {msg}")

    def test_high_rsi_buy_blocked(self):
        # RSI 55 (momentum chase) is where KO/PG (0% win) live -> blocked
        ok, msg, adj = self._buy("KO", rsi=55.0)
        self.assertFalse(ok, f"RSI 55 should be blocked: {msg}")
        self.assertIn("RSI entry gate", msg)

    def test_boundary_rsi_equal_max_blocked(self):
        # RSI == EQUITY_RSI_ENTRY_MAX (50) is at/above the gate -> blocked
        ok, msg, adj = self._buy("NVDA", rsi=50.0)
        self.assertFalse(ok, f"RSI 50 (== max) should be blocked: {msg}")

    def test_crypto_exempt_from_rsi_gate(self):
        # Crypto is 24/7, no RSI mean-reversion edge -> exempt even at high RSI
        ok, msg, adj = self._buy("SOL/USD", rsi=80.0)
        self.assertTrue(ok, f"Crypto should be exempt from RSI gate: {msg}")

    def test_no_rsi_indicator_no_gate(self):
        # RSI gated (None) -> no entry gate, falls through to other checks
        ok, msg, adj = self._buy("MSFT", rsi=None)
        self.assertTrue(ok, f"No RSI should not trip the gate: {msg}")


class TestEquityOpenBuyCap(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        _set_watchlist(["MSFT", "NVDA"])
        import core.feedback as fb
        fb._memo.clear()

    def _buy(self, symbol, qty=1.0, price=100.0):
        decision = {"action": "BUY", "symbol": symbol, "quantity": qty,
                    "current_price": price, "conviction": 0.8,
                    "direction": "bullish", "instrument": "stock",
                    "indicators": {"rsi_14": 40.0}}
        guardrails = RiskGuardrails()
        guardrails.is_market_open_check = lambda: (True, "open")
        return guardrails.validate_and_adjust_decision(
            decision,
            {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0},
            {},
        )

    def test_under_cap_allowed(self):
        # 2 open buys today (cap=3) -> still allowed
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(1))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(2))
        ok, msg, adj = self._buy("MSFT")
        self.assertTrue(ok, f"2 open buys < cap 3 should be allowed: {msg}")

    def test_at_cap_blocked(self):
        # 3 open buys today (cap=3) -> blocked
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(1))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(2))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(3))
        ok, msg, adj = self._buy("MSFT")
        self.assertFalse(ok, f"3 open buys == cap 3 should be blocked: {msg}")
        self.assertIn("Open-buy churn cap", msg)

    def test_sell_closes_open_buy(self):
        # 3 buys but 1 sell closes one -> 2 open -> allowed. The sell is the
        # OLDEST trade so the most recent trade is a BUY; the anti-whipsaw
        # guardrail only blocks a BUY after a recent SELL, so it won't interfere
        # with verifying the open-buy cap logic.
        _insert_trade("MSFT", "sell", 1.0, 101.0, _today_ts(8))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(5))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(6))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _today_ts(7))
        ok, msg, adj = self._buy("MSFT")
        self.assertTrue(ok, f"Sell closes an open buy -> 2 open should be allowed: {msg}")

    def test_previous_day_buys_do_not_count(self):
        # Buys from a prior day don't count toward today's cap
        _insert_trade("MSFT", "buy", 1.0, 100.0, _yesterday_ts(1))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _yesterday_ts(2))
        _insert_trade("MSFT", "buy", 1.0, 100.0, _yesterday_ts(3))
        ok, msg, adj = self._buy("MSFT")
        self.assertTrue(ok, f"Prior-day buys should not count: {msg}")

    def test_crypto_exempt_from_open_buy_cap(self):
        # Crypto exempt from the open-buy churn cap
        _insert_trade("SOL/USD", "buy", 1.0, 100.0, _today_ts(1))
        _insert_trade("SOL/USD", "buy", 1.0, 100.0, _today_ts(2))
        _insert_trade("SOL/USD", "buy", 1.0, 100.0, _today_ts(3))
        _insert_trade("SOL/USD", "buy", 1.0, 100.0, _today_ts(4))
        ok, msg, adj = self._buy("SOL/USD")
        self.assertTrue(ok, f"Crypto should be exempt from open-buy cap: {msg}")


class TestEquityRsiEntryGateAB(unittest.TestCase):
    """Entry-gate A/B: alternates the RSI threshold per day and stamps it."""

    def setUp(self):
        init_db()
        _clean()
        _set_watchlist(["MSFT", "NVDA", "KO"])
        import core.feedback as fb
        fb._memo.clear()
        self._orig_ab = getattr(config, "EQUITY_RSI_ENTRY_AB_VALUES", "")
        self._orig_max = getattr(config, "EQUITY_RSI_ENTRY_MAX", 50)

    def tearDown(self):
        config.EQUITY_RSI_ENTRY_AB_VALUES = self._orig_ab
        config.EQUITY_RSI_ENTRY_MAX = self._orig_max

    def _buy(self, symbol, rsi=None, qty=1.0, price=100.0):
        indicators = {}
        if rsi is not None:
            indicators["rsi_14"] = rsi
        decision = {"action": "BUY", "symbol": symbol, "quantity": qty,
                    "current_price": price, "conviction": 0.8,
                    "direction": "bullish", "instrument": "stock",
                    "indicators": indicators}
        guardrails = RiskGuardrails()
        guardrails.is_market_open_check = lambda: (True, "open")
        return guardrails.validate_and_adjust_decision(
            decision,
            {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0},
            {},
        )

    def test_ab_alternates_and_stamps(self):
        """With A/B values set, the gate alternates per day and stamps the value."""
        config.EQUITY_RSI_ENTRY_AB_VALUES = "50,45"
        g = RiskGuardrails()
        # The active value is deterministic per UTC date; just verify it's one of
        # the two configured values and that it's stamped on the decision.
        active = g._active_rsi_entry_max()
        self.assertIn(active, (50.0, 45.0))
        ok, msg, adj = self._buy("MSFT", rsi=40.0)
        self.assertTrue(ok, f"RSI 40 should be allowed: {msg}")
        self.assertIn("entry_gate", adj)
        self.assertTrue(adj["entry_gate"].startswith("rsi_max="))

    def test_ab_no_values_uses_default(self):
        """Without A/B values, the gate uses EQUITY_RSI_ENTRY_MAX."""
        config.EQUITY_RSI_ENTRY_AB_VALUES = ""
        config.EQUITY_RSI_ENTRY_MAX = 50
        g = RiskGuardrails()
        self.assertEqual(g._active_rsi_entry_max(), 50.0)

    def test_ab_single_value_uses_default(self):
        """A single A/B value (not two) falls back to EQUITY_RSI_ENTRY_MAX."""
        config.EQUITY_RSI_ENTRY_AB_VALUES = "50"
        config.EQUITY_RSI_ENTRY_MAX = 50
        g = RiskGuardrails()
        self.assertEqual(g._active_rsi_entry_max(), 50.0)


if __name__ == "__main__":
    unittest.main()