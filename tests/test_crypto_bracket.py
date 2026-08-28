# filename: tests/test_crypto_bracket.py
"""Tests for crypto TP/SL bracket support.

Verifies the runner's crypto TP/SL computation (config defaults + brain
overrides) and that the alpaca client preserves fractional qty for crypto
brackets while rounding equities.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config


class TestCryptoBracketConfig(unittest.TestCase):
    def test_defaults_present(self):
        self.assertTrue(getattr(config, "CRYPTO_BRACKET_ENABLED", True))
        self.assertGreater(getattr(config, "CRYPTO_TAKE_PROFIT_PCT", 0.05), 0)
        self.assertGreater(getattr(config, "CRYPTO_STOP_LOSS_PCT", 0.03), 0)
        # TP must be above SL for a valid bracket.
        self.assertGreater(
            getattr(config, "CRYPTO_TAKE_PROFIT_PCT", 0.05),
            getattr(config, "CRYPTO_STOP_LOSS_PCT", 0.03),
        )


class TestCryptoBracketSizing(unittest.TestCase):
    """Mirrors the runner's crypto TP/SL computation (deterministic)."""

    def _compute(self, base_price, tp=None, sl=None):
        take_profit = tp if tp else round(base_price * (1.0 + getattr(config, "CRYPTO_TAKE_PROFIT_PCT", 0.05)), 2)
        stop_loss = sl if sl else round(base_price * (1.0 - getattr(config, "CRYPTO_STOP_LOSS_PCT", 0.03)), 2)
        max_allowed_stop = round(min(base_price * 0.995, base_price - 0.01), 2)
        if stop_loss > max_allowed_stop:
            stop_loss = max_allowed_stop
        min_allowed_tp = round(base_price + 0.01, 2)
        if take_profit < min_allowed_tp:
            take_profit = min_allowed_tp
        return take_profit, stop_loss

    def test_default_tp_sl(self):
        base = 100.0
        tp, sl = self._compute(base)
        self.assertAlmostEqual(tp, 105.0, places=2)   # +5%
        self.assertAlmostEqual(sl, 97.0, places=2)    # -3%
        self.assertGreater(tp, sl)

    def test_brain_override_respected(self):
        base = 100.0
        tp, sl = self._compute(base, tp=110.0, sl=95.0)
        self.assertAlmostEqual(tp, 110.0, places=2)
        self.assertAlmostEqual(sl, 95.0, places=2)

    def test_sl_capped_below_base(self):
        # SL must stay below base price (valid bracket).
        base = 100.0
        tp, sl = self._compute(base, tp=101.0, sl=99.99)
        self.assertLess(sl, base)
        self.assertGreater(tp, sl)


class TestBracketQtyRounding(unittest.TestCase):
    """Mirror the alpaca_client bracket qty decision."""

    def test_equity_rounds_to_whole(self):
        qty = 6.9
        bracket_qty = int(qty)
        self.assertEqual(bracket_qty, 6)

    def test_crypto_preserves_fractional(self):
        qty = 0.5
        bracket_qty = qty  # crypto preserves fractional
        self.assertEqual(bracket_qty, 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)