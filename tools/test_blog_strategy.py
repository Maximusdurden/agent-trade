#!/usr/bin/env python3
"""Test the blog strategy status summary function."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.blog_update import _strategy_status_summary

# Use the downloaded cloud DB
db_path = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
summary = _strategy_status_summary(db_path, "2026-09-30")
print("Strategy status summary for 2026-09-30:")
print(summary or "(empty)")

print("\n---")
summary2 = _strategy_status_summary(db_path, "2026-09-21")
print("Strategy status summary for 2026-09-21:")
print(summary2 or "(empty)")