#!/usr/bin/env python3
"""Close the auto-generated Jira error tickets from the non-tech diversification
analysis (TMCL-1039..1042). These were created by the Jira error hook during
the first (buggy) run of sideload/nontech_diversification.py due to a
key-name mismatch — they are false-positive error artifacts, not real failures.

Usage:
    python close_nontech_error_tickets.py
"""
import sys

sys.path.insert(0, r"Z:\python\projects\agent-jira-client")

from agent_jira import IssueManager

TICKETS = ["TMCL-1039", "TMCL-1040", "TMCL-1041", "TMCL-1042"]
CLOSE_TRANSITION = "Done"  # or "Closed" depending on workflow


def run() -> None:
    manager = IssueManager()
    for key in TICKETS:
        try:
            issue = manager.get_issue(key)
            status = issue["fields"]["status"]["name"]
            summary = issue["fields"]["summary"]
            print(f"[{key}] status={status!r} summary={summary!r}")
        except Exception as e:
            print(f"[{key}] could not fetch: {e}")
            continue

        # Add a comment explaining this is a false-positive artifact.
        try:
            manager.add_comment(
                key,
                "Closing as a false-positive artifact: auto-created by the Jira "
                "error hook during the first run of sideload/nontech_diversification.py "
                "due to a key-name mismatch in the analysis script. The analysis "
                "completed successfully on re-run; no production failure occurred.",
            )
            print(f"[{key}] comment added")
        except Exception as e:
            print(f"[{key}] comment failed: {e}")

        # Transition to Done/Closed.
        try:
            manager.transition_issue(key, CLOSE_TRANSITION)
            print(f"[{key}] transitioned to {CLOSE_TRANSITION}")
        except Exception as e:
            print(f"[{key}] transition failed: {e}")


if __name__ == "__main__":
    run()