#!/usr/bin/env python3
"""AMD sideloaded trading cycle — paper trading, same DB.

A dedicated AMD-only trading cycle for the sideload lane. It reuses agent-trade's
indicator engine, brain, guardrails, and Alpaca client, but:

  - Applies AMD-tuned config overrides (sideload/config_sideload.py).
  - Positions the brain as an AMD expert/king/god (high-conviction, backtest-
    validated entries only; skip low-confidence days).
  - Writes decisions/trades to the SAME database (so dashboard.agenttrade.us and
    treatmotivated.capital pick AMD up automatically, zero changes).

Usage:
    python -m sideload.runner_sideload --once --dry-run   # AMD decision, no order
    python -m sideload.runner_sideload --once             # place a paper AMD order
    python -m sideload.runner_sideload --loop             # continuous 15-min loop
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

# Apply AMD-tuned overrides BEFORE constructing brain/guardrails/client.
from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
sl_cfg.apply_sideload_overrides()

from core import config, database
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.guardrails import RiskGuardrails
from core.trading_brain import TradingBrain
from core import logger_setup

logger = logging.getLogger("SideloadRunner")


def _build_cycle_id() -> str:
    return f"{sl_cfg.SL_CYCLE_PREFIX}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"


def run_single_cycle(alpaca_client: AlpacaClient, data_provider: DataProvider,
                     brain: TradingBrain, guardrails: RiskGuardrails,
                     dry_run: bool = False) -> dict:
    """Run one AMD trading cycle and return a summary dict."""
    symbol = sl_cfg.SL_SYMBOL
    interval = sl_cfg.SL_INTERVALS[0] if sl_cfg.SL_INTERVALS else "15min"

    # 1. Account state + positions
    account_state = alpaca_client.get_account_state()
    positions = alpaca_client.get_positions()
    cash = float(account_state.get("cash", 0.0) or 0.0)
    equity = float(account_state.get("equity", 0.0) or 0.0)
    logger.info(f"[{symbol}] equity=${equity:,.2f} cash=${cash:,.2f}")

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

    # 4. Brain decision with AMD-expert positioning
    decisions = brain.make_decision(
        market_states, account_state, appraised_positions, recent_decisions,
        expert_instruction=sl_cfg.amd_expert_instruction(),
    )
    if not isinstance(decisions, list):
        decisions = [decisions]

    cycle_id = _build_cycle_id()
    cycle_context = {"spent": 0.0, "trades": 0}
    executed_results = []
    # Capture the AMD decision payload so it can be re-inserted into a fresh GCS
    # DB right before upload (race-condition fix — see step 8).
    amd_decision_payload = None

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        target_symbol = str(decision.get("symbol", "") or "").upper()
        # Only act on AMD in this lane.
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

        # Capture the payload for the pre-upload re-insert (step 8).
        amd_decision_payload = {
            "ticker_indicators": {symbol: market_state.get("indicators", {})},
            "portfolio_state": {"cash": cash, "equity": equity, "positions": positions},
            "thought_process": thought,
            "proposed_action": action,
            "proposed_symbol": symbol,
            "proposed_qty": qty,
            "is_approved": is_approved,
            "rejection_reason": status_msg if not is_approved else None,
            "direction": adjusted.get("direction"),
            "conviction": adjusted.get("conviction"),
            "instrument": adjusted.get("instrument"),
            "cycle_id": cycle_id,
            "reasoning": reasoning,
            "model": adjusted.get("model"),
            "entry_gate": adjusted.get("entry_gate"),
        }

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

        try:
            database.log_ticker_conviction(
                cycle_id=cycle_id, symbol=symbol,
                direction=adjusted.get("direction"),
                conviction=adjusted.get("conviction"),
                reasoning=reasoning,
            )
        except Exception as e:
            logger.error(f"Failed to log conviction: {e}")

        if not is_approved or action in ("HOLD", "NO_ACTION"):
            logger.info(f"[{symbol}] {status_msg}")
            continue

        if not (action in ("BUY", "SELL") and qty > 0):
            continue

        # 6. TP/SL defaults for AMD BUYs (equity bracket).
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
                    alpaca_order_id=order_result.get("id"),
                    filled_avg_price=order_result.get("filled_avg_price"),
                )
            except Exception as e:
                logger.error(f"Failed to log execution: {e}")
            database.log_trade(
                decision_id=decision_id, alpaca_order_id=order_result["id"],
                symbol=symbol, side=action, qty=qty,
                filled_avg_price=order_result.get("filled_avg_price"),
                status=order_result.get("status", "submitted"),
            )
            executed_results.append(f"{action} {qty} {symbol}")
            cycle_context["trades"] = int(cycle_context.get("trades", 0)) + 1
        except Exception as e:
            logger.critical(f"Order execution failed for {symbol}: {e}")
            log_exception_to_jira(e, "AMD Sideload Order Execution Failure",
                                  {"symbol": symbol, "action": action, "qty": qty})
            try:
                database.log_execution(
                    decision_id=decision_id, attempt=1, symbol=symbol, side=action,
                    qty=qty, order_type="market", status="failed", error=str(e),
                )
            except Exception as log_err:
                logger.error(f"Failed to log failed execution: {log_err}")

    # 7. Broker-order reconciliation (so broker-side fills reach the DB).
    try:
        broker_orders = alpaca_client.get_executed_orders(limit=500)
        if broker_orders:
            inserted = database.reconcile_broker_orders(broker_orders)
            if inserted:
                logger.info(f"Broker reconciliation backfilled {inserted} fill(s).")
    except Exception as e:
        logger.error(f"Broker reconciliation failed: {e}")
        log_exception_to_jira(e, "AMD Sideload Broker Reconciliation Failure")

    # 8. Sync to GCS if configured (so the cloud DB stays fresh for blog/dashboard).
    # RACE-CONDITION FIX (2026-09-18): the normal lane and this sideload lane both
    # upload the WHOLE trading_agent.db to the same GCS blob, and the last upload
    # wins. When the normal lane uploads AFTER this lane, it clobbers this lane's
    # AMD decision — so AMD "disappears" from the dashboard a few minutes after
    # appearing. To prevent that, re-download the freshest GCS DB right before
    # uploading and re-insert this lane's AMD decision into it, so this upload
    # carries BOTH the latest normal-lane decisions AND the AMD decision.
    try:
        from core.gcs_sync import download_from_gcs
        download_from_gcs()
        # Re-insert this cycle's AMD decision into the freshly-downloaded DB so
        # it survives the upload (the download may have overwritten the local DB,
        # which is fine — we only need to persist the AMD decision).
        if amd_decision_payload:
            try:
                database.log_decision(**amd_decision_payload)
                logger.info("Re-inserted AMD decision into fresh GCS DB before upload.")
            except Exception as reinsert_err:
                logger.error(f"Failed to re-insert AMD decision before upload: {reinsert_err}")
    except Exception as dl_err:
        logger.warning(f"Could not re-download GCS DB before sideload upload: {dl_err}")

    try:
        from core.gcs_sync import upload_to_gcs
        upload_to_gcs()
    except Exception as e:
        logger.error(f"GCS sync failed: {e}")
        log_exception_to_jira(e, "AMD Sideload GCS Sync Failure")

    return {
        "status": "ok",
        "symbol": symbol,
        "cycle_id": cycle_id,
        "executed": executed_results,
        "equity": equity,
        "cash": cash,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AMD sideloaded trading cycle")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions but place no orders")
    args = parser.parse_args()

    logger_setup.setup_logging(app_name="agent-trade-sideload", env="production")
    setup_jira_logging(app_name="agent-trade-sideload")
    logger.info(f"Starting AMD sideload lane (symbol={sl_cfg.SL_SYMBOL}, dry_run={args.dry_run})")

    alpaca_client = AlpacaClient()
    data_provider = DataProvider(alpaca_client)
    brain = TradingBrain()
    guardrails = RiskGuardrails()

    if args.loop:
        interval_min = float(getattr(config, "TRADING_INTERVAL_MINUTES", 15))
        logger.info(f"Entering loop mode (interval={interval_min} min). Ctrl+C to stop.")
        while True:
            try:
                run_single_cycle(alpaca_client, data_provider, brain, guardrails, dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                log_exception_to_jira(e, "AMD Sideload Cycle Failure")
            time.sleep(interval_min * 60)
    else:
        try:
            result = run_single_cycle(alpaca_client, data_provider, brain, guardrails, dry_run=args.dry_run)
            logger.info(f"Cycle result: {result}")
        except Exception as e:
            logger.critical(f"Cycle failed: {e}")
            log_exception_to_jira(e, "AMD Sideload Cycle Failure")
            raise


if __name__ == "__main__":
    main()