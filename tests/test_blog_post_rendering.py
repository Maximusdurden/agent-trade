"""Regression tests for the 2026-11-09 post corruption.

Three bugs shipped in that post:
  1. "Date: $525 2026-11-09" -- the PnL repair regex matched the year inside a
     plain date and rewrote it as a dollar amount.
  2. Four empty metrics rendered as a bare "%" (Alpha vs SPY: %, MAE: %, ...).
  3. Raw prompt scaffolding ("MARKET NEWS:", "LATEST BUYS CONTEXT:") and stray
     "</p>" tags appeared in the published HTML.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.blog_update import (  # noqa: E402
    _render_grade_metrics,
    _render_intro_block,
    verify_pnl_in_body,
)


# --------------------------------------------------------------------------
# 1. PnL repair must never touch a plain date
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        "Date: 2026-11-09",
        "Market update for 2026-11-09.",
        "Total PnL: $525 on 2026-11-09",
        "We closed the book on 2026-11-09 and moved on.",
    ],
)
def test_plain_dates_are_never_rewritten(body):
    """A date with no currency marker must pass through untouched."""
    assert verify_pnl_in_body(body, 525.0, "2026-11-09") == body


def test_real_fusion_bug_is_still_repaired():
    """The original bug this function exists for must still be fixed."""
    out = verify_pnl_in_body("Closed out at -$105-09-18 today.", -105.0, "2026-09-18")
    assert "$105" in out
    assert "2026-09-18" in out
    assert "-$105-09-18" not in out


def test_sign_flip_is_repaired():
    out = verify_pnl_in_body("We finished up $105 on the day.", -105.0, "2026-11-09")
    assert "-$105" in out


def test_positive_pnl_left_alone():
    body = "We finished up $525 on the day."
    assert verify_pnl_in_body(body, 525.0, "2026-11-09") == body


# --------------------------------------------------------------------------
# 2. Empty metrics must not render
# --------------------------------------------------------------------------
def test_empty_metrics_are_hidden():
    """The exact shape the grader emits when benchmark data is missing."""
    html = _render_grade_metrics(
        {
            "grade": "A",
            "composite_score": 92.0,
            "alpha_vs_spy": None,
            "alpha_vs_sector": None,
            "mae_drawdown": None,
            "mfe_capture": None,
        }
    )
    assert "Alpha vs SPY" not in html
    assert "MAE Drawdown" not in html
    assert "MFE Capture" not in html
    # A bare percent sign is the specific symptom that shipped.
    assert ">%<" not in html
    # Real values still render.
    assert "A" in html
    assert "92.0" in html


def test_blank_string_metrics_are_hidden():
    html = _render_grade_metrics(
        {"grade": "B", "alpha_vs_spy": "", "mae_drawdown": "%", "mfe_capture": "nan"}
    )
    assert "Alpha vs SPY" not in html
    assert "MAE Drawdown" not in html
    assert "MFE Capture" not in html


def test_no_grade_info_renders_nothing():
    assert _render_grade_metrics(None) == ""
    assert _render_grade_metrics({}) == ""


def test_real_metrics_do_render():
    html = _render_grade_metrics(
        {"grade": "A", "composite_score": 92.0, "alpha_vs_spy": 1.5, "mfe_capture": 88.0}
    )
    assert "Alpha vs SPY" in html
    assert "1.5%" in html
    assert "MFE Capture" in html
    assert "88.0%" in html


# --------------------------------------------------------------------------
# 3. Intro block must be clean HTML
# --------------------------------------------------------------------------
def test_scaffolding_labels_are_stripped():
    body = (
        "MARKET NEWS:\nThe tape was kind to us today.\n\n"
        "LATEST BUYS CONTEXT:\nNo new buys.\nTickers: ['AMD']"
    )
    html = _render_intro_block("Market Update: 2026-11-09", body, 525.0, "2026-11-09")
    assert "MARKET NEWS" not in html
    assert "LATEST BUYS CONTEXT" not in html
    assert "Tickers:" not in html
    assert "The tape was kind to us today." in html


def test_intro_has_no_stray_closing_tags():
    html = _render_intro_block("T", "One paragraph.\n\nTwo paragraph.", 10.0, "2026-11-09")
    # Every <p> opened must be closed, and no orphan </p> may appear.
    assert html.count("<p ") == html.count("</p>")
    assert html.count("<div") == html.count("</div>")


def test_intro_shows_real_pnl_not_a_date():
    html = _render_intro_block("T", "Body.", 525.0, "2026-11-09")
    assert "$525" in html
    # The corruption symptom: a dollar amount glued to the date.
    assert "$525 2026-11-09" not in html


def test_negative_pnl_uses_loss_colour():
    html = _render_intro_block("T", "Body.", -105.0, "2026-11-09")
    assert "-$105" in html
    assert "var(--loss" in html
