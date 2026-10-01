#!/usr/bin/env python3
"""Query Cloud Logging for agent-trade job logs in a time window.

Usage: python cloud_logs.py <job_name> <start_iso> <end_iso> [--limit N] [--grep PATTERN]
"""
import sys
import re
from datetime import datetime, timezone

from google.cloud import logging as gcloud_logging

def main():
    job = sys.argv[1]
    start = sys.argv[2]
    end = sys.argv[3]
    limit = 200
    grep = None
    execution = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--grep" in sys.argv:
        grep = sys.argv[sys.argv.index("--grep") + 1]
    if "--execution" in sys.argv:
        execution = sys.argv[sys.argv.index("--execution") + 1]

    client = gcloud_logging.Client()
    if execution:
        filter_str = (
            f'resource.type="cloud_run_job" AND '
            f'labels."run.googleapis.com/execution_name"="{execution}"'
        )
    else:
        filter_str = (
            f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" '
            f'AND timestamp>="{start}" AND timestamp<="{end}"'
        )
    entries = list(client.list_entries(filter_=filter_str, order_by=gcloud_logging.DESCENDING, max_results=limit))
    entries.reverse()
    pat = re.compile(grep) if grep else None
    for e in entries:
        payload = e.payload
        if isinstance(payload, dict):
            payload = payload.get("textPayload", str(payload))
        line = str(payload)
        if pat and not pat.search(line):
            continue
        ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S") if e.timestamp else "?"
        print(f"{ts} | {line}")

if __name__ == "__main__":
    main()