#!/usr/bin/env python3
"""SOL/USD sideloaded trading lane — paper trading, same DB.

A dedicated SOL/USD-only trading cycle (the second sideload lane, after AMD).
It reuses agent-trade's indicator engine, brain, guardrails, and Alpaca client,
but with SOL-tuned config (``config_sideload_sol.py``) and a SOL-expert prompt.

It writes decisions/trades to the SAME database so dashboard.agenttrade.us and
treatmotivated.capital pick SOL up automatically.

Usage:
    python -m sideload.runner_sideload_sol --once --dry-run   # SOL decision, no order
    python -m sideload.runner_sideload_sol --once             # place a paper SOL order
    python -m sideload.runner_sideload_sol --loop             # continuous 15-min loop
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

# Apply SOL-tuned overrides BEFORE constructing brain/guardrails/client.
from sideload import config_sideload_sol as sol_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
sol_cfg.apply_sol_overrides()

from core import config, database
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.guardrails import RiskGuardrails
from core.trading_brain import TradingBrain
from core import logger_setup

logger = logging.getLogger("SideloadSolRunner")


def _build_cycle_id() -> str:
    return f"{sol_cfg._base.SL_CYCLE_PREFIX}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"


def run_single_cycle(alpaca_client: AlpacaClient, data_provider: DataProvider,
                     brain: TradingBrain, guardrails: RiskGuardrails,
                     dry_run: bool = False) -> dict:
    """Run one SOL trading cycle and return a summary dict."""
    symbol = sol_cfg._base.SL_SYMBOL
    interval = sol_cfg._base.SL_INTERVALS[0] if sol_cfg._base.SL_INTERVALS else "15min"

    # 1. Account state + positions
    account_state = alpaca_client.get_account_state()
    positions = alpaca_client.get_positions()
    cash = float(account_state.get("cash", 0.0) or 0.0)
    equity = float(account_state.get("equity", 0.0) or 0.0)
    logger.info(f"[{symbol}] equity=${equity:,.2f} cash=${cash:,.2f}")

    # 2. Market state (indicators) for SOL
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

    # 4. Brain decision with SOL-expert positioning
    decisions = brain.make_decision(
        market_states, account_state, appraised_positions, recent_decisions,
        expert_instruction=sol_cfg.sol_expert_instruction(),
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
            logger.info(f"Ignoring non-SOL decision for {target_symbol}.")
            continue

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
                instrument=adjusted.get("instrument"),
                cycle_id=cycle_id,
                reasoning=reasoning,
                model=adjusted.get("model"),
                entry_gate=adjusted.get("entry_gate"),
            )
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")

        if not is_approved or action in ("HOLD", "NO_ACTION"):
            logger.info(f"[{symbol}] {status_msg}")
            continue

        if not (action in ("BUY", "SELL") and qty > 0):
            continue

        # 6. TP/SL defaults for SOL BUYs (crypto bracket).
        take_profit_price = None
        stop_loss_price = None
        if action == "BUY":
            base_price = float(market_state.get("current_price", 0.0) or 0.0)
            take_profit_price = round(base_price * 1.05, 2)
            stop_loss_price = round(base_price * 0.97, 2)

        if dry_run:
            logger.info(f"[DRY RUN] Would execute: {action} {qty} of {symbol}")
            executed_results.append(f"DRY {action} {qty}x {symbol}")
            continue

        logger.info(f"EXECUTING {action} {qty} {symbol}...")
        try:
            order_result = alpaca_client.execute_market_order(
                symbol, qty, action,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
            )
            try:
                database.log_execution(
                    decision_id=decision_id, attempt=1, symbol=symbol, side=action,
                    qty=order_result.get("qty", qty),
                    order_type=order_result.get("order_type", "market"),
                    status=order_result.get("status", "submitted"),
                    error=order_result.get("error"),
                    alpaca_order_id=str(order_result.get("id", "")),
                )
            except Exception as log_err:
                logger.error(f"Failed to log execution: {log_err}")
            executed_results.append(order_result)
        except Exception as e:
            logger.error(f"Execution failed for {symbol}: {e}")
            log_exception_to_jira(e, "SOL Execution Failure",
                                  {"symbol": symbol, "cycle_id": cycle_id})

    return {
        "status": "ok",
        "symbol": symbol,
        "cycle_id": cycle_id,
        "executed": executed_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SOL/USD sideloaded trading cycle")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions, no orders")
    parser.add_argument("--cycles", type=int, default=1, help="Max cycles for --loop")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-sol")
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
                logger.critical(f"SOL cycle failed: {e}")
                log_exception_to_jira(e, "SOL Cycle Failure")
            count += 1
            if count < args.cycles:
                time.sleep(60)
    else:
        _one()


if __name__ == "__main__":
    main()