import time
import argparse
from core.database import Database

database = Database()
import logging
import os
import sys
from datetime import datetime

from core import config
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.guardrails import RiskGuardrails
from core.trading_brain import TradingBrain
from core.strategy_rules import is_crypto_symbol, validate_strategy_rule

def get_current_eastern_time() -> datetime:
    """Returns the current datetime in America/New_York timezone."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        return datetime.now(tz)
    except Exception:
        try:
            import pytz
            tz = pytz.timezone("America/New_York")
            return datetime.now(tz)
        except Exception:
            return datetime.now()

def format_positions(positions: dict) -> str:
    """Formats the open positions dict into a clean human-readable string."""
    if not positions:
        return "None."
    parts = []
    for symbol, details in positions.items():
        qty = details.get("qty", 0.0)
        try:
            qty = float(qty)
            qty_str = f"{qty:.4f}".rstrip('0').rstrip('.')
        except ValueError:
            qty_str = str(qty)
        parts.append(f"{symbol} ({qty_str} shares)")
    return ", ".join(parts)


def build_appraisal_universe(screened_symbols: list[str], positions: dict,
                             actual_market_open: bool) -> list[str]:
    """Build a deduplicated universe, excluding equities whenever their market is closed."""
    candidates = [*screened_symbols, *positions.keys()]
    if not actual_market_open:
        candidates = [symbol for symbol in candidates if is_crypto_symbol(symbol)]
    return list(dict.fromkeys(candidates))


def maybe_run_intraday_options_watch(alpaca_client, positions: dict) -> bool:
    """While we HOLD an option position, re-tune the options strategy every cooldown.

    Options are "time bombs" (theta/IV/DTE decay risk), so the strategist should
    stay locked onto any open leveraged position intraday — not just once after
    market close. This runs the options track on a short DB-backed cooldown
    (default 30m). Returns True when a tune was attempted+persisted, False
    otherwise (off cooldown, or nothing to do).
    """
    from datetime import datetime as _dt, timezone as _tz
    from core import database, config as _cfg
    from core.feedback import is_option_contract_symbol, option_underlying

    held_occs = [s for s in positions if is_option_contract_symbol(s)]
    if not held_occs:
        return False

    cooldown_min = float(getattr(_cfg, "OPTIONS_WATCH_COOLDOWN_MINUTES", 30))
    status_ts = database.get_system_state("last_options_intraday_watch")
    due = True
    if status_ts:
        try:
            last_intraday = _dt.fromisoformat(str(status_ts))
            if last_intraday.tzinfo is not None:
                last_intraday = last_intraday.astimezone(_tz.utc).replace(tzinfo=None)
            elapsed = (_dt.utcnow() - last_intraday).total_seconds() / 60.0
            due = elapsed >= cooldown_min
        except Exception:
            due = True

    held_underlyings = list(dict.fromkeys(option_underlying(s) for s in held_occs))
    if not due:
        logger.info(f"Options held ({held_underlyings}); within {cooldown_min}m cooldown, skipping.")
        return False

    from core.strategist import MetaStrategist
    refined = MetaStrategist().run_option_strategy_refinement(alpaca_client)
    database.set_system_state("last_options_intraday_watch", _dt.utcnow().isoformat())
    logger.info(f"INTRADAY options watch refined for {held_underlyings}: {refined}")
    return True


SHOCK_COOLDOWN_MINUTES = 60
# Cooldown between on-demand strategy-refinement attempts for a missing rule.
STRATEGY_REFRESH_COOLDOWN_MINUTES = 60


def shock_cooldown_active(symbol: str) -> bool:
    """Return True if the emergency strategist was triggered for ``symbol`` recently.

    Uses the system_state table to dedupe repeated [SHOCK DETECTED] triggers for
    the same ticker (e.g. a persistent -9% crash) so we don't fire an expensive
    MetaStrategist LLM call every 15-min cycle. Returns True to SKIP re-triggering
    while within the cooldown window.
    """
    last_key = f"shock_last_trigger_{symbol.upper()}"
    last_ts = database.get_system_state(last_key)
    if not last_ts:
        return False
    try:
        last = datetime.fromisoformat(str(last_ts))
    except ValueError:
        return False
    # System state stored in UTC (datetime.utcnow). Compare against naive now.
    elapsed = datetime.utcnow() - last
    return elapsed.total_seconds() < SHOCK_COOLDOWN_MINUTES * 60


def mark_shock_triggered(symbol: str) -> None:
    """Record that the emergency strategist ran for `symbol` (to enforce cooldown)."""
    try:
        database.set_system_state(f"shock_last_trigger_{symbol}",
                                  datetime.utcnow().isoformat())
    except Exception as e:
        logger.debug(f"Could not persist shock cooldown for {symbol}: {e}")


def ensure_active_strategy(symbol: str, alpaca_client: AlpacaClient) -> bool:
    """Generate a missing strategy on demand and confirm that it was persisted.

    Includes a cooldown so a ticker with a persistently-unproducible rule (e.g.
    missing_rule) doesn't re-trigger an expensive MetaStrategist LLM call every
    cycle. Once a refresh is attempted, we don't try again for
    STRATEGY_REFRESH_COOLDOWN_MINUTES unless the rule actually repairs itself.
    """
    active_rule = database.get_active_strategy(symbol)
    is_valid, validation_reason = validate_strategy_rule(symbol, active_rule)
    if is_valid:
        return True

    # Cooldown: skip re-triggering if we already tried to refine this ticker recently.
    last_attempt_key = f"strategy_refresh_attempt_{symbol.upper()}"
    last_attempt = database.get_system_state(last_attempt_key)
    if last_attempt:
        try:
            last = datetime.fromisoformat(str(last_attempt))
            if (datetime.utcnow() - last).total_seconds() < STRATEGY_REFRESH_COOLDOWN_MINUTES * 60:
                logger.info(
                    f"No valid strategy rule for {symbol} ({validation_reason}) but "
                    f"recently refreshed; skipping appraisal this cycle (cooldown)."
                )
                return False
        except ValueError:
            pass

    logger.warning(
        f"No valid strategy rule found for {symbol} ({validation_reason}). "
        "Triggering on-demand strategist run..."
    )
    try:
        from core.strategist import MetaStrategist
        MetaStrategist().run_single_ticker_refinement(symbol, alpaca_client)
    except Exception as err:
        logger.error(f"Could not execute on-demand strategist update for {symbol}: {err}")
    # Mark the attempt regardless of success so we don't hammer the LLM each cycle.
    try:
        database.set_system_state(last_attempt_key, datetime.utcnow().isoformat())
    except Exception:
        pass

    repaired_rule = database.get_active_strategy(symbol)
    is_valid, validation_reason = validate_strategy_rule(symbol, repaired_rule)
    if not is_valid:
        logger.error(
            f"No valid strategy rule is available for {symbol} "
            f"({validation_reason}); skipping appraisal this cycle."
        )
        return False
    return True


# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE)
    ]
)
# Suppress noisy third-party deprecation warnings that are surfaced at INFO/WARNING
# by the google-genai SDK (e.g. the "Direct use of automatic function calling (AFC)"
# advisory). These are cosmetic and clutter the System Activity Logs.
for _noisy_logger in ("google_genai.models", "google_genai", "genai"):
    logging.getLogger(_noisy_logger).setLevel(logging.ERROR)
from core import logger_setup
logger_setup.setup_logging(app_name="agent-trade", env="production")
logger = logging.getLogger("Runner")

def check_execution_window(alpaca_client: AlpacaClient) -> tuple[bool, str]:
    """
    Checks if the current time is between 09:30 and 16:30 Eastern Time on a weekday
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

    # 2. Time-of-day check (standard session open through the configured close buffer)
    current_minutes = now.hour * 60 + now.minute
    start_minutes = 9 * 60 + 30     # 09:30
    end_minutes = 16 * 60 + 30      # 16:30
    
    if current_minutes < start_minutes or current_minutes > end_minutes:
        return False, f"Outside allowed window of 09:30 - 16:30 ET (Current Eastern Time: {now.strftime('%H:%M')})."

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
            from typing import Any
            market_open_today = False
            for entry in calendar_entries:
                entry_any: Any = entry
                entry_date = entry_any.date
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

