"""Tests for the transient-error retry fix in llm_client (TMCL-896..902).

Regression: when OpenRouter returned a transient 503/429/connection error, the
old retry loop only caught ``concurrent.futures.TimeoutError``. A 503 surfaced
as a generic exception from ``future.result()`` and bubbled straight to the
outer except, giving up immediately (and, with Gemini also 503ing, producing
"backoff would exceed 180s" / "empty rule" failures). Now transient errors are
retried with backoff, and only non-transient errors are re-raised.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm_client import SharedLLMClient, LLMClientError


def _make_client():
    client = SharedLLMClient.__new__(SharedLLMClient)
    client.gemini_client = MagicMock(name="gemini_client")
    client.tier_mapping = {"daily_driver": "google/gemini-2.5-flash"}
    client.gemini_model = "gemini-2.5-flash"
    client.max_retries = 2
    client.retry_delay = 0  # no sleep in tests
    client.max_backoff = 0
    return client


class TestTransientErrorRetry(unittest.TestCase):
    def test_is_transient_error_detects_503(self):
        client = _make_client()
        self.assertTrue(client._is_transient_error(
            Exception("503 UNAVAILABLE: This model is currently experiencing high demand")))
        self.assertTrue(client._is_transient_error(
            Exception("429 Too Many Requests: rate limit exceeded")))
        self.assertTrue(client._is_transient_error(
            Exception("Connection reset by peer")))
        self.assertFalse(client._is_transient_error(
            Exception("401 Unauthorized: invalid api key")))
        self.assertFalse(client._is_transient_error(
            Exception("400 Bad Request: schema mismatch")))

    def test_transient_error_retries_then_succeeds(self):
        """A 503 on the first attempt should be retried, not fatal."""
        client = _make_client()
        calls = {"n": 0}

        def flaky_execution(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMClientError("503 UNAVAILABLE: high demand")
            return '{"key": "value"}'

        client._execute_completion = flaky_execution
        with patch.dict(os.environ, {"LLM_MAX_TOTAL_SECONDS": "30"}):
            result = client.generate_structured(prompt="test", response_model=dict, tier="daily_driver")
        self.assertEqual(result, {"key": "value"})
        self.assertEqual(calls["n"], 2, "should have retried once after the 503")

    def test_non_transient_error_not_retried(self):
        """A 401 (auth) is not transient and must NOT be retried."""
        client = _make_client()
        calls = {"n": 0}

        def auth_fail(*a, **k):
            calls["n"] += 1
            raise LLMClientError("401 Unauthorized: invalid api key")

        client._execute_completion = auth_fail
        with patch.dict(os.environ, {"LLM_MAX_TOTAL_SECONDS": "30"}):
            with self.assertRaises(LLMClientError):
                client.generate_structured(prompt="test", response_model=dict, tier="daily_driver")
        self.assertEqual(calls["n"], 1, "non-transient error should not be retried")


if __name__ == "__main__":
    unittest.main()