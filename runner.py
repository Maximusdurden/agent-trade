import time
import argparse
import logging
import sys
from datetime import datetime

from core import config
from core import database
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.guardrails import RiskGuardrails
from core.trading_brain import TradingBrain

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE)
    ]
)
from core import logger_setup
logger_setup.setup_logging(app_name="agent-trade", env="production")
logger = logging.getLogger("Runner")

def check_execution_window(alpaca_client: AlpacaClient) -> tuple[bool, str]:
    """
    Checks if the current time is between 09:00 and 16:30 Eastern Time on a weekday
    when the US equity market is open today.
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

    # 1. Weekday check
    weekday = now.weekday()
    if weekday >= 5:
        return False, f"Weekend ({now.strftime('%A')})."

    # 2. Time-of-day check (09:00 to 16:30 Eastern Time)
    current_minutes = now.hour * 60 + now.minute
    start_minutes = 9 * 60          # 09:00
    end_minutes = 16 * 60 + 30      # 16:30
    
    if current_minutes < start_minutes or current_minutes > end_minutes:
        return False, f"Outside allowed window of 09:00 - 16:30 ET (Current Eastern Time: {now.strftime('%H:%M')})."

    # 3. Market Open Today Check (Alpaca Calendar)
    if alpaca_client.is_mock:
        logger.info("Alpaca Client is in mock mode. Skipping API-based calendar check.")
        return True, "Within trading window (Mock fallback)."
        
    try:
        # Check if alpaca trading client is available to query calendar
        if hasattr(alpaca_client, "trading_client") and alpaca_client.trading_client:
            from alpaca.trading.requests import GetCalendarRequest
            # Query calendar for today's date
            today_date = now.date()
            request_params = GetCalendarRequest(start=today_date, end=today_date)
            calendar_entries = alpaca_client.trading_client.get_calendar(request_params)
            
            if not calendar_entries:
                return False, f"Market is closed today ({today_date}) according to Alpaca calendar (Holiday)."
            
            # Additional check: verify if the calendar entry is indeed today
            market_open_today = False
            for entry in calendar_entries:
                entry_date = entry.date
                if isinstance(entry_date, str):
                    try:
                        entry_date = datetime.strptime(entry_date, "%Y-%m-%d").date()
                    except ValueError:
                        pass
                
                if entry_date == today_date:
                    market_open_today = True
                    break
            
            if not market_open_today:
                return False, f"Market is closed today ({today_date}) according to Alpaca calendar."
    except Exception as e:
        logger.warning(f"Error checking Alpaca calendar: {e}. Falling back to weekday-only check.")

    return True, "Within trading window and market is open today."

def run_trading_cycle(alpaca_client: AlpacaClient, data_provider: DataProvider,
                      brain: TradingBrain, guardrails: RiskGuardrails, dry_run: bool = False):
    """Executes a single workflow cycle of the autonomous trading agent."""
    # Check execution window (09:00 - 16:30 ET, weekdays, market open)
    is_allowed, reason = check_execution_window(alpaca_client)
    import os
    if os.getenv("BYPASS_MARKET_WINDOW") == "True":
        is_allowed, reason = True, "Execution window check bypassed via BYPASS_MARKET_WINDOW environment variable."

    if not is_allowed:
        logger.info(f"Trading cycle skipped: {reason}")
        return

    logger.info("Starting autonomous trading cycle...")
    
    # 1. Fetch current account state
    try:
        account_state = alpaca_client.get_account_state()
        equity = account_state["equity"]
        cash = account_state["cash"]
        unrealized_pnl = account_state["unrealized_pnl"]
        
        logger.info(f"Account Balance: Equity: ${equity:,.2f} | Cash: ${cash:,.2f} | Open PnL: ${unrealized_pnl:,.2f}")
        
        # Log portfolio history to database only if running with a real live session (not simulated mock defaults)
        if not alpaca_client.is_mock:
            database.log_portfolio_history(equity, cash, unrealized_pnl)
    except Exception as e:
        logger.error(f"Critical error fetching account state: {e}. Aborting cycle.")
        return

    # 2. Fetch current active positions
    try:
        positions = alpaca_client.get_positions()
        logger.info(f"Active Positions: {list(positions.keys())}")
    except Exception as e:
        logger.error(f"Error fetching positions: {e}. Proceeding with assumptions.")
        positions = {}

    # 3. Fetch indicators and market state for the trading universe
    market_states = []
    for symbol in config.TRADING_UNIVERSE:
        logger.info(f"Fetching market data and indicators for {symbol}...")
        state = data_provider.get_market_state(symbol)
        if state:
            market_states.append(state)
            logger.info(f"{symbol} latest close: ${state['current_price']:.2f} | RSI: {state['indicators'].get('rsi_14')}")
            
            # Intraday Shock Check (Triggering dynamic emergency strategist updates)
            # Threshold: 3.0% for stock indices (SPY/QQQ), 8.0% for highly volatile crypto (SOL)
            is_crypto = "SOL" in symbol.upper() or "USD" in symbol.upper()
            shock_threshold = 8.0 if is_crypto else 3.0
            daily_change = state["daily_return_pct"]
            
            if abs(daily_change) >= shock_threshold:
                logger.warning(f"[SHOCK DETECTED] INTRADAY REGIME SHOCK: {symbol} daily move is {daily_change:.2f}% (Limit: {shock_threshold}%). Triggering emergency strategist run...")
                try:
                    from core.strategist import MetaStrategist
                    emergency_strategist = MetaStrategist()
                    # Trigger an immediate real-time strategy rules rewrite!
                    emergency_strategist.run_single_ticker_refinement(symbol, alpaca_client)
                except Exception as err:
                    logger.error(f"Could not execute emergency strategist update: {err}")
        else:
            logger.warning(f"Failed to fetch market data for {symbol}.")
            
    if not market_states:
        logger.error("No market data available for any ticker in universe. Aborting cycle.")
        return

    # 4. Fetch recent decisions for context memory
    try:
        recent_decisions = database.get_recent_decisions(limit=5)
    except Exception as e:
        logger.error(f"Error retrieving recent decisions from DB: {e}")
        recent_decisions = []

    # 5. Let the AI Brain reason and generate a decision
    logger.info("Querying AI strategy brain for proposed action...")
    decision = brain.make_decision(market_states, account_state, positions, recent_decisions)
    
    # Enrich decision with current pricing data for guardrail calculations
    target_symbol = decision.get("symbol", "").upper()
    current_price = 0.0
    for state in market_states:
        if state["symbol"] == target_symbol:
            current_price = state["current_price"]
            break
    decision["current_price"] = current_price

    # 6. Filter proposed decision through Risk Guardrails
    logger.info("Evaluating decision with deterministic Risk Guardrails...")
    is_approved, status_msg, adjusted_decision = guardrails.validate_and_adjust_decision(
        decision, account_state, positions
    )
    
    action = adjusted_decision.get("action", "HOLD")
    symbol = adjusted_decision.get("symbol", "")
    qty = adjusted_decision.get("quantity", 0.0)
    thought_process = adjusted_decision.get("thought_process", "")
    
    logger.info(f"Guardrails Result: {status_msg}")

    # 7. Log decision to SQLite Database
    try:
        decision_id = database.log_decision(
            ticker_indicators={s["symbol"]: s["indicators"] for s in market_states},
            portfolio_state={"cash": cash, "equity": equity, "positions": positions},
            thought_process=thought_process,
            proposed_action=action,
            proposed_symbol=symbol,
            proposed_qty=qty,
            is_approved=is_approved,
            rejection_reason=status_msg if not is_approved else None
        )
        logger.info(f"Decision logged successfully. DB ID: {decision_id}")
    except Exception as e:
        logger.error(f"Failed to log decision to DB: {e}")
        decision_id = None

    # 8. Execution Phase (only if approved and not a HOLD)
    if is_approved and action in ("BUY", "SELL") and qty > 0:
        take_profit_price = adjusted_decision.get("take_profit_price")
        stop_loss_price = adjusted_decision.get("stop_loss_price")
        
        is_crypto = "/" in symbol or "USD" in symbol or "SOL" in symbol
        
        if action == "BUY" and not is_crypto:
            # Fetch the real-time latest trade price as base_price to prevent validation failure due to price drift.
            try:
                base_price = alpaca_client.get_latest_price(symbol)
                logger.info(f"Fetched real-time latest price for {symbol} as base_price: ${base_price:.2f} (originally ${current_price:.2f})")
            except Exception as price_err:
                logger.warning(f"Could not fetch real-time price: {price_err}. Falling back to cached current_price: ${current_price:.2f}")
                base_price = float(current_price)
            
            # Since the entry base_price might have changed, we should recalculate TP/SL targets 
            # to preserve the relative distance (percentage offsets) intended by the Strategy Brain.
            cached_base = float(current_price) if current_price > 0 else base_price
            
            # Calculate original target percent offsets if they were provided
            if take_profit_price:
                tp_pct = float(take_profit_price) / cached_base
                take_profit_price = round(base_price * tp_pct, 2)
            else:
                take_profit_price = round(base_price * 1.05, 2)
                logger.info(f"Applying default 5% Take-Profit fallback target at ${take_profit_price:.2f}.")

            if stop_loss_price:
                sl_pct = float(stop_loss_price) / cached_base
                stop_loss_price = round(base_price * sl_pct, 2)
            else:
                stop_loss_price = round(base_price * 0.97, 2)
                logger.info(f"Applying default 3% Stop-Loss fallback target at ${stop_loss_price:.2f}.")

            min_allowed_tp = round(base_price + 0.05, 2)
            if take_profit_price < min_allowed_tp:
                logger.info(f"Take-Profit price ${take_profit_price:.2f} is too close to entry or below entry. Raising to ${min_allowed_tp:.2f} to satisfy Alpaca validation with a safety buffer.")
                take_profit_price = min_allowed_tp

            # Safeguard: Alpaca requires stop_loss.stop_price <= base_price - 0.01 for equities
            # We use a 0.5% or 5-cent buffer (whichever is more conservative) to handle live market spread fluctuations
            max_allowed_stop = round(min(base_price * 0.995, base_price - 0.05), 2)
            if stop_loss_price > max_allowed_stop:
                logger.info(f"Stop-Loss price ${stop_loss_price:.2f} is too close to entry or above entry. Capping at ${max_allowed_stop:.2f} to satisfy Alpaca validation with a safety buffer.")
                stop_loss_price = max_allowed_stop
        else:
            take_profit_price = None
            stop_loss_price = None

        if dry_run:
            logger.info(f"[DRY RUN] Would execute order: {action} {qty} shares of {symbol}")
            if action == "BUY" and not is_crypto:
                logger.info(f"[DRY RUN] Bracket Order details -> TP: ${take_profit_price:.2f} | SL: ${stop_loss_price:.2f}")
            return
            
        logger.info(f"EXECUTING ORDER: Submitting {action} order for {qty} shares of {symbol}...")
        try:
            order_result = alpaca_client.execute_market_order(
                symbol, qty, action,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price
            )
            logger.info(f"Order executed successfully! Details: {order_result}")
            
            # Log executed trade to SQLite
            database.log_trade(
                decision_id=decision_id,
                alpaca_order_id=order_result["id"],
                symbol=symbol,
                side=action,
                qty=qty,
                filled_avg_price=order_result.get("filled_avg_price"),
                status=order_result.get("status", "submitted")
            )
            logger.info("Trade transaction logged to database.")
            
        except Exception as e:
            logger.critical(f"FATAL: Order execution failed! Error: {e}")
    else:
        logger.info("No order execution required for this cycle.")

def main():
    parser = argparse.ArgumentParser(description="Autonomous Alpaca AI Trading Agent Runner")
    parser.add_argument("--once", action="store_true", help="Run a single trading cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Perform all calculations, fetch data, query LLM but skip actual order placement.")
    parser.add_argument("--loop", action="store_true", help="Run continuously on the configured intervals.")
    args = parser.parse_args()

    # Validate configuration
    is_valid, msg = config.is_config_valid()
    if not is_valid:
        logger.warning(f"Configuration warning: {msg}")
        logger.warning("Will proceed using mock fallbacks where credentials are required.")

    # Initialize agent components
    logger.info("Initializing trading agent system components...")
    alpaca_client = AlpacaClient()
    data_provider = DataProvider(alpaca_client)
    brain = TradingBrain()
    guardrails = RiskGuardrails()

    # If neither --once nor --loop is chosen, default to once for safety
    if not args.once and not args.loop:
        logger.info("No execution mode specified. Defaulting to a single dry-run cycle (--once --dry-run) for safety.")
        args.once = True
        args.dry_run = True

    if args.once:
        logger.info("Executing single cycle...")
        run_trading_cycle(alpaca_client, data_provider, brain, guardrails, dry_run=args.dry_run)
        logger.info("Single cycle completed.")
    elif args.loop:
        interval_min = config.TRADING_INTERVAL_MINUTES
        logger.info(f"Entering continuous trading loop. Interval: {interval_min} minutes. Press Ctrl+C to stop.")
        try:
            while True:
                run_trading_cycle(alpaca_client, data_provider, brain, guardrails, dry_run=args.dry_run)
                logger.info(f"Sleeping for {interval_min} minutes before next cycle...")
                time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            logger.info("Loop execution interrupted by user. Shutting down trading agent.")
            sys.exit(0)

if __name__ == "__main__":
    main()