def _run_trading_cycle_impl(alpaca_client: AlpacaClient, data_provider: DataProvider,
                            brain: TradingBrain, guardrails: RiskGuardrails,
                            dry_run: bool = False) -> tuple[str, str, str]:
    """Executes a single workflow cycle of the autonomous trading agent."""
    import os
    import json
    from datetime import datetime
    
    market_states = []
    
    # 0. Sync Latest Database and State from GCS
    try:
        from core.gcs_sync import download_from_gcs
        download_from_gcs()
        from core.database import init_db
        init_db()
    except Exception as gcs_dl_err:
        logger.error(f"Failed to download files from GCS: {gcs_dl_err}")

    # 1. GCS Kill Switch Check
    try:
        from core.gcs_sync import check_kill_switch
        ks_data = check_kill_switch()
        if ks_data and ks_data.get("status") == "HALTED":
            logger.info("Runner: Skipping execution cycle. System is Halted via GCS Kill Switch.")
            return "SKIPPED_KILL_SWITCH", "UNKNOWN", "System halted by GCS kill switch."
    except Exception as ks_err:
        logger.warning(f"Failed to check GCS kill switch: {ks_err}")

    # Fetch current Eastern Time
    now_et = get_current_eastern_time()
    today_str = now_et.strftime('%Y-%m-%d')
    current_minutes = now_et.hour * 60 + now_et.minute
    
    # This actual exchange state always controls asset eligibility. A bypass must
    # never make equities eligible while their market is closed.
    actual_market_open, market_reason = check_execution_window(alpaca_client)
    asset_scope = "EQUITIES_AND_CRYPTO" if actual_market_open else "CRYPTO_ONLY"

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
        return "ACCOUNT_STATE_FAILED", asset_scope, str(e)

    # 1b. Options auto-close sweep (deterministic safety net for expiry)
    if getattr(config, "OPTIONS_ENABLED", False):
        try:
            from core.option_lifecycle import OptionLifecycle
            lifecycle = OptionLifecycle(alpaca_client)
            closed_options = lifecycle.sweep()
            if closed_options:
                logger.info(f"Option auto-close sweep closed {len(closed_options)} position(s).")
                for c in closed_options:
                    logger.info(f"  - Auto-close: {c.get('summary', c)}")
        except Exception as ec:
            logger.warning(f"Options auto-close sweep failed: {ec}")

    # 2. Fetch current active positions
    try:
        positions = alpaca_client.get_positions()
        logger.info(f"Active Positions: {list(positions.keys())}")
    except Exception as e:
        logger.error(f"Error fetching positions: {e}. Proceeding with assumptions.")
        positions = {}

    # Define weekend check parameters
    weekday = now_et.weekday()
    is_weekend_hours = False
    if weekday >= 5: # Saturday or Sunday
        is_weekend_hours = True
    elif weekday == 4 and current_minutes >= (16 * 60 + 30): # Friday after 16:30
        is_weekend_hours = True
    elif weekday == 0 and current_minutes < (9 * 60): # Monday before 09:00
        is_weekend_hours = True

    # 3. Daily Status Cadence & Friday Smart Sleep Triggers (fully DB backed)
    try:
        from core.discord_notifier import send_discord_message
        positions_str = format_positions(positions)

        # A. Morning Start Message (Weekdays Monday-Friday, at or after 09:00 ET)
        if weekday < 5 and current_minutes >= (9 * 60):
            last_sent = database.get_system_state("last_morning_sent")
            if last_sent != today_str:
                msg = (
                    f"Good Morning! Agent-Trade Desk is active.\n"
                    f"* **Total Equity**: ${equity:,.2f}\n"
                    f"* **Cash Balance**: ${cash:,.2f}\n"
                    f"* **Open Positions**: {positions_str}"
                )
                send_discord_message(msg)
                database.set_system_state("last_morning_sent", today_str)

        # B. Evening equity-desk status. The scheduler and crypto desk remain active.
        if weekday < 5 and current_minutes >= (16 * 60 + 30):
            last_evening_sent = database.get_system_state("last_evening_sent")
            if last_evening_sent != today_str:
                msg = (
                    f"Good Evening! The equity desk is closed and crypto-only monitoring remains active.\n"
                    f"* **Closing Equity**: ${equity:,.2f}\n"
                    f"* **Cash Balance**: ${cash:,.2f}\n"
                    f"* **Open Positions**: {positions_str}"
                )
                send_discord_message(msg)
                database.set_system_state("last_evening_sent", today_str)

                # Strategy-model A/B self-prompt: fire the Discord tally right after
                # the evening status, so results land in front of the user on their
                # own cadence (new-data OR weekly heartbeat) — never more than ~1/day,
                # and harmless if it fails (wrapped in its own try/except).
                try:
                    from tools.strategist_ab_notify import main as ab_notify_main
                    # Analyze the authoritative DB this runner syncs from GCS each
                    # cycle (download_from_gcs -> DATABASE_PATH, done above at cycle
                    # start). --no-pull avoids a redundant 2nd GCS fetch mid-cycle.
                    from core.database import DATABASE_PATH as _AB_DB_PATH  # noqa: E402
                    ab_notify_main(["--no-pull", "--db", str(_AB_DB_PATH)])
                except Exception as ab_err:
                    logger.warning(f"Strategist A/B notify skipped/failed: {ab_err}")

                # C. Daily OPTIONS strategy track (once per day, after market close).
                # Options are leveraged; tune their instrument knobs (conviction
                # threshold, DTE, OTM%, allocation) on a SEPARATE curve from stocks.
                try:
                    last_opts = database.get_system_state("last_options_strategy_sent")
                    if last_opts != today_str:
                        from core.strategist import MetaStrategist
                        refined_opts = MetaStrategist().run_option_strategy_refinement(alpaca_client)
                        database.set_system_state("last_options_strategy_sent", today_str)
                        logger.info(f"Daily OPTIONS strategy track refined: {refined_opts}")
                except Exception as opt_strat_err:
                    logger.warning(f"Daily OPTIONS strategy track failed: {opt_strat_err}")

                # D. INTRADAY OPTIONS WATCH (runs every cycle while we HOLD an option).
                # Options are "time bombs" — theta/IV decay means risk changes intraday,
                # not just daily. While any option position is open, re-run the options
                # strategy track on a short cooldown (default 30m) so the strategist
                # stays locked onto the leveraged position and tunes its knobs as the
                # market / PnL / DTE evolves. Harmless if it fails.
                try:
                    maybe_run_intraday_options_watch(alpaca_client, positions)
                except Exception as opt_watch_err:
                    logger.warning(f"Intraday options watch failed: {opt_watch_err}")
                    
    except Exception as cadence_err:
        logger.error(f"Error handling daily status cadence or smart Friday triggers: {cadence_err}")

    if is_weekend_hours:
        logger.info("Weekend/off-hours mode active. Continuing 24/7 crypto-only monitoring.")

    # Check if we should allow trading for the current market state
    if not actual_market_open:
        logger.info(f"US Equity Market is closed ({market_reason}). Continuing cycle for CRYPTO ONLY trading.")


    # Run the dynamic AI screener to select top candidates
    try:
        from core.screener import run_screener, load_screener_pool
        screener_candidates = load_screener_pool()
        
        # Filter screener candidates to crypto only if outside market hours
        if not actual_market_open:
            screener_candidates = [
                symbol for symbol in screener_candidates 
                if is_crypto_symbol(symbol)
            ]
            logger.info(f"US Equity Market is closed/outside hours. Filtering screener candidates to CRYPTO ONLY: {screener_candidates}")
            
        screened_list = run_screener(alpaca_client, data_provider, watchlist_limit=5, candidates=screener_candidates)
        logger.info(f"Screener generated watchlist: {screened_list}")
    except Exception as screener_err:
        logger.error(f"Screener execution failed: {screener_err}. Falling back to static TRADING_UNIVERSE.")
        screened_list = config.TRADING_UNIVERSE
        
    # Re-filter after the screener because its own error paths can return a static
    # mixed-asset fallback universe.
    filtered_universe = build_appraisal_universe(screened_list, positions, actual_market_open)
    logger.info(f"Final tradeable appraisal universe: {filtered_universe}")
        
    market_states = []
    for symbol in filtered_universe:
        # Check if an active strategy rule exists in the database for this symbol.
        # If no rule exists, trigger on-demand strategist refinement to write a tailored rule immediately.
        if not ensure_active_strategy(symbol, alpaca_client):
            continue

        logger.info(f"Fetching market data and indicators for {symbol}...")
        state = data_provider.get_market_state(symbol)
        if state:
            market_states.append(state)
            logger.info(f"{symbol} latest close: ${state['current_price']:.2f} | RSI: {state['indicators'].get('rsi_14')}")
            
            # Intraday Shock Check (Triggering dynamic emergency strategist updates)
            # Threshold: 3.0% for stock indices (SPY/QQQ), 8.0% for highly volatile crypto (SOL)
            is_crypto = is_crypto_symbol(symbol)
            shock_threshold = 8.0 if is_crypto else 3.0
            daily_change = state["daily_return_pct"]
            
            if abs(daily_change) >= shock_threshold:
                if shock_cooldown_active(symbol):
                    logger.info(
                        f"[SHOCK] {symbol} daily move is {daily_change:.2f}% (Limit: {shock_threshold}%) "
                        f"but emergency strategist already ran recently; skipping (cooldown {SHOCK_COOLDOWN_MINUTES}m)."
                    )
                else:
                    logger.warning(f"[SHOCK DETECTED] INTRADAY REGIME SHOCK: {symbol} daily move is {daily_change:.2f}% (Limit: {shock_threshold}%). Triggering emergency strategist run...")
                    mark_shock_triggered(symbol)
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
        return "NO_MARKET_DATA", asset_scope, "No eligible ticker produced market data."

    # 4. Fetch recent decisions for context memory
    try:
        recent_decisions = database.get_recent_decisions(limit=5)
    except Exception as e:
        logger.error(f"Error retrieving recent decisions from DB: {e}")
        recent_decisions = []

    # 5. Let the AI Brain reason and generate per-ticker decisions (Option B)
    logger.info("Querying AI strategy brain for proposed actions...")
    appraised_positions = positions if actual_market_open else {
        symbol: details for symbol, details in positions.items() if is_crypto_symbol(symbol)
    }
    decisions = brain.make_decision(market_states, account_state, appraised_positions, recent_decisions)
    if not isinstance(decisions, list):
        decisions = [decisions]  # tolerate a single dict fallback

    # Shared cycle context for cumulative budget / per-cycle trade cap across all
    # per-ticker decisions this cycle. A single cycle_id groups this batch of
    # per-ticker decisions so the dashboard can read them as one "cycle run".
    cycle_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    cycle_context = {"spent": 0.0, "trades": 0}

    executed_results = []
    rejected_count = 0
    hold_count = 0

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        # Enrich decision with current pricing data for guardrail calculations
        target_symbol = decision.get("symbol", "").upper()
        current_price = 0.0
        atr_pct = None
        for state in market_states:
            if state["symbol"] == target_symbol:
                current_price = state["current_price"]
                atr_pct = state.get("indicators", {}).get("atr_pct")
                break
        decision["current_price"] = current_price
        decision["atr_pct"] = atr_pct

        # 6. Filter proposed decision through Risk Guardrails
        is_approved, status_msg, adjusted_decision = guardrails.validate_and_adjust_decision(
            decision, account_state, positions, cycle_context=cycle_context
        )

        action = adjusted_decision.get("action", "HOLD")
        symbol = adjusted_decision.get("symbol", "")
        qty = adjusted_decision.get("quantity", 0.0)
        thought_process = adjusted_decision.get("thought_process", "")
        reasoning = adjusted_decision.get("reasoning") or adjusted_decision.get("thought_process")
        logger.info(f"[{symbol or 'PORTFOLIO'}] Guardrails Result: {status_msg}")

        # 7. Log decision to SQLite Database
        decision_id = None
        try:
            decision_id = database.log_decision(
                ticker_indicators={s["symbol"]: s["indicators"] for s in market_states},
                portfolio_state={"cash": cash, "equity": equity, "positions": positions},
                thought_process=thought_process,
                proposed_action=action,
                proposed_symbol=symbol,
                proposed_qty=qty,
                is_approved=is_approved,
                rejection_reason=status_msg if not is_approved else None,
                direction=adjusted_decision.get("direction"),
                conviction=adjusted_decision.get("conviction"),
                instrument=adjusted_decision.get("instrument"),
                cycle_id=cycle_id,
                reasoning=reasoning,
            )
        except Exception as e:
            logger.error(f"Failed to log decision to DB: {e}")

        # Record per-ticker conviction for dashboard badges / change indicators
        if symbol:
            try:
                database.log_ticker_conviction(
                    cycle_id=cycle_id,
                    symbol=symbol,
                    direction=adjusted_decision.get("direction"),
                    conviction=adjusted_decision.get("conviction"),
                    reasoning=reasoning,
                )
            except Exception as e:
                logger.error(f"Failed to log ticker conviction for {symbol}: {e}")

        if not is_approved or action in ("HOLD", "NO_ACTION"):
            if is_approved and action in ("HOLD", "NO_ACTION"):
                hold_count += 1
            else:
                rejected_count += 1
            continue

        # 8. Execution Phase (only if approved and action not HOLD)
        if not (action in ("BUY", "SELL") and qty > 0):
            continue

        # Option vs stock routing
        from core.guardrails import is_occ_symbol as _occ_hint
        is_crypto = is_crypto_symbol(symbol)
        option_executed = False

        if adjusted_decision.get("instrument") == "option" or _occ_hint(symbol):
            try:
                from core.option_executor import OptionExecutor
                option_executor = OptionExecutor(alpaca_client)
                option_result = option_executor.execute(adjusted_decision, account_state)
                logger.info(f"Option order result for {symbol}: {option_result}")
                option_symbol = option_result.get("symbol", symbol)
                # Parse OCC metadata (root/DTE/type/strike) for the trade log so
                # the feedback/learning engine can attribute option PnL.
                option_meta = {}
                try:
                    from core.option_picker import parse_option_symbol
                    parsed_occ = parse_option_symbol(option_symbol)
                    if parsed_occ:
                        option_meta = {
                            "option_type": parsed_occ["type"],
                            "option_dte": (
                                parsed_occ["expiration_date"] - datetime.utcnow().date()
                            ).days,
                            "strike": parsed_occ["strike_price"],
                            "contract_symbol": parsed_occ["symbol"],
                        }
                except Exception as occ_parse_err:
                    logger.warning(f"Could not parse OCC metadata for {option_symbol}: {occ_parse_err}")
                try:
                    database.log_execution(
                        decision_id=decision_id, attempt=1,
                        symbol=option_symbol, side=action.lower(),
                        qty=option_result.get("contracts", qty), order_type="option",
                        status=str(option_result.get("status", "submitted")),
                        error=None if option_result.get("status") != "failed" else option_result.get("summary"),
                        alpaca_order_id=option_result.get("order_info", {}).get("id"),
                    )
                except Exception as log_ex:
                    logger.error(f"Failed to log option execution: {log_ex}")
                # Log the option fill to the trades table so the feedback engine
                # (compute_closed_round_trips) can track option PnL for learning.
                if str(option_result.get("status", "")).lower() in ("filled", "partially_filled", "closed"):
                    try:
                        database.log_trade(
                            decision_id=decision_id,
                            alpaca_order_id=option_result.get("order_info", {}).get("id"),
                            symbol=option_symbol,
                            side=action,
                            qty=option_result.get("contracts", qty),
                            filled_avg_price=option_result.get("order_info", {}).get("filled_avg_price"),
                            status=str(option_result.get("status", "submitted")),
                            **option_meta,
                        )
                    except Exception as tr_ex:
                        logger.error(f"Failed to log option trade: {tr_ex}")
                executed_results.append(f"OPTION {action} {option_result.get('contracts', qty)}x {symbol}")
                option_executed = True
            except Exception as e:
                logger.error(f"Option execution failed for {symbol}: {e}")
                executed_results.append(f"OPTION-FAIL {symbol}: {e}")
                option_executed = True  # count as handled so we skip stock path below

        if option_executed:
            cycle_context["trades"] = int(cycle_context.get("trades", 0)) + 1
            continue

        if not actual_market_open and not is_crypto:
            logger.error(f"Blocking {action} for equity {symbol}: market closed.")
            continue

        if action == "BUY" and not is_crypto:
            # Rebase TP/SL on a fresh price to avoid validation drift
            try:
                base_price = alpaca_client.get_latest_price(symbol)
            except Exception:
                base_price = float(current_price)
            take_profit_price = adjusted_decision.get("take_profit_price")
            stop_loss_price = adjusted_decision.get("stop_loss_price")
            cached_base = float(current_price) if current_price > 0 else base_price
            if take_profit_price:
                take_profit_price = round(base_price * (float(take_profit_price)/cached_base), 2)
            else:
                take_profit_price = round(base_price * 1.05, 2)
            if stop_loss_price:
                stop_loss_price = round(base_price * (float(stop_loss_price)/cached_base), 2)
            else:
                stop_loss_price = round(base_price * 0.97, 2)
            max_allowed_stop = round(min(base_price * 0.995, base_price - 0.05), 2)
            if stop_loss_price > max_allowed_stop:
                stop_loss_price = max_allowed_stop
            min_allowed_tp = round(base_price + 0.05, 2)
            if take_profit_price < min_allowed_tp:
                take_profit_price = min_allowed_tp
        elif action == "BUY" and is_crypto and getattr(config, "CRYPTO_BRACKET_ENABLED", True):
            # Crypto bracket support: attach TP/SL to crypto BUYs so 24/7
            # positions aren't left running unhedged. Uses config defaults when
            # the brain doesn't supply explicit levels.
            try:
                base_price = alpaca_client.get_latest_price(symbol)
            except Exception:
                base_price = float(current_price)
            take_profit_price = adjusted_decision.get("take_profit_price")
            stop_loss_price = adjusted_decision.get("stop_loss_price")
            cached_base = float(current_price) if current_price > 0 else base_price
            if take_profit_price:
                take_profit_price = round(base_price * (float(take_profit_price)/cached_base), 2)
            else:
                take_profit_price = round(base_price * (1.0 + getattr(config, "CRYPTO_TAKE_PROFIT_PCT", 0.05)), 2)
            if stop_loss_price:
                stop_loss_price = round(base_price * (float(stop_loss_price)/cached_base), 2)
            else:
                stop_loss_price = round(base_price * (1.0 - getattr(config, "CRYPTO_STOP_LOSS_PCT", 0.03)), 2)
            # Ensure TP > SL and both are valid relative to base price.
            max_allowed_stop = round(min(base_price * 0.995, base_price - 0.01), 2)
            if stop_loss_price > max_allowed_stop:
                stop_loss_price = max_allowed_stop
            min_allowed_tp = round(base_price + 0.01, 2)
            if take_profit_price < min_allowed_tp:
                take_profit_price = min_allowed_tp
        else:
            take_profit_price = None
            stop_loss_price = None

        if dry_run:
            logger.info(f"[DRY RUN] Would execute order: {action} {qty} of {symbol}")
            executed_results.append(f"DRY {action} {qty}x {symbol}")
            continue

        logger.info(f"EXECUTING ORDER: {action} {qty} {symbol}...")
        try:
            order_result = alpaca_client.execute_market_order(
                symbol, qty, action,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price
            )
            logger.info(f"Order executed: {order_result}")
            try:
                database.log_execution(
                    decision_id=decision_id, attempt=1, symbol=symbol, side=action,
                    qty=order_result.get("qty", qty),
                    order_type=order_result.get("order_type", "market"),
                    status=order_result.get("status", "submitted"),
                    alpaca_order_id=order_result.get("id"),
                    filled_avg_price=order_result.get("filled_avg_price")
                )
            except Exception as exec_log_err:
                logger.error(f"Failed to log execution: {exec_log_err}")
            database.log_trade(
                decision_id=decision_id, alpaca_order_id=order_result["id"], symbol=symbol,
                side=action, qty=qty,
                filled_avg_price=order_result.get("filled_avg_price"),
                status=order_result.get("status", "submitted")
            )
            executed_results.append(f"{action} {qty} {symbol}")
            # Account this executed trade toward the per-cycle cap and cumulative budget
            cycle_context["trades"] = int(cycle_context.get("trades", 0)) + 1
            cycle_context["spent"] = float(cycle_context.get("spent", 0.0)) + qty * float(order_result.get("filled_avg_price", current_price) or current_price)
        except Exception as e:
            logger.critical(f"FATAL: Order execution failed for {symbol}: {e}")
            try:
                database.log_execution(
                    decision_id=decision_id, attempt=1, symbol=symbol, side=action, qty=qty,
                    order_type="bracket" if (action == "BUY" and not is_crypto and take_profit_price and stop_loss_price) else "market",
                    status="failed", error=str(e)
                )
            except Exception as exec_log_err:
                logger.error(f"Failed to log failed execution: {exec_log_err}")

    # 8b. Broker-order reconciliation (learning-engine safety net).
    # The runner logs fills it executes itself, but broker-side fills — option
    # SELL-to-close, the option auto-close sweep, TP/SL bracket fills, dust
    # liquidations — never reach the local trades table. Backfill ANY closed
    # broker order (deduped by alpaca_order_id) so the feedback engine's
    # round-trip computation sees both stock AND option PnL. This is what lets
    # the strategist learn from options.
    try:
        from core import database as _db
        broker_orders = alpaca_client.get_executed_orders(limit=500)
        if broker_orders:
            inserted = _db.reconcile_broker_orders(broker_orders)
            if inserted:
                logger.info(f"Broker reconciliation backfilled {inserted} fill(s) into trades table.")
    except Exception as reco_err:
        logger.error(f"Broker-order reconciliation failed: {reco_err}")

    # 9. Sync database and logs to GCS if configured
    try:
        from core.gcs_sync import upload_to_gcs
        upload_to_gcs()
    except Exception as gcs_err:
        logger.error(f"Failed to sync to GCS: {gcs_err}")

    if not executed_results:
        logger.info("No order was executed this cycle.")
    else:
        logger.info(f"Executed in this cycle: {executed_results}")
    return "COMPLETED", asset_scope, ("Cycle completed. Executed: " + (", ".join(executed_results) if executed_results else "no trades"))


