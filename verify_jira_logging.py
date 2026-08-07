"""End-to-end verification of the agent-trade -> Jira error logging path.

Exercises the real integration used in production:
  runner.py -> core.logger_setup.setup_logging() -> JiraLoggingHandler -> agent_jira.jira_logger

Logs a test ERROR and confirms a Jira ticket is created (or a comment appended to
an existing deduplicated ticket) in project TMCL.
"""
import sys
import os
import logging

# Mirror the sys.path setup used by core/logger_setup.py
sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, r"Z:\python\projects\agent-jira-client")

# Load agent-trade .env so config.JIRA_* are populated
def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

_load_env(r"Z:\python\projects\agent-trade\.env")

from core import logger_setup

# Configure logging exactly as runner.py does
logger_setup.setup_logging(app_name="agent-trade", env="production")

logger = logging.getLogger("VerifyJiraLogging")
logger.error("VERIFICATION TEST: Jira logging path is working. This is a test error from the fractional-shares/Jira-logging fix verification.")

print("\n[Verify] ERROR logged. Check Jira project TMCL for a ticket containing 'VERIFICATION TEST'.")
print("[Verify] If a ticket was created, the fix is confirmed working end-to-end.")
