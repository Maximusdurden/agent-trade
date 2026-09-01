"""Tests for the OpenRouter -> Gemini cross-provider fallback.

Regression: when OpenRouter HANGS (times out at the executor level), the old
code gave up and fell back to rule-based trading. Now generate_structured
retries Gemini directly before giving up, keeping the LLM decision path alive
during a transient OpenRouter outage.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm_client import SharedLLMClient


def _make_client():
    client = SharedLLMClient.__new__(SharedLLMClient)
    client.gemini_client = MagicMock(name="gemini_client")
    client.tier_mapping = {"daily_driver": "google/gemini-2.5-flash"}
    client.gemini_model = "gemini-2.5-flash"
    client.max_retries = 1
    client.retry_delay = 1
    client.max_backoff = 2
    return client


class TestGeminiCrossProviderFallback(unittest.TestCase):
    def test_falls_back_to_gemini_when_openrouter_hangs(self):
        client = _make_client()
        # Simulate OpenRouter HANGING: it never returns fast. With the bounded
        # executor + small budget, the attempt will time out (not raise).
        def slow_execution(*a, **k):
            import time
            time.sleep(30)  # far longer than the budget
        client._execute_completion = slow_execution

        mock_resp = MagicMock()
        mock_resp.text = '{"key": "value"}'
        client.gemini_client.models.generate_content.return_value = mock_resp

        with patch.dict(os.environ, {"LLM_MAX_TOTAL_SECONDS": "3"}), \
             patch.object(client, "max_retries", 0):
            # max_retries=0 lets the FIRST timeout immediately try Gemini.
            result = client.generate_structured(prompt="test", response_model=dict, tier="daily_driver")

        self.assertEqual(result, {"key": "value"})
        self.assertTrue(client.gemini_client.models.generate_content.called)


if __name__ == "__main__":
    unittest.main()