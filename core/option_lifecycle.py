"""Option lifecycle management for agent-trade.

Provides the pre-expiry auto-close sweep that closes option positions before
expiry to avoid exercise/assignment. This is a deterministic safety net that
runs independently of the LLM brain.
"""

import logging
from datetime import datetime

from core import config
from core.option_picker import parse_option_symbol

logger = logging.getLogger("OptionLifecycle")


class OptionLifecycle:
    """Manages the lifecycle of open option positions."""

    def __init__(self, alpaca_client):
        self.client = alpaca_client

    def sweep(self) -> list[dict]:
        """Runs the auto-close sweep over all open option positions.

        Closes any option position whose DTE (days-to-expiry) is at or below
        ``config.OPTIONS_AUTO_CLOSE_DTE`` (default 3). Returns a list of
        close results.
        """
        if not getattr(config, "OPTIONS_ENABLED", False):
            logger.info("Options trading disabled; skipping auto-close sweep.")
            return []

        try:
            positions = self.client.get_option_positions()
        except Exception as e:
            logger.error(f"Failed to fetch option positions for auto-close: {e}")
            return []

        today = datetime.now().date()
        threshold = int(getattr(config, "OPTIONS_AUTO_CLOSE_DTE", 3))
        results = []
        for occ_symbol, details in positions.items():
            qty = float(details.get("qty", 0))
            if qty <= 0:
                continue
            parsed = parse_option_symbol(occ_symbol)
            if not parsed:
                logger.warning(f"Could not parse OCC symbol for auto-close check: {occ_symbol}")
                continue
            expiry = parsed["expiration_date"]
            dte = (expiry - today).days
            if dte <= threshold:
                logger.warning(f"Auto-closing option {occ_symbol}: DTE {dte} <= {threshold} to avoid exercise/assignment.")
                try:
                    result = self.client.close_option_position(occ_symbol)
                    results.append({
                        "symbol": occ_symbol,
                        "dte": dte,
                        "expiry": str(expiry),
                        "status": "closed",
                        "result": result,
                        "summary": f"Auto-closed {occ_symbol} (DTE {dte})",
                    })
                    from core import database
                    try:
                        database.log_execution(
                            decision_id=None, attempt=1, symbol=occ_symbol,
                            side="sell", qty=int(qty), order_type="option_autoclose",
                            status="closed", error=None,
                            alpaca_order_id=str(result.get("id", "")),
                        )
                    except Exception as log_err:
                        logger.error(f"Failed to log auto-close for {occ_symbol}: {log_err}")
                except Exception as e:
                    logger.error(f"Auto-close failed for {occ_symbol}: {e}")
                    results.append({"symbol": occ_symbol, "dte": dte, "status": "failed", "summary": str(e)})
        return results