"""Tests for the anti-whipsaw timezone normalization fix (TMCL-895).

The anti-whipsaw guardrail subtracts the last trade's timestamp from
``datetime.utcnow()``. When the stored timestamp carries a timezone offset
(broker-reconcile timestamps can be '+00:00' or 'Z'), ``fromisoformat`` returns
an AWARE datetime, and subtracting it from a NAIVE ``utcnow`` raises
"can't subtract offset-naive and offset-aware datetimes". These tests patch
``database.get_recent_trades`` to return offset timestamps and assert the
guardrail no longer errors.
"""

import os
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_FILENAME"] = "test_antiwhipsaw_tz.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.guardrails import RiskGuardrails


def _account(equity=100000.0):
    return {"equity": equity, "cash": 60000.0, "unrealized_pnl": 0.0, "last_equity": equity}


def _decision(symbol, action="SELL", qty=10.0, price=100.0):
    return {
        "action": action,
        "symbol": symbol,
        "quantity": qty,
        "current_price": price,
        "conviction": 0.7,
        "direction": "bullish",
    }


class TestAntiWhipsawTimezone(unittest.TestCase):
    def setUp(self):
        self.guardrails = RiskGuardrails()

    @patch("core.database.get_recent_trades")
    def test_aware_utc_offset_timestamp_no_error(self, mock_trades):
        """A '+00:00'-offset recent trade must NOT raise (naive/aware subtraction)."""
        aware_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        mock_trades.return_value = [{
            "timestamp": aware_ts,
            "symbol": "NVDA",
            "side": "buy",
            "status": "filled",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("NVDA", action="SELL"), _account(), {}
        )
        # The trade was 1 minute ago (BUY) vs a SELL -> whipsaw block expected, but
        # importantly there must be NO exception. The guardrail returns normally.
        self.assertFalse(approved)
        self.assertIn("anti-whipsaw", msg.lower())

    @patch("core.database.get_recent_trades")
    def test_z_suffix_timestamp_no_error(self, mock_trades):
        """A 'Z'-suffixed recent trade must NOT raise."""
        z_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        mock_trades.return_value = [{
            "timestamp": z_ts,
            "symbol": "NVDA",
            "side": "buy",
            "status": "filled",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("NVDA", action="SELL"), _account(), {}
        )
        self.assertFalse(approved)
        self.assertIn("anti-whipsaw", msg.lower())

    @patch("core.database.get_recent_trades")
    def test_naive_timestamp_still_works(self, mock_trades):
        """A plain naive timestamp behaves exactly as before (regression guard)."""
        naive_ts = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
        mock_trades.return_value = [{
            "timestamp": naive_ts,
            "symbol": "NVDA",
            "side": "buy",
            "status": "filled",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("NVDA", action="SELL"), _account(), {}
        )
        self.assertFalse(approved)
        self.assertIn("anti-whipsaw", msg.lower())


if __name__ == "__main__":
    unittest.main()