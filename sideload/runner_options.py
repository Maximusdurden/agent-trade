#!/usr/bin/env python3
"""AMD options sideloaded trading lane — paper trading, same DB.

A dedicated AMD-OPTIONS-only trading cycle (P3: split stocks/options into
dedicated lanes). This lane owns AMD OPTIONS exclusively; the stock lane
(``runner_sideload.py``) owns AMD shares. It reuses agent-trade's indicator
engine, brain, guardrails, and the full options stack (option_picker,
option_executor, option_lifecycle, option_risk), but with AMD-specific option
tuning (DTE/OTM/delta/conviction) applied via
``config_sideload.apply_sideload_options_overrides()``.

It writes decisions/trades to the SAME database so dashboard.agenttrade.us and
treatmotivated.capital pick AMD options up automatically.

Usage:
    python -m sideload.runner_options --once --dry-run   # AMD option decision, no order
    python -m sideload.runner_options --once             # place a paper AMD option order
    python -m sideload.runner_options --loop             # continuous 15-min loop
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Apply AMD-tuned overrides (stock knobs) THEN options overrides BEFORE
# constructing brain/guardrails/client.
from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
sl_cfg.apply_sideload_overrides()
sl_cfg.apply_sideload_options_overrides()

from core import config, database
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.guardrails import RiskGuardrails
from core.trading_brain import TradingBrain
from core import logger_setup

logger = logging.getLogger("SideloadOptionsRunner")


def _build_cycle_id() -> str:
    return f"{sl_cfg.SL_CYCLE_PREFIX}_opt-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"


def _amd_options_expert_instruction() -> str:
    """System-level instruction positioning the brain as an AMD-options expert.

    Emphasizes: options are leveraged (conviction-gated), long calls AND puts
    (both directions), AMD's event-driven volatility, and the DTE/OTM discipline.
    """
    return f"""
AMD-OPTIONS EXPERT POSITIONING (SIDELOAD OPTIONS LANE):
You are the world's foremost AMD OPTIONS specialist. You trade ONLY {sl_cfg.SL_SYMBOL}
options — long calls (bullish) and long puts (bearish) — never shares. You are
measured on consistent expectancy, not on trading every day.

AMD OPTIONS PERSONALITY:
- AMD is a HIGH-BETA semiconductor. Its options are event-driven: earnings and
  AI/data-center guidance dominate IV and price. A single event can gap AMD
  several percent, which is where option gamma lives.
- You express a DIRECTIONAL view (bullish -> long call, bearish -> long put) with
  an honest CONVICTION score. Options are leveraged, so only route to options at
  conviction >= {sl_cfg.SL_OPTIONS_CONVICTION_THRESHOLD:.1f}. Below that, HOLD.
- Respect the DTE window ({sl_cfg.SL_OPTIONS_DTE_MIN}-{sl_cfg.SL_OPTIONS_DTE_MAX} days).
  Do NOT hold through earnings/FOMC unless the setup is exceptional — the event
  gate will flatten you anyway.
- Size by premium cost (ask * 100 * contracts), capped at
  {sl_cfg.SL_OPTIONS_MAX_ALLOC_PCT:.0%} of equity per position.
- You trade BOTH directions: buy the pullback (long call) AND the overbought fade
  (long put). A day with no trade is a good day if the setup was weak.

EXPERT DISCIPLINE:
- ONLY enter on HIGH-CONVICTION, backtest-validated setups. If the setup is not
  clearly a winner, output HOLD.
- Never average down into a losing option position. Never chase momentum.
- Your conviction must be honest: 0.7+ only when multiple indicators (RSI,
  VWAP edge, regime, news) agree.
