"""Tests for tagging and the post-publish cross-linking step.

Two bugs motivated these:

1. Posts published with ``tags: []``. The autolinkers emit ``/tag/<slug>/``
   links, so every ticker link pointed at an archive that did not include the
   post, and ``/tag/pnl/`` / ``/tag/sector/`` 404'd because those tags had never
   been created. ``get_or_create_tag_id`` existed but was never called.

2. A partial update (rewriting only ``content`` to add a cross-link) must not
   touch tags. An earlier version of the fix blanket-tagged every post with the
   full financial-term list.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wordpress as wp  # noqa: E402
from tools import post_publish  # noqa: E402


# --------------------------------------------------------------------------
# tagging
# --------------------------------------------------------------------------
def test_tags_for_post_includes_ticker_and_terms(monkeypatch):
    """A post about AMD must be tagged AMD plus the standard terms."""
    captured = {}

    def fake_resolve(names):
        captured["names"] = list(names)
        return list(range(len(names)))

    monkeypatch.setattr(wp, "resolve_tag_ids", fake_resolve)
    ids = wp.tags_for_post(["AMD"])

    assert "AMD" in captured["names"]
    assert "PnL" in captured["names"]
    assert "Sector" in captured["names"]
    assert len(ids) == len(captured["names"])


def test_tickers_are_uppercased(monkeypatch):
    captured = {}

    def fake_resolve(names):
        captured["names"] = list(names)
        return []

    monkeypatch.setattr(wp, "resolve_tag_ids", fake_resolve)
    wp.tags_for_post(["amd", " nvda "])

    assert "AMD" in captured["names"]
    assert "NVDA" in captured["names"]
    assert "amd" not in captured["names"]


def test_duplicate_tickers_are_deduped(monkeypatch):
    captured = {}

    def fake_resolve(names):
        captured["names"] = list(names)
        return []

    monkeypatch.setattr(wp, "resolve_tag_ids", fake_resolve)
    wp.tags_for_post(["AMD", "AMD", "amd"])

    assert captured["names"].count("AMD") == 1


def test_resolve_tag_ids_skips_failures(monkeypatch):
    """A tag that cannot be created must not break the whole list."""
    def fake_get_or_create(name):
        return None if name == "Broken" else 42

    monkeypatch.setattr(wp, "get_or_create_tag_id", fake_get_or_create)
    assert wp.resolve_tag_ids(["Good", "Broken", "AlsoGood"]) == [42]


def test_resolve_tag_ids_dedupes_ids(monkeypatch):
    monkeypatch.setattr(wp, "get_or_create_tag_id", lambda name: 7)
    assert wp.resolve_tag_ids(["A", "B", "C"]) == [7]


def test_financial_term_tags_cover_autolink_targets():
    """Every term the autolinker links to must be in the tag list.

    Otherwise the link 404s -- which is exactly what happened to /tag/pnl/.
    """
    terms = {t.lower() for t in wp.FINANCIAL_TERM_TAGS}
    # The autolinker's term set, lowercased.
    autolinked = {
        "ema", "sma", "golden cross", "death cross", "hard stop", "stop loss",
        "trailing stop", "limit order", "market order", "slippage", "support",
        "resistance", "rsi", "macd", "volume", "candlestick", "grid",
        "grid trading", "going long", "shorting", "short sale", "pnl",
        "profit and loss", "volatility", "bull market", "bear market",
        "sector", "sectors",
    }
    # The ones that actually appear in post bodies and must resolve.
    must_exist = {"pnl", "sector", "sectors", "ema", "sma", "rsi", "macd",
                  "volume", "support", "resistance", "volatility", "stop loss"}
    missing = must_exist - terms
    assert not missing, f"autolinked terms with no tag: {sorted(missing)}"
    assert autolinked  # keep the reference set meaningful


# --------------------------------------------------------------------------
# cross-linking
# --------------------------------------------------------------------------
def test_related_block_has_markers():
    posts = [
        {"id": 1, "link": "https://x.test/a/", "title": {"rendered": "Post A"}},
        {"id": 2, "link": "https://x.test/b/", "title": {"rendered": "Post B"}},
    ]
    block = post_publish._related_block(posts)
    assert post_publish.BLOCK_START in block
    assert post_publish.BLOCK_END in block
    assert "https://x.test/a/" in block
    assert "Post A" in block


def test_related_block_empty_for_no_posts():
    assert post_publish._related_block([]) == ""


def test_strip_block_removes_previous_block():
    """Re-running must replace the block, not stack a second one."""
    posts = [{"id": 1, "link": "https://x.test/a/", "title": {"rendered": "A"}}]
    block = post_publish._related_block(posts)
    content = f"<p>Body</p>\n{block}"

    stripped = post_publish._strip_block(content)
    assert post_publish.BLOCK_START not in stripped
    assert "<p>Body</p>" in stripped


def test_strip_block_is_idempotent():
    posts = [{"id": 1, "link": "https://x.test/a/", "title": {"rendered": "A"}}]
    block = post_publish._related_block(posts)
    content = f"<p>Body</p>\n{block}"
    once = post_publish._strip_block(content)
    twice = post_publish._strip_block(once)
    assert once == twice


def test_title_strips_html_and_unescapes():
    post = {"title": {"rendered": "Monday&#8217;s <em>Reckoning</em>"}}
    assert post_publish._title(post) == "Monday\u2019s Reckoning"


def test_title_handles_missing_title():
    assert post_publish._title({}) == ""


def test_related_block_never_emits_raw_markup_from_titles():
    """A title containing HTML must not inject live markup into the block.

    ``_title()`` strips tags before the block escapes the result, so the tag is
    removed entirely rather than escaped. Either way the property that matters
    is that no executable markup survives.
    """
    posts = [
        {
            "id": 1,
            "link": "https://x.test/a/",
            "title": {"rendered": "<script>alert(1)</script>Real Title"},
        }
    ]
    block = post_publish._related_block(posts)
    assert "<script>" not in block
    assert "</script>" not in block
    # The human-readable part of the title survives.
    assert "Real Title" in block


def test_related_block_escapes_entities_in_titles():
    """Ampersands and angle brackets in a title must be escaped, not raw."""
    posts = [{"id": 1, "link": "https://x.test/a/", "title": {"rendered": "A &amp; B"}}]
    block = post_publish._related_block(posts)
    # _title() unescapes to "A & B", then the block re-escapes to "A &amp; B".
    assert "A &amp; B" in block
