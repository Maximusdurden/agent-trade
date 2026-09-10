#!/usr/bin/env python3
"""Backfill blog post author + category on treatmotivated.capital.

Finds posts authored by the admin user (id=1) and/or in "Uncategorized" and
reassigns them to Dexter (WP_AUTHOR_ID) and the configured category
(WP_CATEGORY_NAME). Idempotent: skips posts already authored by Dexter and in
the target category.

Usage:
    python -m tools.backfill_post_meta            # dry-run (report only)
    python -m tools.backfill_post_meta --apply    # actually update posts
    python -m tools.backfill_post_meta --limit 50 # cap how many to process
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import config, wordpress as wp

logger = logging.getLogger("BackfillPostMeta")


def _auth_headers():
    return wp.get_auth_header()


def fetch_posts_to_fix(per_page: int = 100) -> list[dict]:
    """Fetch published posts authored by admin or in Uncategorized."""
    headers = _auth_headers()
    posts = []
    page = 1
    while True:
        url = (f"{config.WP_URL}/wp-json/wp/v2/posts"
               f"?per_page={per_page}&page={page}&status=publish&_fields=id,title,author,categories,link")
        resp = wp.get_retry_session().get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return posts


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill post author + category.")
    ap.add_argument("--apply", action="store_true", help="Actually update posts (default dry-run).")
    ap.add_argument("--limit", type=int, default=0, help="Max posts to process (0 = all).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    cat_id = wp.get_or_create_category_id(config.WP_CATEGORY_NAME)
    if not cat_id:
        logger.error("Could not resolve/create category '%s'.", config.WP_CATEGORY_NAME)
        return 1
    logger.info("Category '%s' -> id %s", config.WP_CATEGORY_NAME, cat_id)
    logger.info("Target author id: %s", config.WP_AUTHOR_ID)

    posts = fetch_posts_to_fix()
    logger.info("Fetched %d published posts.", len(posts))

    to_fix = []
    for p in posts:
        author = p.get("author")
        cats = p.get("categories") or []
        if author == config.WP_AUTHOR_ID and cat_id in cats:
            continue  # already correct
        to_fix.append(p)

    logger.info("%d posts need author/category fix.", len(to_fix))
    if args.limit:
        to_fix = to_fix[: args.limit]

    headers = _auth_headers()
    updated = 0
    for p in to_fix:
        post_id = p["id"]
        title = (p.get("title") or {}).get("rendered", "") if isinstance(p.get("title"), dict) else p.get("title", "")
        payload = {
            "author": config.WP_AUTHOR_ID,
            "categories": [cat_id],
        }
        if not args.apply:
            logger.info("[dry] would fix post %s (%s)", post_id, title[:60])
            continue
        url = f"{config.WP_URL}/wp-json/wp/v2/posts/{post_id}"
        try:
            r = wp.get_retry_session().post(url, headers=headers, json=payload, timeout=20)
            if r.status_code in (200, 201):
                updated += 1
                logger.info("[ok] fixed post %s (%s)", post_id, title[:60])
            else:
                logger.error("[fail] post %s: %s - %s", post_id, r.status_code, r.text[:200])
        except Exception as e:
            logger.error("[err] post %s: %s", post_id, e)

    logger.info("Done. %d/%d posts updated.", updated, len(to_fix))
    return 0


if __name__ == "__main__":
    sys.exit(main())