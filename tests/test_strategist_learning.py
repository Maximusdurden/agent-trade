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


if __name__ == "__main__":
    tests = [
        test_feedback_text_contains_per_symbol_and_global_sections,
        test_feedback_text_all_symbols_global_only,
        test_rule_version_format,
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