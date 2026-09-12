#!/usr/bin/env python3
"""Cloud Run job entrypoint for the Dexter blog update.

Wraps ``tools.blog_update`` so the Cloud Run job can run ``run_blog.py`` as its
command. On a Cloud Run job the runtime is ephemeral: the job pulls the fresh DB
from GCS, builds the mirror, grades, publishes, then exits (container tears down).

Usage (in Cloud Run job command):
    python run_blog.py
    python run_blog.py --dry
"""

from __future__ import annotations

import logging
import sys

from tools.blog_update import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Wire error -> Jira ticket creation (same as runner.py). Any logger.error()
    # / logger.critical() in the blog pipeline files a Jira bug ticket.
    try:
        from core import logger_setup
        logger_setup.setup_logging(app_name="agent-trade-blog", env="production")
    except Exception as e:
        print(f"[run_blog] Jira logging setup failed: {e}", file=sys.stderr)
    raise SystemExit(main())