"""Tests for the missing-comma JSON repair (TMCL-997..1003).

Regression: the brain's 16-ticker ``decisions`` array is large (>8KB) and the
model intermittently DROPS the comma between adjacent array elements (``}{``)
or between an object member and the next key (``}"``). ``json.loads`` then
fails with ``Expecting ',' delimiter: line N column M`` — the exact failure
signature of the TMCL-997..1003 brain tickets. The fix inserts the missing
comma string-aware.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm_client import _repair_missing_comma


class TestMissingCommaRepair(unittest.TestCase):
    def test_inserts_comma_between_array_elements(self):
        # Two adjacent decision objects with NO comma between them.
        raw = ('{"decisions": ['
               '{"symbol": "AAPL", "action": "HOLD"}'
               '{"symbol": "NVDA", "action": "BUY"}]}')
        repaired = _repair_missing_comma(raw)
        parsed = json.loads(repaired)
        self.assertEqual(len(parsed["decisions"]), 2)
        self.assertEqual(parsed["decisions"][0]["symbol"], "AAPL")
        self.assertEqual(parsed["decisions"][1]["symbol"], "NVDA")

    def test_inserts_comma_between_members(self):
        # Missing comma between an object member (object value) and the next key.
        raw = '{"a": {"x": 1} "b": 2}'
        repaired = _repair_missing_comma(raw)
        parsed = json.loads(repaired)
        self.assertEqual(parsed, {"a": {"x": 1}, "b": 2})

    def test_inserts_comma_between_array_and_object(self):
        # `]` followed directly by `{` (next member) with no comma.
        raw = '{"x": [1, 2] "y": 3}'
        repaired = _repair_missing_comma(raw)
        parsed = json.loads(repaired)
        self.assertEqual(parsed, {"x": [1, 2], "y": 3})

    def test_does_not_touch_valid_json(self):
        raw = '{"decisions": [{"a": 1}, {"b": 2}], "meta": "ok"}'
        repaired = _repair_missing_comma(raw)
        self.assertEqual(repaired, raw)
        json.loads(repaired)  # must still parse

    def test_does_not_touch_comma_inside_string(self):
        # A `}` or `]` inside a quoted string value must NOT get a comma.
        raw = '{"thought": "use {x} and [y]", "action": "HOLD"}'
        repaired = _repair_missing_comma(raw)
        self.assertEqual(repaired, raw)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["thought"], "use {x} and [y]")

    def test_does_not_touch_escaped_quote(self):
        raw = '{"a": "say \\"hi\\"", "b": 1}'
        repaired = _repair_missing_comma(raw)
        self.assertEqual(repaired, raw)
        json.loads(repaired)

    def test_no_change_returns_original_identity(self):
        raw = '{"a": 1, "b": [1, 2]}'
        self.assertIs(_repair_missing_comma(raw), raw)

    def test_empty_and_none(self):
        self.assertEqual(_repair_missing_comma(""), "")
        self.assertIsNone(_repair_missing_comma(None))

    def test_full_brain_style_response(self):
        # Simulate the large brain response with a dropped comma between two
        # decision objects (the TMCL-997..1003 signature).
        raw = (
            '{"decisions": ['
            '{"symbol": "AAPL", "action": "HOLD", "quantity": 0.0, '
            '"direction": "neutral", "conviction": 0.5, '
            '"thought_process": "Ranging, no edge"}'
            '{"symbol": "NVDA", "action": "BUY", "quantity": 10.0, '
            '"direction": "bullish", "conviction": 0.8, '
            '"thought_process": "Momentum above VWAP"}]}'
        )
        repaired = _repair_missing_comma(raw)
        parsed = json.loads(repaired)
        self.assertEqual(len(parsed["decisions"]), 2)
        self.assertEqual(parsed["decisions"][1]["action"], "BUY")


if __name__ == "__main__":
    unittest.main()