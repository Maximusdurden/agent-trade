import logging
from core import config
from datetime import datetime

logger = logging.getLogger("Guardrails")

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

    def validate_and_adjust_decision(self, decision: dict, account_state: dict, current_positions: dict) -> tuple[bool, str, dict]:
        """
        Validates the proposed decision from the LLM trading brain against risk limits.
        If necessary, it safely scales down trade size (shares) to comply with guidelines.
        
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

        # 1. HOLD/NO_ACTION requires no guardrail checks
        if action in ("HOLD", "NO_ACTION"):
            adjusted_decision["quantity"] = 0.0
            return True, "Approved: No action taken.", adjusted_decision

        # 2. Check Action validity
        if action not in ("BUY", "SELL"):
            return False, f"Rejected: Unknown action '{action}'. Only BUY, SELL, or HOLD are permitted.", adjusted_decision

        # 3. Check Universe Restrictions
        # Allow trading if symbol is in config.TRADING_UNIVERSE or in the latest screened watchlist
        from core import database
        allowed_symbols = set(s.upper() for s in config.TRADING_UNIVERSE)
        
        latest_watchlist = database.get_latest_watchlist_raw()
        for ws_symbol in latest_watchlist:
            # Normalize watchlist symbols (e.g. SOL/USD -> SOLUSD) and add both forms
            allowed_symbols.add(ws_symbol.upper())
            allowed_symbols.add(ws_symbol.upper().replace('/', ''))
            
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
                
            return True, f"Approved: Sell order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        # 6. Buy Rules Guardrail
        if action == "BUY":
            # Max dollar allocation allowed for a single trade
            max_trade_value = equity * config.MAX_TRADE_ALLOCATION_PCT
            proposed_trade_value = proposed_qty * current_price
            
            # Check if transaction value exceeds max allocation limit
            if proposed_trade_value > max_trade_value:
                if is_crypto:
                    max_allowed_qty = round(max_trade_value / current_price, 4)
                else:
                    max_allowed_qty = int(max_trade_value // current_price)
                    
                logger.warning(f"Proposed buy value (${proposed_trade_value:,.2f}) exceeds max trade size limit (${max_trade_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price
                
            # Check per-ticker allocation limit
            existing_position_value = 0.0
            if symbol in current_positions:
                existing_position_value = current_positions[symbol]["qty"] * current_price
                
            max_ticker_value = equity * config.MAX_TICKER_ALLOCATION_PCT
            total_proposed_value = existing_position_value + proposed_trade_value
            
            if total_proposed_value > max_ticker_value:
                max_allowed_value = max(0, max_ticker_value - existing_position_value)
                if is_crypto:
                    max_allowed_qty = round(max_allowed_value / current_price, 4)
                else:
                    max_allowed_qty = int(max_allowed_value // current_price)
                    
                logger.warning(f"Proposed total position value (${total_proposed_value:,.2f}) exceeds per-ticker limit (${max_ticker_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price
                
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Buy quantity scaled down to 0 because total position allocation for {symbol} would exceed the per-ticker limit of {config.MAX_TICKER_ALLOCATION_PCT * 100}% of equity.", adjusted_decision

            # Ensure we maintain the cash buffer
            min_cash_required = equity * config.MIN_CASH_BUFFER_PCT
            available_spending_cash = cash - min_cash_required
            
            if available_spending_cash <= 0:
                return False, f"Rejected: Cash balance (${cash:,.2f}) is below or near the required cash buffer (${min_cash_required:,.2f}). Buying blocked.", adjusted_decision
                
            if proposed_trade_value > available_spending_cash:
                if is_crypto:
                    max_cash_qty = round(available_spending_cash / current_price, 4)
                else:
                    max_cash_qty = int(available_spending_cash // current_price)
                    
                logger.warning(f"Proposed buy cost (${proposed_trade_value:,.2f}) exceeds available spending cash (${available_spending_cash:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_cash_qty}.")
                proposed_qty = max_cash_qty
                proposed_trade_value = proposed_qty * current_price

            if proposed_qty <= 0:
                return False, "Rejected: Buy quantity scaled down to 0 due to cash limits.", adjusted_decision

            adjusted_decision["quantity"] = proposed_qty
            return True, f"Approved: Buy order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        return False, "Rejected: Fell through all guardrail options.", adjusted_decision

