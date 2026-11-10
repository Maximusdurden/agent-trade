"""Post Dexter's comment on the 2026-11-09 post, linking the honeypot.

Idempotent: if a comment by Dexter already links the honeypot, it is left alone.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wordpress as wp  # noqa: E402

POST_ID = 3257
HONEYPOT = "https://ai-honeypot-dc6gtgwbrq-ue.a.run.app/bestiary"

COMMENT = (
    "Okay so this is the weird part. Dad has been running this thing in the "
    "background for a while now and I finally got him to show me the output. "
    "It's a honeypot &mdash; a fake site that logs every bot that crawls it &mdash; "
    "and then it <em>names</em> them. Like Linnaeus. Every crawler gets a Latin "
    "binomial and a little portrait drawn from its own fingerprint, so the same "
    "bot always looks the same. "
    f'<a href="{HONEYPOT}" rel="noopener">You can walk through the bestiary here</a>. '
    "My favourite so far is <em>Furtim headlesschromensis</em> &mdash; a browser "
    "that swears it is a person and trips over its own webdriver flag. "
    "I have opinions about all of them. Mostly that they do not smell like anything."
)


def main() -> int:
    headers = wp.get_auth_header()

    existing = requests.get(
        f"{wp.WP_URL}/wp-json/wp/v2/comments",
        params={"post": POST_ID, "per_page": 100},
        headers=headers,
        timeout=30,
    ).json()

    for c in existing:
        if HONEYPOT in (c.get("content") or {}).get("rendered", ""):
            print(f"comment already present (id={c['id']}); nothing to do")
            return 0

    resp = requests.post(
        f"{wp.WP_URL}/wp-json/wp/v2/comments",
        headers=headers,
        json={"post": POST_ID, "content": COMMENT, "status": "approved"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"FAILED {resp.status_code}: {resp.text[:400]}")
        return 1

    data = resp.json()
    print(f"created comment id={data['id']} status={data['status']}")
    print(f"author={data.get('author_name')!r}")
    print(f"link={data.get('link')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
