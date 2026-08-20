import logging
import re
from datetime import datetime, timedelta
from core import config

logger = logging.getLogger("Guardrails")

# OCC option symbol detection: 6-char root (padded), 6-digit date, C/P, 8-digit strike
OCC_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", re.IGNORECASE)


def is_occ_symbol(symbol: str) -> bool:
    """Return True if a symbol is an OCC option contract."""
    clean = (symbol or "").upper().replace(" ", "")
    return bool(OCC_SYMBOL_RE.match(clean)) and not clean.endswith("USD")


class RiskGuardrails:
    """Deterministic security/risk layer between LLM decisions and execution."""
    
    def __init__(self):
        pass

    def is_market_open_check(self) -> tuple[bool, str]:
        """
        Checks if the US Equity market (NYSE/NASDAQ) is currently open.
        Regular trading hours are Monday through Friday, 9:30 AM to 4:00 PM Eastern Time.
        """
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/New_York")
            now = datetime.now(tz)
        except Exception:
            try:
                import pytz
                tz = pytz.timezone("America/New_York")
                now = datetime.now(tz)
            except Exception:
                # Fallback: Assume current local machine time is in Eastern Time
                logger.warning("Could not load zoneinfo or pytz. Falling back to local time zone for market hours check.")
                now = datetime.now()
                
        # Check day of week (0 = Monday, ..., 6 = Sunday)
        weekday = now.weekday()
        if weekday >= 5:
            return False, f"Market closed: Weekend ({now.strftime('%A')})."
            
        # Check hour and minute
        market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
        
        if now < market_start:
            return False, f"Market closed: Pre-market (Current Eastern Time: {now.strftime('%H:%M')})."
        elif now > market_end:
            return False, f"Market closed: Post-market (Current Eastern Time: {now.strftime('%H:%M')})."
            
        return True, "Market is open."

    def validate_and_adjust_decision(self, decision: dict, account_state: dict, current_positions: dict,
                                     cycle_context: dict | None = None) -> tuple[bool, str, dict]:
        """
        Validates the proposed decision from the LLM trading brain against risk limits.
        If necessary, it safely scales down trade size (shares) to comply with guidelines.
        
        Args:
            cycle_context (optional): mutable dict tracking cumulative spend / trade
                count across ALL decisions in the current cycle. Passed by the runner
                so multiple per-ticker BUYs share ONE budget. Expected keys:
                {"spent": float, "trades_executed": int, "equity": float}.
        
        Returns:
            - is_approved (bool)
            - status_message (str)
            - adjusted_decision (dict): Copy of decision with adjusted quantities if needed.
        """
        action = decision.get("action", "HOLD").upper()
        symbol = decision.get("symbol", "").upper()
        proposed_qty = float(decision.get("quantity", 0.0))
        
        adjusted_decision = decision.copy()
        adjusted_decision["action"] = action
        adjusted_decision["symbol"] = symbol
        adjusted_decision["quantity"] = proposed_qty

        # Cycle-level limits: enforce MAX_TRADES_PER_CYCLE + cumulative budget.
        # cycle_context is shared across all decisions in this cycle.
        cycle_context = cycle_context if isinstance(cycle_context, dict) else {}

        # 1. HOLD/NO_ACTION requires no guardrail checks
        if action in ("HOLD", "NO_ACTION"):
            adjusted_decision["quantity"] = 0.0
            return True, "Approved: No action taken.", adjusted_decision

        # 1b. Per-cycle trade cap
        max_trades = int(getattr(config, "MAX_TRADES_PER_CYCLE", 1))
        trades_so_far = int(cycle_context.get("trades", 0))
        if action in ("BUY", "SELL") and proposed_qty > 0 and trades_so_far >= max_trades:
            adjusted_decision["quantity"] = 0.0
            return False, (f"Rejected: Per-cycle trade cap reached ({trades_so_far} >= "
                           f"MAX_TRADES_PER_CYCLE {max_trades}). No more executable trades this cycle."), adjusted_decision

        # 2. Check Action validity
        if action not in ("BUY", "SELL"):
            return False, f"Rejected: Unknown action '{action}'. Only BUY, SELL, or HOLD are permitted.", adjusted_decision

        # 2b. Resolve instrument (stock vs option) via the conviction-threshold rule.
        # The agent expresses a directional view + conviction. A deterministic rule
        # decides whether this decision is expressed via options (leverage) or shares.
        # This prevents stock<->option whipsaw on the same underlying.
        # NOTE: This guardrail only routes NEW option BUYs. Existing option positions
        # (SELL) are detected separately below by their OCC symbol.
        instrument = None
        if is_occ_symbol(symbol):
            instrument = "option"  # closing an existing option position
        elif action == "BUY":
            conviction = float(decision.get("conviction", 0.0) or 0.0)
            options_on = getattr(config, "OPTIONS_ENABLED", False)
            in_universe = symbol.upper() in set(getattr(config, "OPTIONS_UNIVERSE", []))
            threshold = float(getattr(config, "OPTIONS_CONVICTION_THRESHOLD", 0.7))
            # Direction must support a leveraged long (bullish -> call). For a buy,
            # options leverage only applies to bullish calls (we only do long calls/puts).
            direction = str(decision.get("direction", "neutral")).lower()
            if options_on and in_universe and conviction >= threshold and direction in ("bullish", "bearish"):
                instrument = "option"
                adjusted_decision["instrument"] = "option"
            else:
                adjusted_decision["instrument"] = "stock"
        else:
            adjusted_decision["instrument"] = "stock"

        # 2c. Options-specific validation (BUY-to-open of a new option position,
        # or SELL-to-close of an existing OCC option position).
        if instrument == "option":
            ok, msg, adjusted = self._validate_option_decision(
                decision, adjusted_decision, account_state, current_positions
            )
            if not ok:
                return False, msg, adjusted
            return True, msg, adjusted

        # 3. Check Universe Restrictions
        # Allow trading if symbol is in config.TRADING_UNIVERSE, in the latest
        # screened watchlist, OR is a currently-held position. Held positions must
        # always be tradable so the agent can manage (SELL) or add to (BUY) them,
        # even if the symbol is not in the static universe or the latest top-N
        # watchlist (e.g. a position opened earlier that later fell out of the
        # top-N screener output).
        from core import database
        allowed_symbols = set(s.upper() for s in config.TRADING_UNIVERSE)
        
        latest_watchlist = database.get_latest_watchlist_raw()
        for ws_symbol in latest_watchlist:
            # Normalize watchlist symbols (e.g. SOL/USD -> SOLUSD) and add both forms
            allowed_symbols.add(ws_symbol.upper())
            allowed_symbols.add(ws_symbol.upper().replace('/', ''))
            
        # Add currently-held positions (both raw and normalized forms)
        for pos_symbol in current_positions:
            allowed_symbols.add(pos_symbol.upper())
            allowed_symbols.add(pos_symbol.upper().replace('/', ''))
            
        normalized_symbol = symbol.upper().replace('/', '')
        if symbol.upper() not in allowed_symbols and normalized_symbol not in allowed_symbols:
            return False, f"Rejected: Symbol '{symbol}' is not in the allowed trading universe ({config.TRADING_UNIVERSE}) nor in the latest watchlist ({latest_watchlist}).", adjusted_decision

        # 3b. Check Market Hours for Equities (Crypto is 24/7)
        is_crypto = "SOL" in symbol or "USD" in symbol or "/" in symbol
        if not is_crypto:
            is_open, msg = self.is_market_open_check()
            if not is_open:
                return False, f"Rejected: {msg} Equities trading restricted outside regular market hours.", adjusted_decision

        # 3c. Check Anti-Whipsaw Holding Period (4 hours)
        try:
            from core import database
            recent_trades = database.get_recent_trades(limit=10)
            last_trade = None
            for t in recent_trades:
                if t["symbol"] == symbol and t["status"] == "filled":
                    last_trade = t
                    break
                    
            if last_trade:
                trade_time_str = last_trade["timestamp"]
                if trade_time_str.endswith("Z"):
                    trade_time_str = trade_time_str[:-1]
                trade_time = datetime.fromisoformat(trade_time_str)
                time_diff = datetime.utcnow() - trade_time
                hours_since_last_trade = time_diff.total_seconds() / 3600.0
                
                MIN_HOLDING_HOURS = 4.0
                if hours_since_last_trade < MIN_HOLDING_HOURS:
                    last_side = last_trade["side"].upper()
                    if last_side == "BUY" and action == "SELL":
                        return False, f"Rejected: Anti-whipsaw guardrail. Asset bought too recently ({hours_since_last_trade:.2f} hours ago < {MIN_HOLDING_HOURS} hours limit). Selling blocked.", adjusted_decision
                    elif last_side == "SELL" and action == "BUY":
                        return False, f"Rejected: Anti-whipsaw guardrail. Asset sold too recently ({hours_since_last_trade:.2f} hours ago < {MIN_HOLDING_HOURS} hours limit). Re-buying blocked.", adjusted_decision
        except Exception as err:
            logger.error(f"Error checking anti-whipsaw guardrail: {err}")

        # 4. Check Daily Loss Limit (Equity Drawdown Guardrail)
        equity = account_state.get("equity", 0.0)
        cash = account_state.get("cash", 0.0)
        unrealized_pnl = account_state.get("unrealized_pnl", 0.0)
        
        # Alpaca provides 'last_equity' which is equity at previous market close
        # If unavailable (e.g. mock), we assume starting equity was close to current
        last_equity = account_state.get("last_equity", equity)
        if last_equity > 0:
            daily_drawdown_pct = (last_equity - equity) / last_equity
            if daily_drawdown_pct >= config.DAILY_LOSS_LIMIT_PCT:
                # If we've hit the loss limit, only allow selling (to liquidate/de-risk)
                if action == "BUY":
                    return False, f"Rejected: Daily loss limit exceeded ({daily_drawdown_pct:.2%} >= {config.DAILY_LOSS_LIMIT_PCT:.2%}). All BUY orders blocked.", adjusted_decision

        # Fetch current price
        current_price = float(decision.get("current_price", 0.0))
        if current_price <= 0:
            return False, "Rejected: Missing or invalid current price for execution.", adjusted_decision

        # 5. Sell Rules Guardrail
        if action == "SELL":
            # Check if we own this asset
            if symbol not in current_positions:
                return False, f"Rejected: Attempted to sell {symbol} but do not own any shares.", adjusted_decision
            
            owned_qty = current_positions[symbol]["qty"]
            qty_available = current_positions[symbol].get("qty_available", owned_qty)
            
            # If the proposed quantity exceeds qty_available, log a warning and scale it down to qty_available.
            if proposed_qty > qty_available:
                logger.warning(f"Adjusting SELL quantity for {symbol} from {proposed_qty} to available {qty_available} (owned: {owned_qty}, locked in other orders: {owned_qty - qty_available}).")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available
                
            if qty_available == 0 or proposed_qty <= 0:
                adjusted_decision["quantity"] = 0.0
                return False, f"Rejected: Sell quantity scaled down to 0 because all owned shares ({owned_qty}) are currently locked/held in other open or pending-cancel orders.", adjusted_decision

            # Dust-liquidation guardrail: if the whole position (or the proposed
            # sell) is worth less than MIN_SELL_VALUE, escalate to a full exit so
            # the agent never bleeds out a tiny fraction (e.g. 25% of a small
            # position) every cycle. This is deterministic and protects against
            # any LLM-generated rule emitting fractional sells.
            position_value = owned_qty * current_price
            escalated = False
            if position_value <= config.MIN_SELL_VALUE:
                logger.warning(f"Dust position for {symbol}: value ${position_value:.2f} <= MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}. Escalating SELL to full liquidation of {qty_available} shares.")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available
                escalated = True
            elif proposed_qty * current_price < config.MIN_SELL_VALUE:
                logger.warning(f"Proposed SELL value for {symbol} (${proposed_qty * current_price:.2f}) is below MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}. Escalating to full liquidation of {qty_available} shares.")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available
                escalated = True

            if escalated:
                return True, f"Approved: Dust-liquidation full exit of {proposed_qty} shares of {symbol} (position value ${position_value:.2f} <= MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}).", adjusted_decision
                
            return True, f"Approved: Sell order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        # 6. Buy Rules Guardrail
        if action == "BUY":
            # Max dollar allocation allowed for a single trade
            max_trade_value = equity * config.MAX_TRADE_ALLOCATION_PCT
            proposed_trade_value = proposed_qty * current_price
            
            # Check if transaction value exceeds max allocation limit
            if proposed_trade_value > max_trade_value:
                # Fractional quantities are preserved for both crypto and equities.
                # The broker layer (alpaca_client) handles eligibility: bracket (OCO)
                # orders round to whole shares, and plain market orders fall back to
                # whole shares if Alpaca rejects a fractional qty for a given equity.
                max_allowed_qty = round(max_trade_value / current_price, 4)
                    
                logger.warning(f"Proposed buy value (${proposed_trade_value:,.2f}) exceeds max trade size limit (${max_trade_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price

            # Cumulative cycle budget: cap total spend across ALL buys this cycle so
            # N per-ticker BUYs can't each pass the per-trade check while summing far
            # beyond acceptable exposure.
            spent_so_far = float(cycle_context.get("spent", 0.0))
            # Total allowed cycle spend = per-trade cap * max trades (bounded by none).
            max_cycle_budget = max_trade_value * int(getattr(config, "MAX_TRADES_PER_CYCLE", 1))
            if spent_so_far + proposed_trade_value > max_cycle_budget:
                allowed_for_cycle = max(0.0, max_cycle_budget - spent_so_far)
                if allowed_for_cycle <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, (f"Rejected: Cumulative cycle spend budget exhausted "
                                   f"(${spent_so_far:,.2f} spent / ${max_cycle_budget:,.2f})."), adjusted_decision
                max_allowed_qty = round(allowed_for_cycle / current_price, 4)
                logger.warning(f"Cycle spend budget would be exceeded (${spent_so_far+proposed_trade_value:,.2f} > ${max_cycle_budget:,.2f}). "
                               f"Scaling buy for {symbol} to ${allowed_for_cycle:,.2f}.")
                proposed_qty = max(0.0, max_allowed_qty)
                proposed_trade_value = proposed_qty * current_price
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Cumulative cycle budget exhausted for {symbol}.", adjusted_decision
                
                
            # Check per-ticker allocation limit
            existing_position_value = 0.0
            if symbol in current_positions:
                existing_position_value = current_positions[symbol]["qty"] * current_price
                
            max_ticker_value = equity * config.MAX_TICKER_ALLOCATION_PCT
            total_proposed_value = existing_position_value + proposed_trade_value
            
            if total_proposed_value > max_ticker_value:
                max_allowed_value = max(0, max_ticker_value - existing_position_value)
                # Fractional quantities preserved for both crypto and equities (see note above).
                max_allowed_qty = round(max_allowed_value / current_price, 4)
                    
                logger.warning(f"Proposed total position value (${total_proposed_value:,.2f}) exceeds per-ticker limit (${max_ticker_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price
                
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Buy quantity scaled down to 0 because total position allocation for {symbol} would exceed the per-ticker limit of {config.MAX_TICKER_ALLOCATION_PCT * 100}% of equity.", adjusted_decision

            # Ensure we maintain the cash buffer
            # Calculate dynamic cash buffer based on number of open positions
            num_open_positions = len(current_positions)
            
            # Base buffer percentage (e.g., 5%)
            base_buffer_pct = 0.05
            
            # Additional buffer per open position (e.g., 1%)
            per_position_buffer_pct = 0.01
            
            # Maximum buffer percentage (e.g., 25%)
            max_buffer_pct = 0.25
            
            # Calculate dynamic buffer percentage
            dynamic_buffer_pct = min(max_buffer_pct, base_buffer_pct + (num_open_positions * per_position_buffer_pct))
            
            min_cash_required = equity * dynamic_buffer_pct
            
            available_spending_cash = cash - min_cash_required
            
            if available_spending_cash <= 0:
                return False, f"Rejected: Cash balance (${cash:,.2f}) is below or near the required cash buffer (${min_cash_required:,.2f}). Buying blocked.", adjusted_decision
                
            if proposed_trade_value > available_spending_cash:
                # Fractional quantities preserved for both crypto and equities (see note above).
                max_cash_qty = round(available_spending_cash / current_price, 4)
                    
                logger.warning(f"Proposed buy cost (${proposed_trade_value:,.2f}) exceeds available spending cash (${available_spending_cash:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_cash_qty}.")
                proposed_qty = max_cash_qty
                proposed_trade_value = proposed_qty * current_price

            if proposed_qty <= 0:
                return False, "Rejected: Buy quantity scaled down to 0 due to cash limits.", adjusted_decision

            adjusted_decision["quantity"] = proposed_qty
            return True, f"Approved: Buy order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        return False, "Rejected: Fell through all guardrail options.", adjusted_decision

    # ------------------------------------------------------------------
    # OPTIONS VALIDATION
    # ------------------------------------------------------------------

    def _validate_option_decision(self, decision: dict, adjusted_decision: dict,
                                  account_state: dict, current_positions: dict) -> tuple[bool, str, dict]:
        """Validates an option decision (BUY-to-open or SELL-to-close).

        Enforces: kill-switch, market hours, options universe, options buying
        power, per-ticker contract limits, DTE hard bounds, and the
        earnings/IV filter (no earnings inside the DTE window).
        """
        from core import database
        action = adjusted_decision.get("action", "HOLD").upper()
        symbol = adjusted_decision.get("symbol", "").upper()
        proposed_qty = float(adjusted_decision.get("quantity", 0.0))

        # Kill-switch
        if not getattr(config, "OPTIONS_ENABLED", False):
            return False, "Rejected: Options trading is disabled (OPTIONS_ENABLED=false).", adjusted_decision

        cleared_o = symbol.replace(" ", "")
        # Determine underlying root + eligibility
        is_occ = is_occ_symbol(symbol)
        if is_occ:
            # SELL-to-close of an existing position: allow the underlying regardless
            underlying = re.match(r"^[A-Z]+", cleared).group(0).strip() or symbol
            # Verify we actually hold this contract
            held = current_positions.get(symbol) or current_positions.get(cleared)
            if not held or float(held.get("qty", 0)) <= 0:
                return False, f"Rejected: Attempted to SELL-to-close {symbol} but no position is held.", adjusted_decision
            # Cap sell qty at held qty (never go short)
            held_qty = float(held.get("qty", 0))
            if proposed_qty > held_qty:
                logger.warning(f"Option SELL qty {proposed_qty} capped to held {held_qty} for {symbol}.")
                adjusted_decision["quantity"] = held_qty
                proposed_qty = held_qty
        else:
            underlying = symbol.upper()
            # Market hours (options are equity-hours only)
            is_open, msg = self.is_market_open_check()
            if not is_open:
                return False, f"Rejected: {msg} Options trading restricted outside regular market hours.", adjusted_decision
            # Options universe membership
            if underlying not in set(getattr(config, "OPTIONS_UNIVERSE", [])):
                return False, f"Rejected: Symbol '{underlying}' is not in the options universe.", adjusted_decision
            # DTE hard bounds (agent override must stay within config bounds)
            dte_min = int(adjusted_decision.get("option_dte_min") or getattr(config, "OPTIONS_DTE_MIN", 30))
            dte_max = int(adjusted_decision.get("option_dte_max") or getattr(config, "OPTIONS_DTE_MAX", 45))
            hard_min = getattr(config, "OPTIONS_DTE_HARD_MIN", 14)
            hard_max = getattr(config, "OPTIONS_DTE_HARD_MAX", 90)
            if dte_min < hard_min:
                dte_min = hard_min
            if dte_max > hard_max:
                dte_max = hard_max
            if dte_min > dte_max:
                dte_min, dte_max = dte_max, dte_min
            adjusted_decision["option_dte_min"] = dte_min
            adjusted_decision["option_dte_max"] = dte_max
            # OTM% bounds
            otm = adjusted_decision.get("option_strike_otm_pct")
            if otm is not None:
                otm = max(0.0, min(0.30, float(otm)))
                adjusted_decision["option_strike_otm_pct"] = otm
            # Earnings/IV filter: reject if underlying reports earnings inside DTE window
            cleared_underlying = underlying
            earnings_msg = self._has_earnings_before_expiry(cleared_underlying, dte_max)
            if earnings_msg:
                return False, f"Rejected: {earnings_msg}", adjusted_decision

        # Buying power sizing (only for BUY-to-open; SELL just closes, no new capital)
        if action == "BUY":
            equity = float(account_state.get("equity", 0.0))
            max_cost = equity * float(getattr(config, "OPTIONS_MAX_ALLOCATION_PCT", 0.05))
            # Estimate premium per contract ~ 1-2% OTM; we use a placeholder until the
            # executor resolves the actual contract. The executor will enforce the real
            # cost. Here we cap proposed contracts by a config max and rough budget.
            max_contracts = getattr(config, "OPTIONS_MAX_CONTRACTS_PER_TICKER", 5)
            if proposed_qty > max_contracts:
                logger.warning(f"Option buy qty {proposed_qty} capped to max contracts {max_contracts} per ticker.")
                adjusted_decision["quantity"] = max_contracts
                proposed_qty = max_contracts
            # Options buying power check (upper bound)
            obp = float(self._get_options_buying_power())
            # Reserve at least ~equity*2% per contract as a sanity bound; the executor
            # does the exact ask*100*contracts check. Skip hard rejection here.
            if obp <= 0:
                return False, "Rejected: No options buying power available.", adjusted_decision

        adjusted_decision["instrument"] = "option"
        return True, f"Approved: Option {'BUY-to-open' if action == 'BUY' else 'SELL-to-close'} via instrument rule.", adjusted_decision

    def get_options_buying_power(self) -> float:
        """Returns account options buying power (separate from equity buying power)."""
        return self._get_options_buying_power()

    def _get_options_buying_power(self) -> float:
        """Reads options buying power from the active Alpaca client (via a small cache)."""
        try:
            from core import alpaca_client as ac
            # get singleton client if it exists
            inst = getattr(ac, "_client_instance", None)
            if inst is None:
                return 0.0
            return inst.get_options_buying_power()
        except Exception as e:
            logger.error(f"Error fetching options buying power in guardrails: {e}")
            return 0.0

    def _has_earnings_before_expiry(self, underlying: str, dte_max: int) -> str:
        """Returns an error string if the underlying reports earnings within dte_max days; '' if clear."""
        try:
            from core import data_provider as dp
            earn = getattr(dp, "get_earnings_dates", None)
            if earn is None:
                return ""
            df = earn([underlying.upper()], days_ahead=dte_max)
            if df is not None and not df.empty:
                return (f"{underlying} has an earnings date within the {dte_max}-day "
                        f"option window; options rejected to avoid IV crush.")
            return ""
        except Exception as e:
            logger.warning(f"Earnings check failed for {underlying}: {e}. Allowing (fail-open).")
            return ""

