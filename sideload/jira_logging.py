"""Shared Jira error-logging helper for the sideload lane.

Wires agent-trade's Jira logging into every sideload script so that ERROR /
CRITICAL logs and uncaught exceptions automatically file Jira tickets (same
pattern as the normal lane via ``core.logger_setup.setup_logging``).

Two mechanisms:
  1. ``setup_jira_logging()`` — installs the global Jira handler + exception
     hook (call once at the top of each script's ``main()``).
  2. ``log_exception_to_jira()`` — explicit Jira ticket for a caught exception
     (call inside ``except`` blocks with context metadata).

Both are no-ops when the Jira library is unavailable or in a test/CI env.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("SideloadJira")


def setup_jira_logging(app_name: str = "agent-trade-sideload", env: str = "production") -> None:
    """Install the Jira logging handler + global exception hook.

    Safe to call multiple times (the handler is deduped). No-op if the Jira
    library is unavailable or in a test/CI environment.
    """
    try:
        from core import logger_setup
        logger_setup.setup_logging(app_name=app_name, env=env)
    except Exception as e:  # never let Jira wiring break the lane
        logger.warning(f"Jira logging setup failed (non-fatal): {e}")


def log_exception_to_jira(exc: BaseException, context: str, metadata: dict | None = None) -> None:
    """File an explicit Jira ticket for a caught exception.

    Args:
        exc: The caught exception.
        context: Short human label for the failure (e.g. "AMD Backtest Failure").
        metadata: Extra key/value context to attach to the ticket.
    """
    try:
        from agent_jira.jira_logger import log_exception
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_value is None:
            exc_type = type(exc)
            exc_value = exc
            exc_tb = exc.__traceback__
        log_exception(
            exc_type, exc_value, exc_tb,
            app_name="agent-trade-sideload",
            env="production",
            metadata={"Context": context, **(metadata or {})},
        )
    except Exception as e:  # never let Jira logging break the lane
        logger.error(f"Failed to log exception to JIRA: {e}")