"""


def run_single_cycle(alpaca_client: AlpacaClient, data_provider: DataProvider,
                     brain: TradingBrain, guardrails: RiskGuardrails,
                     dry_run: bool = False) -> dict:
    """Run one AMD-options cycle and return a summary dict."""
    symbol = sl_cfg.SL_SYMBOL
    interval = sl_cfg.SL_INTERVALS[0] if sl_cfg.SL_INTERVALS else "15min"

    # 1. Account state + positions
    account_state = alpaca_client.get_account_state()
    positions = alpaca_client.get_positions()
    cash = float(account_state.get("cash", 0.0) or 0.0)
    equity = float(account_state.get("equity", 0.0) or 0.0)
    logger.info(f"[{symbol} OPTIONS] equity=${equity:,.2f} cash=${cash:,.2f}")

    # 1b. Options auto-close sweep (deterministic safety net for expiry).
    try:
        from core.option_lifecycle import OptionLifecycle
        lifecycle = OptionLifecycle(alpaca_client)
        closed = lifecycle.sweep()
        if closed:
            logger.info(f"AMD options auto-close sweep closed {len(closed)} position(s).")
    except Exception as e:
        logger.warning(f"AMD options auto-close sweep failed: {e}")

    # 1c. Options risk sweep (event gate + vega/delta caps). Only closes.
    try:
        from core.option_lifecycle import OptionLifecycle
        lifecycle = OptionLifecycle(alpaca_client)
        risk_closed = lifecycle.risk_sweep(account_state)
        if risk_closed:
            logger.info(f"AMD options risk sweep closed {len(risk_closed)} position(s).")
    except Exception as e:
        logger.warning(f"AMD options risk sweep failed: {e}")

    # 2. Market state (indicators) for AMD
    market_state = data_provider.get_market_state(symbol, timeframe_str=interval)
    if not market_state:
        logger.error(f"No market data for {symbol}; aborting cycle.")
        return {"status": "no_data", "symbol": symbol}

    market_states = [market_state]
    appraised_positions = {k: v for k, v in positions.items() if k.upper() == symbol}

    # 3. Recent decisions (for the brain's memory)
    try:
        recent_decisions = database.get_recent_decisions(limit=10)
    except Exception as e:
        logger.warning(f"Could not fetch recent decisions: {e}")
        recent_decisions = []

    # 4. Brain decision with AMD-options expert positioning.
    decisions = brain.make_decision(
        market_states, account_state, appraised_positions, recent_decisions,
        expert_instruction=_amd_options_expert_instruction(),
    )
    if not isinstance(decisions, list):
        decisions = [decisions]

    cycle_id = _build_cycle_id()
    cycle_context = {"spent": 0.0, "trades": 0}
    executed_results = []

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        target_symbol = str(decision.get("symbol", "") or "").upper()
        if target_symbol and target_symbol != symbol:
            logger.info(f"Ignoring non-AMD decision for {target_symbol}.")
            continue

        # Enrich with pricing/indicators for guardrails.
        decision["current_price"] = market_state.get("current_price", 0.0)
        decision["atr_pct"] = market_state.get("indicators", {}).get("atr_pct")
        decision["indicators"] = market_state.get("indicators", {}) or {}

        is_approved, status_msg, adjusted = guardrails.validate_and_adjust_decision(
            decision, account_state, positions, cycle_context=cycle_context
        )
        action = str(adjusted.get("action", "HOLD")).upper()
        qty = float(adjusted.get("quantity", 0.0) or 0.0)
        thought = adjusted.get("thought_process", "")
        reasoning = adjusted.get("reasoning") or thought
        instrument = adjusted.get("instrument")

        # 5. Log decision to the SAME DB (dashboard picks it up).
        decision_id = None
        try:
            decision_id = database.log_decision(
                ticker_indicators={symbol: market_state.get("indicators", {})},
                portfolio_state={"cash": cash, "equity": equity, "positions": positions},
                thought_process=thought,
                proposed_action=action,
                proposed_symbol=symbol,
                proposed_qty=qty,
                is_approved=is_approved,
                rejection_reason=status_msg if not is_approved else None,
                direction=adjusted.get("direction"),
                conviction=adjusted.get("conviction"),
                instrument=instrument,
                cycle_id=cycle_id,
                reasoning=reasoning,
                model=adjusted.get("model"),
                entry_gate=adjusted.get("entry_gate"),
            )
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")

        if not is_approved or action in ("HOLD", "NO_ACTION"):
            logger.info(f"[{symbol} OPTIONS] {status_msg}")
            continue

        if not (action in ("BUY", "SELL") and qty > 0):
            continue

        # 6. Route to the OPTIONS executor (this lane is options-only).
        if instrument != "option":
            logger.info(f"[{symbol} OPTIONS] Brain chose instrument={instrument}; "
                        f"options lane only executes options. Holding.")
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would execute option: {action} {qty} of {symbol} "
                        f"(direction={adjusted.get('direction')})")
            executed_results.append(f"DRY OPTION {action} {qty}x {symbol}")
            continue

        logger.info(f"EXECUTING option {action} {qty} {symbol}...")
        try:
            from core.option_executor import OptionExecutor
            option_executor = OptionExecutor(alpaca_client)
            option_result = option_executor.execute(adjusted, account_state)
            logger.info(f"Option order result for {symbol}: {option_result}")
            executed_results.append(option_result)
        except Exception as e:
            logger.error(f"Option execution failed for {symbol}: {e}")
            log_exception_to_jira(e, "AMD Options Execution Failure",
                                  {"symbol": symbol, "cycle_id": cycle_id})

    return {
        "status": "ok",
        "symbol": symbol,
        "cycle_id": cycle_id,
        "executed": executed_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD options sideloaded trading cycle")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions, no orders")
    parser.add_argument("--cycles", type=int, default=1, help="Max cycles for --loop")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-options")
    client = AlpacaClient()
    data_provider = DataProvider(client)
    brain = TradingBrain()
    guardrails = RiskGuardrails()

    def _one():
        return run_single_cycle(client, data_provider, brain, guardrails, dry_run=args.dry_run)

    if args.loop:
        count = 0
        while count < args.cycles:
            try:
                _one()
            except Exception as e:
                logger.critical(f"AMD options cycle failed: {e}")
                log_exception_to_jira(e, "AMD Options Cycle Failure")
            count += 1
            if count < args.cycles:
                time.sleep(60)
    else:
        _one()


if __name__ == "__main__":
    main()