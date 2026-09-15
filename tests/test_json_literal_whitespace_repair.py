"""Tests for the literal-whitespace-in-string JSON repair (TMCL-946..962).

Regression: the strategist LLM (OpenRouter) emitted JSON whose string values
contained LITERAL newlines/tabs (e.g. a multi-line ``meta_reasoning``). The
sanitize step strips most control chars but deliberately keeps ``\\n``/``\\r``,
so ``json.loads`` failed with ``Expecting ',' delimiter: line 2 column NNN`` —
the exact failure signature of the 17 strategist tickets. The fix escapes
literal whitespace inside quoted string values string-aware before any other
heuristic.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm_client import (
    _repair_literal_whitespace_in_strings,
    _repair_truncated_json,
)


class TestLiteralWhitespaceRepair(unittest.TestCase):
    def test_escapes_literal_newline_inside_string(self):
        # A literal newline inside a quoted value is invalid JSON.
        raw = '{"meta_reasoning": "line one\nline two", "todays_rules": "hold"}'
        repaired = _repair_literal_whitespace_in_strings(raw)
        # The newline must be escaped so json.loads succeeds.
        parsed = json.loads(repaired)
        self.assertEqual(parsed["meta_reasoning"], "line one\nline two")
        self.assertEqual(parsed["todays_rules"], "hold")

    def test_escapes_literal_tab_inside_string(self):
        raw = '{"a": "col1\tcol2", "b": 1}'
        repaired = _repair_literal_whitespace_in_strings(raw)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["a"], "col1\tcol2")

    def test_does_not_touch_escaped_newline(self):
        # A properly escaped \\n must be left alone.
        raw = '{"a": "line one\\nline two", "b": 1}'
        repaired = _repair_literal_whitespace_in_strings(raw)
        self.assertEqual(repaired, raw)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["a"], "line one\nline two")

    def test_does_not_touch_whitespace_outside_strings(self):
        # Whitespace between tokens is valid JSON and must be preserved.
        raw = '{\n  "a": 1,\n  "b": [1, 2]\n}'
        repaired = _repair_literal_whitespace_in_strings(raw)
        self.assertEqual(repaired, raw)
        json.loads(repaired)  # must still parse

    def test_no_change_returns_original_identity(self):
        raw = '{"a": "clean", "b": 2}'
        self.assertIs(_repair_literal_whitespace_in_strings(raw), raw)

    def test_empty_and_none(self):
        self.assertEqual(_repair_literal_whitespace_in_strings(""), "")
        self.assertIsNone(_repair_literal_whitespace_in_strings(None))


class TestTruncatedRepair(unittest.TestCase):
    def test_truncated_mid_string(self):
        raw = '{"meta_reasoning": "unterminated'
        repaired = _repair_truncated_json(raw)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["meta_reasoning"], "unterminated")

    def test_truncated_mid_object(self):
        raw = '{"a": 1, "b": {"c": 2'
        repaired = _repair_truncated_json(raw)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["b"]["c"], 2)

    def test_not_json_returns_none(self):
        self.assertIsNone(_repair_truncated_json("not json at all"))


if __name__ == "__main__":
    unittest.main()