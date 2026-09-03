import sys
import os

os.environ["DATABASE_FILENAME"] = "test_strategist_learning.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.feedback import feedback_text, next_strategy_version


def test_feedback_text_contains_per_symbol_and_global_sections():
    text = feedback_text(symbol="SOL/USD")
    assert "STRUCTURED PERFORMANCE FEEDBACK" in text
    assert "PORTFOLIO-GLOBAL CONTEXT" in text
    assert "COGNITIVE LESSONS FOR RULE ADAPTATION" in text
    # Should reference a concrete testable knob suggestion.
    assert "guardrail-adjustable" in text


def test_feedback_text_all_symbols_global_only():
    text = feedback_text()
    assert "STRUCTURED PERFORMANCE FEEDBACK" in text
    assert "PORTFOLIO-GLOBAL CONTEXT" in text


def test_rule_version_format():
    v = next_strategy_version()
    assert v.startswith("v")


def test_options_feedback_text_has_leverage_lessons():
    from core.feedback import options_feedback_text
    text = options_feedback_text()
    assert "OPTIONS PERFORMANCE FEEDBACK" in text
    assert "LEVERAGE" in text
    assert "conviction" in text.lower()


def test_options_strategy_track_uses_special_ticker_key():
    from core.strategist import MetaStrategist
    from unittest.mock import MagicMock, patch

    client = MagicMock()
    client.get_positions.return_value = {
        "NVDA": {"qty": 30},
        "NVDA261016C00230000": {"qty": 4, "is_option": True},
    }
    client.get_account_state.return_value = {"equity": 100000.0, "cash": 50000.0}
    client.get_historical_bars.return_value = MagicMock(empty=True)

    strategist = MetaStrategist.__new__(MetaStrategist)
    strategist.is_mock = True
    strategist.ab_model = None

    with patch("core.strategist.database.log_strategy_history", return_value=1) as mock_log:
        with patch("core.strategist.database.get_active_strategy", return_value="No active strategy rules defined for OPTIONS/NVDA."):
            refined = strategist.run_option_strategy_refinement(client)

    # NVDA is in OPTIONS_UNIVERSE, so it should be refined.
    assert "NVDA" in refined
    # The strategy must be logged under the special OPTIONS/<underlying> key.
    logged_keys = [c.kwargs.get("ticker") or (c.args[0] if c.args else None) for c in mock_log.call_args_list]
    assert any(k == "OPTIONS/NVDA" for k in logged_keys)


if __name__ == "__main__":
    tests = [
        test_feedback_text_contains_per_symbol_and_global_sections,
        test_feedback_text_all_symbols_global_only,
        test_rule_version_format,
        test_options_feedback_text_has_leverage_lessons,
        test_options_strategy_track_uses_special_ticker_key,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed.")

    import gc
    import pathlib
    from core.config import DATABASE_PATH
    gc.collect()
    p = pathlib.Path(DATABASE_PATH)
    try:
        import time
        time.sleep(0.3)
        p.unlink(missing_ok=True)
    except Exception as e:
        print(f"Warning: could not delete test database {p}: {e}")

    sys.exit(1 if failed else 0)