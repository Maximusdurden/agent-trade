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
    raise SystemExit(main())