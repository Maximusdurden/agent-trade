"""Regression tests for the post-destroying bug of 2026-11-09.

``_recent_posts`` fetched posts WITHOUT ``context=edit``. WordPress then returns
only ``content.rendered`` and omits ``content.raw``, so ``(post["content"] or
{}).get("raw") or ""`` evaluated to ``""``. The backlink step appended its block
to that empty string and wrote it back, replacing the entire post body.

Posts 3252 and 3248 were destroyed this way. These tests pin the two properties
that prevent it:

  1. ``_recent_posts`` must request editable content.
  2. A missing body must abort the write, never be treated as an empty post.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import post_publish  # noqa: E402


# --------------------------------------------------------------------------
# 1. _raw_content must distinguish "no content" from "empty content"
# --------------------------------------------------------------------------
def test_raw_content_returns_none_when_raw_is_absent():
    """A rendered-only payload (no context=edit) must yield None, not ''."""
    post = {"id": 1, "content": {"rendered": "<p>hello</p>", "protected": False}}
    assert post_publish._raw_content(post) is None


def test_raw_content_returns_none_when_content_missing():
    assert post_publish._raw_content({"id": 1}) is None
    assert post_publish._raw_content({"id": 1, "content": None}) is None


def test_raw_content_returns_empty_string_for_a_genuinely_empty_post():
    """An actually-empty post is '' -- and that is different from None."""
    post = {"id": 1, "content": {"raw": "", "rendered": ""}}
    assert post_publish._raw_content(post) == ""


def test_raw_content_returns_the_body():
    post = {"id": 1, "content": {"raw": "<p>real body</p>"}}
    assert post_publish._raw_content(post) == "<p>real body</p>"


# --------------------------------------------------------------------------
# 2. _recent_posts must ask for editable content
# --------------------------------------------------------------------------
def test_recent_posts_requests_edit_context(monkeypatch):
    """Without context=edit the API omits content.raw entirely."""
    captured = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return [{"id": 2, "content": {"raw": "<p>x</p>"}}]

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params or {}
        return FakeResp()

    monkeypatch.setattr(post_publish.requests, "get", fake_get)
    post_publish._recent_posts(exclude_id=1, limit=3)

    assert captured["params"].get("context") == "edit", (
        "context=edit is required or WordPress omits content.raw"
    )


# --------------------------------------------------------------------------
# 3. The write paths must refuse to write when the body is unavailable
# --------------------------------------------------------------------------
def test_backlink_skips_posts_without_editable_content(monkeypatch):
    """A post with no content.raw must be skipped, not overwritten."""
    written = []

    monkeypatch.setattr(
        post_publish,
        "_recent_posts",
        lambda exclude_id, limit=10: [
            {"id": 10, "link": "https://x.test/10/", "content": {"rendered": "<p>a</p>"}},
        ],
    )
    monkeypatch.setattr(
        post_publish.wp,
        "update_post",
        lambda pid, data: written.append((pid, data)) or type("R", (), {"status_code": 200})(),
    )

    new_post = {"id": 99, "link": "https://x.test/99/", "title": {"rendered": "New"}}
    n = post_publish.add_backlinks_from_recent(new_post)

    assert n == 0
    assert written == [], "must not write when content.raw is unavailable"


def test_related_block_skips_when_post_has_no_editable_content(monkeypatch):
    """Same guard on the related-reading path."""
    written = []

    monkeypatch.setattr(
        post_publish,
        "_fetch_post",
        lambda pid: {"id": pid, "content": {"rendered": "<p>a</p>"}},
    )
    monkeypatch.setattr(
        post_publish.wp,
        "update_post",
        lambda pid, data: written.append((pid, data)) or type("R", (), {"status_code": 200})(),
    )

    ok = post_publish.add_related_to_post(99)
    assert ok is False
    assert written == []


def test_backlink_still_works_with_real_content(monkeypatch):
    """The happy path must still append rather than replace."""
    written = []

    monkeypatch.setattr(
        post_publish,
        "_recent_posts",
        lambda exclude_id, limit=10: [
            {"id": 10, "link": "https://x.test/10/", "content": {"raw": "<p>original body</p>"}},
        ],
    )

    class R:
        status_code = 200

    monkeypatch.setattr(
        post_publish.wp,
        "update_post",
        lambda pid, data: written.append((pid, data)) or R(),
    )

    new_post = {"id": 99, "link": "https://x.test/99/", "title": {"rendered": "New"}}
    n = post_publish.add_backlinks_from_recent(new_post)

    assert n == 1
    pid, data = written[0]
    assert pid == 10
    # The original body must survive.
    assert "<p>original body</p>" in data["content"]
    assert "https://x.test/99/" in data["content"]
