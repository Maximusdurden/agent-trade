"""The post-publish step must run after a publish and never break it.

Wiring SEO into the publish path is only safe if a failure there cannot turn a
successful publish into a failed one. These tests pin that contract.

NOTE on patching: ``blog_update._post_publish`` does ``from tools import
post_publish``, which resolves the *attribute* on the ``tools`` package rather
than consulting ``sys.modules``. Patching ``sys.modules["tools.post_publish"]``
therefore has no effect once the real module has been imported -- the first
version of this file did exactly that and silently exercised the real module
(which tried to hit WordPress). These tests patch ``run`` on the real module,
which exercises the actual import path.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import blog_update, post_publish  # noqa: E402


@pytest.fixture()
def captured(monkeypatch):
    """Replace post_publish.run with a recorder; yields the list of ids."""
    seen: list[int] = []

    def fake_run(post_id, dry=False, crosslink=True):
        seen.append(post_id)
        return 0

    monkeypatch.setattr(post_publish, "run", fake_run)
    monkeypatch.delenv("HONEYPOT_SKIP_POST_PUBLISH", raising=False)
    return seen


def test_post_publish_is_called_with_the_new_id(captured):
    """A successful publish must hand the new post id to the SEO step."""
    blog_update._post_publish(4242)
    assert captured == [4242]


def test_post_publish_failure_is_swallowed(monkeypatch):
    """A raising SEO step must not propagate -- the publish already succeeded."""

    def boom(post_id, dry=False, crosslink=True):
        raise RuntimeError("search console is down")

    monkeypatch.setattr(post_publish, "run", boom)
    monkeypatch.delenv("HONEYPOT_SKIP_POST_PUBLISH", raising=False)

    # Must not raise.
    blog_update._post_publish(1)


def test_post_publish_skips_without_an_id(captured):
    """No id means nothing to link; do not call the step at all."""
    blog_update._post_publish(None)
    blog_update._post_publish(0)
    assert captured == []


def test_post_publish_can_be_disabled(captured, monkeypatch):
    """The escape hatch must actually prevent the call."""
    monkeypatch.setenv("HONEYPOT_SKIP_POST_PUBLISH", "1")
    blog_update._post_publish(99)
    assert captured == []


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_skip_flag_accepts_common_truthy_values(captured, monkeypatch, value):
    monkeypatch.setenv("HONEYPOT_SKIP_POST_PUBLISH", value)
    blog_update._post_publish(7)
    assert captured == []


def test_skip_flag_ignores_falsy_values(captured, monkeypatch):
    """An explicit '0' must NOT disable the step."""
    monkeypatch.setenv("HONEYPOT_SKIP_POST_PUBLISH", "0")
    blog_update._post_publish(8)
    assert captured == [8]
