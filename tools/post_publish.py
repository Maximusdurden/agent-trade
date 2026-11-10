"""Post-publish SEO step: nudge Google, then cross-link the new post.

Runs after a post is published or updated. Two jobs:

1. **Tell Google the sitemap changed.** Google exposes no API for "Request
   Indexing" -- that is UI-only. What you *can* do programmatically is re-submit
   the sitemap, which is how you ask Google to re-fetch it. That is the only
   legitimate automated nudge available.

2. **Cross-link.** Add a "Related reading" block to the new post pointing at
   recent posts, and add a link from the most recent posts back to the new one.
   Internal links are the strongest lever a small site actually controls: they
   spread crawl budget and give Google a reason to revisit.

Everything here is idempotent. Re-running does not duplicate links or blocks.

Usage:
    python -m tools.post_publish --post-id 3257
    python -m tools.post_publish --post-id 3257 --dry
    python -m tools.post_publish --post-id 3257 --no-crosslink
"""

from __future__ import annotations

import argparse
import html as _html
import logging
import os
import re
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import wordpress as wp  # noqa: E402

logger = logging.getLogger("PostPublish")

SITE = "sc-domain:treatmotivated.capital"
SITEMAP = "https://treatmotivated.capital/sitemap_index.xml"

# Markers so the block can be found and replaced rather than duplicated.
BLOCK_START = "<!-- related-reading:start -->"
BLOCK_END = "<!-- related-reading:end -->"

# How many neighbours to link in each direction.
RELATED_COUNT = 3
BACKLINK_COUNT = 3


# ---------------------------------------------------------------------------
# 1. Search Console
# ---------------------------------------------------------------------------
def resubmit_sitemap(dry: bool = False) -> bool:
    """Re-submit the sitemap so Google re-fetches it.

    Returns True on success. Never raises: a Search Console failure must not
    fail the publish.
    """
    if dry:
        logger.info("[DRY] would re-submit %s to %s", SITEMAP, SITE)
        return True

    try:
        # connector-hub owns the OAuth credentials and the write scope.
        hub = Path(os.environ.get("CONNECTOR_HUB_PATH", r"Z:\python\projects\connector-hub"))
        if str(hub) not in sys.path:
            sys.path.insert(0, str(hub))
        from connector_hub.google.searchconsole import submit_sitemap  # noqa: PLC0415
        from connector_hub.providers import get_connector  # noqa: PLC0415

        # get_connector(provider, name) -- provider is the module ("google"),
        # name is the connector group ("searchconsole_write").
        sc = get_connector("google", "searchconsole_write")
        submit_sitemap(sc.service(), SITE, SITEMAP)
        logger.info("re-submitted sitemap: %s", SITEMAP)
        return True
    except Exception as exc:  # noqa: BLE001 - never block publishing
        logger.warning("sitemap re-submit failed (non-fatal): %s", exc)
        return False


# ---------------------------------------------------------------------------
# 2. Cross-linking
# ---------------------------------------------------------------------------
def _fetch_post(post_id: int) -> dict | None:
    r = requests.get(
        f"{wp.WP_URL}/wp-json/wp/v2/posts/{post_id}?context=edit",
        headers=wp.get_auth_header(),
        timeout=30,
    )
    return r.json() if r.status_code == 200 else None


def _recent_posts(exclude_id: int, limit: int = 10) -> list[dict]:
    r = requests.get(
        f"{wp.WP_URL}/wp-json/wp/v2/posts",
        params={"per_page": limit, "status": "publish", "orderby": "date", "order": "desc"},
        headers=wp.get_auth_header(),
        timeout=30,
    )
    if r.status_code != 200:
        return []
    return [p for p in r.json() if p["id"] != exclude_id]


def _title(post: dict) -> str:
    raw = (post.get("title") or {}).get("rendered") or (post.get("title") or {}).get("raw") or ""
    return _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _link(post: dict) -> str:
    return post.get("link") or ""


