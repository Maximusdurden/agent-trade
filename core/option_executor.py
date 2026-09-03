"""Option execution for agent-trade.

Takes an LLM decision (directional view + conviction) that the guardrails have
routed to the option path, selects the best contract via ``option_picker``,
risk-checks it, and places the option order. Also handles SELL-to-close of
existing option positions.

Long options only (Level 2). Never opens a short position.
"""

import logging

from core import config

logger = logging.getLogger("OptionExecutor")


class OptionExecutor:
    """Executes option decisions for the agent."""

    def __init__(self, alpaca_client):
        self.client = alpaca_client

    def execute(self, decision: dict, account_state: dict) -> dict:
        """Executes a single option decision.

        Args:
            decision: The (guardrail-adjusted) decision dict. Must contain
                action, symbol, quantity, direction, conviction, and optional
                option_dte_min/max/option_strike_otm_pct.
            account_state: Account state dict (equity, options buying power).

        Returns:
            dict describing the result (order_info, symbol, action, status...).
        """
        action = decision.get("action", "HOLD").upper()
        symbol = decision.get("symbol", "").upper()
        quantity = int(float(decision.get("quantity", 0) or 0))
        direction = str(decision.get("direction", "neutral")).lower()

        if action == "SELL":
            return self._close(symbol, quantity)

        if action == "BUY":
            return self._open(symbol, quantity, direction, decision, account_state)

        return {"status": "noop", "symbol": symbol, "summary": "No option action."}

    # ------------------------------------------------------------------
    def _resolve_contract(self, symbol: str, direction: str, decision: dict):
        """Picks the best call/put contract for a BUY-to-open directional view."""
        from core.option_picker import find_best_option, parse_option_symbol

        parsed = parse_option_symbol(symbol)
        if parsed:
            # Symbol is already an OCC contract (agent referenced a specific one)
            quotes = self.client.get_latest_option_data([parsed["symbol"]])
            quote = quotes.get(parsed["symbol"]) if quotes else None
            return _ExplicitContract(parsed, quote)

        # Direction -> option type. Long bullish = CALL, long bearish = PUT.
        option_type = "CALL" if direction == "bullish" else ("PUT" if direction == "bearish" else None)
        if option_type is None:
            raise ValueError(f"BUY option requires direction 'bullish'/'bearish', got '{direction}'.")

        dte_min = int(decision.get("option_dte_min") or config.OPTIONS_DTE_MIN)
        dte_max = int(decision.get("option_dte_max") or config.OPTIONS_DTE_MAX)
        otm = decision.get("option_strike_otm_pct")
        if otm is None:
            otm_min = config.OPTIONS_OTM_PERCENT_MIN
            otm_max = config.OPTIONS_OTM_PERCENT_MAX
        else:
            otm = float(otm)
            otm_min = otm
            otm_max = otm

        contract = find_best_option(
            underlying_symbol=symbol,
            option_type=option_type,
            days_out_min=dte_min,
            days_out_max=dte_max,
            otm_percent_min=otm_min,
            otm_percent_max=otm_max,
            alpaca_client=self.client,
        )
        # WIDER-WINDOW FALLBACK: if no contract matched the primary DTE window
        # (e.g. weeklies cluster just outside the window on a rollover day), retry
        # with a wider range up to the hard bound. This prevents a transient option
        # chain gap from cancelling a valid, high-conviction option BUY, and mirrors
        # dexter-trader's more robust DTE window.
        if contract is None and dte_max < getattr(config, "OPTIONS_DTE_FALLBACK_MAX", 90):
            fallback_max = int(getattr(config, "OPTIONS_DTE_FALLBACK_MAX", 90))
            logger.warning(
                f"No {option_type} for {symbol} within DTE {dte_min}-{dte_max}; "
                f"retrying with widened window up to DTE {fallback_max}."
            )
            contract = find_best_option(
                underlying_symbol=symbol,
                option_type=option_type,
                days_out_min=dte_min,
                days_out_max=fallback_max,
                otm_percent_min=otm_min,
                otm_percent_max=otm_max,
                alpaca_client=self.client,
            )
        if contract is None:
            raise ValueError(f"No suitable {option_type} option found for {symbol} within DTE {dte_min}-{dte_max}.")
        return contract

    def _open(self, symbol: str, quantity: int, direction: str, decision: dict,
              account_state: dict) -> dict:
        """Opens a long option position (BUY-to-open)."""
        try:
            contract = self._resolve_contract(symbol, direction, decision)
        except ValueError as e:
            logger.error(f"Option open failed for {symbol}: {e}")
            return {"status": "failed", "symbol": symbol, "summary": str(e), "action": "BUY"}

        occ_symbol = getattr(contract, "symbol", symbol)
        ask = float(getattr(contract, "ask_price", 0) or 0)
        premium = getattr(contract, "premium_cost", 0.0) or (ask * 100.0)

        equity = float(account_state.get("equity", 0.0))
        contracts = self._size_contracts(premium, equity)
        if contracts <= 0:
            return {"status": "failed", "symbol": occ_symbol,
                    "summary": "No affordable contracts within allocation.", "action": "BUY"}

        # Hard risk check: premium * contracts <= options allocation * equity
        max_cost = equity * float(config.OPTIONS_MAX_ALLOCATION_PCT)
        if premium * contracts > max_cost:
            contracts = max(1, int(max_cost // premium))

        try:
            if ask > 0:
                order = self.client.place_option_order(symbol=occ_symbol, qty=contracts,
                                                       side="buy", limit_price=ask)
            else:
                order = self.client.place_option_order(symbol=occ_symbol, qty=contracts, side="buy")
        except Exception as e:
            logger.error(f"Option BUY failed for {occ_symbol}: {e}")
            return {"status": "failed", "symbol": occ_symbol, "summary": str(e), "action": "BUY"}

        return {
            "status": "filled" if str(order.get("status", "")).lower() == "filled" else order.get("status", "submitted"),
            "action": "BUY",
            "symbol": occ_symbol,
            "contracts": contracts,
            "contract_strike": getattr(contract, "strike_price", None),
            "contract_expiry": str(getattr(contract, "expiration_date", "")),
            "contract_dte": getattr(contract, "dte", None),
            "contract_ask": ask,
            "order_info": order,
            "summary": f"Opened {contracts}x {occ_symbol} (ask ${ask})",
        }

    def _size_contracts(self, premium: float, equity: float) -> int:
        """Computes a safe number of contracts from equity budget & config bounds."""
        if premium <= 0:
            return 0
        budget = equity * float(config.OPTIONS_MAX_ALLOCATION_PCT)
        by_budget = max(1, int(budget // premium))
        by_cap = config.OPTIONS_MAX_CONTRACTS_PER_TICKER
        return max(1, min(by_budget, by_cap))

    def _close(self, symbol: str, quantity: int) -> dict:
        """Closes a long option position (SELL-to-close)."""
        cleared = symbol.replace(" ", "")
        try:
            order = self.client.close_option_position(cleared)
        except Exception as e:
            logger.error(f"Option SELL-to-close failed for {cleared}: {e}")
            return {"status": "failed", "symbol": cleared, "summary": str(e), "action": "SELL"}
        return {
            "status": order.get("status", "closed"),
            "action": "SELL",
            "symbol": cleared,
            "contracts": int(order.get("qty", 0) or 0),
            "order_info": order,
            "summary": f"Closed option position {cleared}",
        }


class _ExplicitContract:
    """Minimal contract wrapper for when the agent specified an OCC symbol."""
    def __init__(self, parsed, quote):
        import datetime as _dt
        self.symbol = parsed["symbol"]
        self.strike_price = parsed["strike_price"]
        self.expiration_date = parsed["expiration_date"]
        self.dte = (parsed["expiration_date"] - _dt.datetime.now().date()).days
        self.ask_price = float(getattr(quote, "ask_price", 0) or 0)
        self.bid_price = float(getattr(quote, "bid_price", 0) or 0)
        self.premium_cost = self.ask_price * 100.0 if self.ask_price > 0 else 0.0