"""Option lifecycle management for agent-trade.

Provides deterministic safety sweeps that run independently of the LLM brain and
ONLY ever close (SELL-to-close) option positions:

  1. Pre-expiry auto-close sweep — closes positions whose DTE <=
     ``OPTIONS_AUTO_CLOSE_DTE`` to avoid exercise/assignment.
  2. Risk sweep — the event gate + vega/delta exposure caps + optional EOD-flat
     from ``core.option_risk`` (see that module for the reasoning). This is where
     the "flatten before earnings/FOMC" and "cap overnight greeks exposure" rules
     live.
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

    # ------------------------------------------------------------------
    def _close_one(self, occ_symbol: str, qty: float, order_type: str, summary: str) -> dict:
        """Close a single option position and log the execution. Returns a result dict."""
        try:
            result = self.client.close_option_position(occ_symbol)
            from core import database
            try:
                database.log_execution(
                    decision_id=None, attempt=1, symbol=occ_symbol,
                    side="sell", qty=int(qty), order_type=order_type,
                    status="closed", error=None,
                    alpaca_order_id=str(result.get("id", "")),
                )
            except Exception as log_err:
                logger.error(f"Failed to log {order_type} for {occ_symbol}: {log_err}")
            return {"symbol": occ_symbol, "status": "closed", "result": result, "summary": summary}
        except Exception as e:
            logger.error(f"{order_type} failed for {occ_symbol}: {e}")
            return {"symbol": occ_symbol, "status": "failed", "summary": str(e)}

    def sweep(self) -> list[dict]:
        """Runs the pre-expiry auto-close sweep over all open option positions.

        Closes any option position whose DTE (days-to-expiry) is at or below
        ``config.OPTIONS_AUTO_CLOSE_DTE`` (default 3). Returns a list of
        close results.
        """
        if not getattr(config, "OPTIONS_ENABLED", False):
            logger.info("Options trading disabled; skipping auto-close sweep.")
            return []

        today = datetime.now().date()
        threshold = int(getattr(config, "OPTIONS_AUTO_CLOSE_DTE", 3))
        results = []
        for occ_symbol, details in self._positions().items():
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
                res = self._close_one(occ_symbol, qty, "option_autoclose",
                                      f"Auto-closed {occ_symbol} (DTE {dte})")
                res["dte"] = dte
                res["expiry"] = str(expiry)
                results.append(res)
        return results

    def risk_sweep(self, account_state=None) -> list[dict]:
        """Runs the event-gate + vega/delta exposure caps + optional EOD-flat.

        Only fires when options trading is enabled and ONLY closes positions
        (never opens risk). Extra safety on top of the pre-expiry sweep: this is
        what flattens a held option before earnings/FOMC or when the book's
        overnight greeks exceed the configured caps.

        ``account_state`` optionally provides equity; if omitted the module falls
        back to the live account.
        """
        from core import option_risk
        if not getattr(config, "OPTIONS_ENABLED", False):
            return []
        if not getattr(config, "OPTIONS_EVENT_GATE_ENABLED", True) \
                and not getattr(config, "OPTIONS_DELTA_CAP_PCT", 0.0) \
                and not getattr(config, "OPTIONS_VEGA_CAP_MV_PCT", 0.0) \
                and not getattr(config, "OPTIONS_EOD_FLAT", False):
            return []

        positions = self._positions()
        held = [s for s, d in positions.items() if float(d.get("qty", 0) or 0) > 0]
        if not held:
            return []

        equity = option_risk._equity_from_account(account_state)
        decisions = option_risk.rebalance_book(self.client, held, equity)
        decisions += option_risk.eod_flat_decisions(held)
        if not decisions:
            return []

        results = []
        closed_syms = set()
        for dec in decisions:
            occ = dec.get("symbol")
            if occ in closed_syms:
                continue
            closed_syms.add(occ)
            qty = float(positions.get(occ, {}).get("qty", 0) or 0)
            reason = dec.get("summary") or dec.get("reason") or "option risk control"
            logger.warning(f"Risk closing option {occ}: {reason}")
            res = self._close_one(occ, qty, "option_risk", f"Risk-close {occ}: {reason}")
            results.append(res)
        return results

    def _positions(self) -> dict:
        """Fetch open option positions keyed by OCC symbol (fail-safe empty)."""
        try:
            return self.client.get_option_positions()
        except Exception as e:
            logger.error(f"Failed to fetch option positions: {e}")
            return {}