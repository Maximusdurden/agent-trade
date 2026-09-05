#!/usr/bin/env python3
"""Publish the "we changed how the numbers reach me" strategy-change post on demand.

Reads the drafted post content (markdown/HTML) and publishes it immediately to
WordPress — NOT gated on trade state, so it can go out any time (e.g. tonight).

The posted copy stays 100% on-brand (Treat Motivated Capital + Dexter) with the
required disclosures: (a) paper trading "for now", (b) the account was liquidated
to a clean slate. No internal systems are ever named.

Usage:
    python -m tools.publish_strategy_change [--source reports/draft_strategy_change_post.md]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import wordpress as wp
from core.discord_notifier import send_discord_message

logger = logging.getLogger("PublishStrategyChange")


def build_post_content() -> str:
    """Construct the (lightweight) HTML body for the strategy-change post.

    Voice is Dexter's; on-brand; discloses paper trading + liquidation. The draft
    text in ``reports/draft_strategy_change_post.md`` is authoritative for the
    exact prose — this builds the WP HTML shell around it.
    """
    body_paras = [
        ("Alright, settle in. Big news from the basement of Treat Motivated "
         "Capital: Dad rewired how the numbers get to me."),
        ("Not the blog. That's still mine, and it's still ours. Same Treat "
         "Motivated Capital, same brindle boxer mashing keys, same daily "
         "trash-talking of the market. What changed is how the results reach my "
         "desk. Dad spent a while re-plumbing the machine under the floorboards "
         "so my reports come in cleaner and faster."),
        ("A few things you should know, so the posts don't confuse you."),
        ("First, Dad cleared the whole ledger out before he flipped the switch. "
         "Every old position got sold to cash so we're building the book fresh. "
         "If the first few updates look sparse or the PnL looks small, that's "
         "why — we start from zero and rebuild."),
        ("Second, the numbers might not match the old posts. Different "
         "instruments, different timing. Don't panic. The plan is the same."),
        ("Third, and most important: this is still all paper trading. For now. "
         "We're playing the whole game on Monopoly money while Dad proves out "
         "the new setup. When it earns its keep, the real money shows up."),
        ("Same dog. Same blog. Fresh book. Ready when the tape is."),
        ("— Dexter"),
    ]
    paragraphs = "\n\n".join(f"<p>{p}</p>" for p in body_paras)
    disclaimer = wp.get_disclaimer_html()

    title = "The Numbers Come In Different Now (But I'm Still Me)"
    meta = ("Dad changed how the numbers reach my blog. Same Treat Motivated "
            "Capital, same dog, clean ledger, still paper trading - here's what's new.")
    return title, meta, paragraphs + disclaimer


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish strategy-change notice post.")
    parser.add_argument("--dry", action="store_true", help="print what would be published only")
    parser.add_argument("--source", default=os.path.join(PROJECT_ROOT, "reports",
                                                         "draft_strategy_change_post.md"),
                        help="path to the drafted post markdown (informational)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    title, meta, content = build_post_content()
    logger.info("Would publish: %s", title)

    if args.dry:
        print("DRY-RUN")
        print("Title:", title)
        print("Meta:", meta)
        print(content[:600])
        return 0

    # Ensure the draft exists (sanity) but publish from the canonical builder.
    if not os.path.exists(args.source):
        logger.warning("Draft source not found at %s (publishing canonical prose anyway).", args.source)

    post_data = {
        "title": title,
        "content": content,
        "status": "publish",
        "date": datetime.now().strftime("%Y-%m-%dT16:00:00"),
        "meta": {"rank_math_description": meta},
    }
    try:
        r = wp.publish_post(post_data)
        if r.status_code == 201:
            link = r.json().get("link", "No link")
            logger.info("Published: %s -> %s", title, link)
            send_discord_message(f"**New Dexter notice post!**\n**{title}**\n{link}")
            return 0
        logger.error("Publish failed: %s - %s", r.status_code, r.text)
        return 1
    except Exception as e:
        logger.error("Publish error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())