import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_rule_selectivity.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.strategy_rules import validate_strategy_rule


class TestRuleSelectivityGate(unittest.TestCase):
    """The PG failure mode: a rule true 'almost always' churns. The gate rejects
    rules whose entry thresholds are so loose they'd fire on noise."""

    def test_pg_style_vwap_noise_rule_rejected(self):
        # The actual PG rule: vwap_dist < +0.5% is inside the noise band.
        valid, reason = validate_strategy_rule(
            "PG",
            "IF the intraday vwap_dist_pct is less than +0.5% AND the 14-period "
            "RSI is below 65, THEN the execution engine may initiate a starter "
            "position in PG with a maximum allocation of 2.0% of total portfolio "
            "equity.",
        )
        self.assertFalse(valid)
        self.assertIn("rule_not_selective", reason)

    def test_pg_style_compact_rule_rejected(self):
        valid, reason = validate_strategy_rule(
            "PG", "IF vwap_dist_pct < +0.5% AND RSI < 65 THEN buy PG."
        )
        self.assertFalse(valid)
        self.assertIn("rule_not_selective", reason)

    def test_loose_rsi_cap_rejected(self):
        # RSI < 60 is true most of the time -> churn-prone.
        valid, reason = validate_strategy_rule(
            "KO", "Buy KO when vwap_dist is less than +0.8% and RSI below 60."
        )
        self.assertFalse(valid)
        self.assertIn("rule_not_selective", reason)

    def test_selective_pullback_rule_accepted(self):
        # Tight RSI band + real VWAP pullback -> selective, accepted.
        valid, reason = validate_strategy_rule(
            "NVDA",
            "Buy NVDA only when RSI is below 40 and price pulls back to support "
            "at least 1.5% below VWAP.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_selective_rsi_band_accepted(self):
        valid, reason = validate_strategy_rule(
            "JNJ",
            "Buy JNJ when RSI is between 35 and 45 and price is at least 1.2% "
            "below VWAP.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_selective_vwap_threshold_accepted(self):
        # vwap_dist > +1.5% is a real threshold (not noise).
        valid, reason = validate_strategy_rule(
            "MSFT",
            "IF vwap_dist_pct > +1.5% THEN no new BUY; buy MSFT on pullback to "
            "support with RSI below 45.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_crypto_exempt_from_selectivity_gate(self):
        # Crypto has bracket TP/SL and different noise characteristics -> exempt.
        valid, reason = validate_strategy_rule(
            "SOL/USD",
            "Buy SOL when vwap_dist is less than +0.5% and RSI below 65.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    def test_missing_rule_still_rejected(self):
        valid, reason = validate_strategy_rule("AAPL", "")
        self.assertFalse(valid)
        self.assertEqual(reason, "missing_rule")


if __name__ == "__main__":
    unittest.main()