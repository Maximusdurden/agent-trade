"""Tests for get_account_state transient-timeout retry (TMCL-937/938)."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.alpaca_client import AlpacaClient


class _FakeAccount:
    cash = 50000.0
    equity = 100000.0
    buying_power = 200000.0


class TestAccountStateRetry(unittest.TestCase):
    def _client(self, trading_client):
        c = AlpacaClient.__new__(AlpacaClient)
        c.is_mock = False
        c.trading_client = trading_client
        c.get_positions = MagicMock(return_value={})
        return c

    def test_transient_timeout_retries_and_succeeds(self):
        """A transient timeout on the first call should retry and succeed."""
        trading = MagicMock()
        # First call raises a transient timeout, second succeeds.
        trading.get_account.side_effect = [
            Exception('{"code":50410000,"message":"request timed out"}'),
            _FakeAccount(),
        ]
        client = self._client(trading)
        with patch("core.alpaca_client.time.sleep", return_value=None):
            result = client.get_account_state()
        self.assertEqual(result["equity"], 100000.0)
        self.assertEqual(result["cash"], 50000.0)
        # Should have retried (2 calls total).
        self.assertEqual(trading.get_account.call_count, 2)

    def test_transient_timeout_all_retries_fail_then_raise(self):
        """If all retries fail, the error should propagate."""
        trading = MagicMock()
        trading.get_account.side_effect = Exception("request timed out")
        client = self._client(trading)
        with patch("core.alpaca_client.time.sleep", return_value=None):
            with self.assertRaises(Exception):
                client.get_account_state()
        # 1 initial + 3 retries = 4 calls.
        self.assertEqual(trading.get_account.call_count, 4)

    def test_non_transient_error_fails_fast(self):
        """A real (non-transient) error should NOT be retried."""
        trading = MagicMock()
        trading.get_account.side_effect = Exception("insufficient buying power")
        client = self._client(trading)
        with patch("core.alpaca_client.time.sleep", return_value=None):
            with self.assertRaises(Exception):
                client.get_account_state()
        # No retry for non-transient errors.
        self.assertEqual(trading.get_account.call_count, 1)


if __name__ == "__main__":
    unittest.main()