def run_trading_cycle(alpaca_client: AlpacaClient, data_provider: DataProvider,
                      brain: TradingBrain, guardrails: RiskGuardrails,
                      dry_run: bool = False):
    """Run one observable cycle and persist a heartbeat on every exit path.

    Wraps _run_trading_cycle_impl with a lifecycle heartbeat that records
    STARTED before execution and FAILED/COMPLETED on every exit path (including
    unhandled exceptions via finally). The heartbeat is persisted to the local
    SQLite DB *and* synced to GCS so the dashboard can report freshness even if
    the container is preempted.
    """
    execution_id = os.getenv("CLOUD_RUN_EXECUTION", os.getenv("K_REVISION", "local"))
    status = "FAILED"
    asset_scope = "UNKNOWN"
    message = "Cycle terminated unexpectedly."
    database.record_cycle_heartbeat("STARTED", asset_scope, "Cycle started.", execution_id)
    try:
        status, asset_scope, message = _run_trading_cycle_impl(
            alpaca_client, data_provider, brain, guardrails, dry_run=dry_run
        )
        return status, asset_scope, message
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Unhandled trading-cycle failure.")
        raise
    finally:
        database.record_cycle_heartbeat(status, asset_scope, message, execution_id)
        try:
            from core.gcs_sync import upload_to_gcs
            upload_to_gcs()
        except Exception as sync_error:
            logger.error(f"Final heartbeat sync to GCS failed: {sync_error}")
        logger.info(
            f"Cycle heartbeat finalized: execution={execution_id} "
            f"scope={asset_scope} status={status} message={message}"
        )

def main():
    parser = argparse.ArgumentParser(description="Autonomous Alpaca AI Trading Agent Runner")
    parser.add_argument("--once", action="store_true", help="Run a single trading cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Perform all calculations, fetch data, query LLM but skip actual order placement.")
    parser.add_argument("--loop", action="store_true", help="Run continuously on the configured intervals.")
    parser.add_argument("--eod-report", action="store_true", help="Fetch today's trading statistics and publish an EOD report to Discord.")
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

    if args.eod_report:
        logger.info("Executing End-of-Day Performance Audit Report dispatch...")
        from core.performance_auditor import send_eod_report
        success = send_eod_report()
        if success:
            logger.info("EOD Report published successfully.")
        else:
            logger.error("Failed to publish EOD Report.")
        sys.exit(0)

    brain = TradingBrain()
    guardrails = RiskGuardrails()

    # If neither --once, --loop, nor --eod-report is chosen, default to once for safety
    if not args.once and not args.loop and not args.eod_report:
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