def _related_block(posts: list[dict]) -> str:
    """Render the 'Related reading' block for the new post."""
    if not posts:
        return ""
    items = "".join(
        f'<li style="margin:0 0 8px 0;">'
        f'<a href="{_link(p)}" style="color:var(--accent, #c8432c); '
        f'font-weight:600; text-decoration:none;">{_html.escape(_title(p))}</a></li>'
        for p in posts
    )
    return (
        f"{BLOCK_START}"
        f'<div style="margin:32px 0 0 0; padding:20px 22px; '
        f'background:var(--surface, #faf6ec); border:1px solid var(--border, #d8cdb8); '
        f'border-radius:12px;">'
        f'<div style="color:var(--muted, #6f6557); font-size:12px; '
        f'text-transform:uppercase; letter-spacing:.06em; margin:0 0 10px 0;">'
        f"Related reading</div>"
        f'<ul style="margin:0; padding-left:18px; color:var(--ink, #221d16); '
        f'line-height:1.6;">{items}</ul>'
        f"</div>"
        f"{BLOCK_END}"
    )


def _strip_block(content: str) -> str:
    """Remove any existing related-reading block so it can be re-rendered."""
    return re.sub(
        re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END),
        "",
        content,
        flags=re.S,
    )


def add_related_to_post(post_id: int, dry: bool = False) -> bool:
    """Append a 'Related reading' block to the post, replacing any old one."""
    post = _fetch_post(post_id)
    if not post:
        logger.warning("could not fetch post %s", post_id)
        return False

    content = (post.get("content") or {}).get("raw") or ""
    neighbours = _recent_posts(exclude_id=post_id, limit=RELATED_COUNT + 1)[:RELATED_COUNT]
    block = _related_block(neighbours)
    if not block:
        logger.info("no neighbours to link; skipping related block")
        return False

    new_content = _strip_block(content).rstrip() + "\n" + block

    if dry:
        logger.info("[DRY] would add %d related links to post %s", len(neighbours), post_id)
        return True

    r = wp.update_post(post_id, {"content": new_content})
    if r.status_code == 200:
        logger.info("added %d related links to post %s", len(neighbours), post_id)
        return True
    logger.warning("related block update failed: %s %s", r.status_code, r.text[:200])
    return False


def add_backlinks_from_recent(new_post: dict, dry: bool = False) -> int:
    """Add a link to the new post from the most recent posts.

    This is the half that actually helps: a brand-new post with no inbound
    internal links is a dead end for a crawler. Linking it from pages Google
    already visits is how it gets discovered.
    """
    new_id = new_post["id"]
    new_link = _link(new_post)
    new_title = _title(new_post)
    if not new_link:
        return 0

    updated = 0
    for post in _recent_posts(exclude_id=new_id, limit=BACKLINK_COUNT):
        content = (post.get("content") or {}).get("raw") or ""
        if new_link in content:
            continue  # already linked

        block = (
            f"{BLOCK_START}"
            f'<div style="margin:28px 0 0 0; padding:16px 20px; '
            f'background:var(--surface, #faf6ec); border-left:4px solid var(--accent, #c8432c); '
            f'border-radius:8px;">'
            f'<span style="color:var(--muted, #6f6557); font-size:12px; '
            f'text-transform:uppercase; letter-spacing:.06em;">Latest</span><br />'
            f'<a href="{new_link}" style="color:var(--accent, #c8432c); '
            f'font-weight:700; text-decoration:none;">{_html.escape(new_title)}</a>'
            f"</div>"
            f"{BLOCK_END}"
        )
        new_content = _strip_block(content).rstrip() + "\n" + block

        if dry:
            logger.info("[DRY] would backlink post %s -> %s", post["id"], new_id)
            updated += 1
            continue

        r = wp.update_post(post["id"], {"content": new_content})
        if r.status_code == 200:
            logger.info("backlinked post %s -> %s", post["id"], new_id)
            updated += 1
        else:
            logger.warning("backlink failed for %s: %s", post["id"], r.status_code)
    return updated


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def run(post_id: int, dry: bool = False, crosslink: bool = True) -> int:
    post = _fetch_post(post_id)
    if not post:
        logger.error("post %s not found", post_id)
        return 1

    logger.info("post %s: %s", post_id, _title(post))

    ok = resubmit_sitemap(dry=dry)

    if crosslink:
        add_related_to_post(post_id, dry=dry)
        n = add_backlinks_from_recent(post, dry=dry)
        logger.info("backlinks written: %d", n)

    return 0 if ok else 0  # sitemap failure is non-fatal by design


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-id", type=int, required=True)
    parser.add_argument("--dry", action="store_true", help="Show what would change.")
    parser.add_argument("--no-crosslink", action="store_true", help="Only re-submit the sitemap.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return run(args.post_id, dry=args.dry, crosslink=not args.no_crosslink)


if __name__ == "__main__":
    raise SystemExit(main())
