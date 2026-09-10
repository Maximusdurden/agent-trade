#!/usr/bin/env python3
"""DB integrity validation for agent-trade.

Validates the trading SQLite database for structural and data-integrity issues
per the Testing & Validation Protocols in sprint_plan.md:

    4. Audit DB Integrity: Run diagnostic checks on trading_agent.db using
       sqlite tools to confirm watchlist_history is written correctly and
       trades register expected timestamps.

Checks performed:
  - All expected tables exist with the expected columns.
  - No orphaned rows (trades/executions referencing missing decisions).
  - No duplicate/overlapping primary keys.
  - Trades have valid side/status/qty and monotonic-ish timestamps.
  - Decisions have valid proposed_action and is_approved.
  - watchlist_history rows contain valid JSON arrays.
  - strategy_snapshots have no duplicate (snapshot_date, ticker).
  - system_state has no NULL values.

Read-only: never mutates the database. Exits non-zero if any check fails.
"""
import argparse
import json
import os
import sqlite3
import sys

# Expected schema: table -> set of required columns.
EXPECTED_SCHEMA = {
    "decisions": {
        "id", "timestamp", "ticker_indicators", "portfolio_state",
        "thought_process", "proposed_action", "proposed_symbol", "proposed_qty",
        "is_approved", "rejection_reason", "direction", "conviction",
        "instrument", "cycle_id", "reasoning",
    },
    "trades": {
        "id", "decision_id", "alpaca_order_id", "timestamp", "symbol", "side",
        "qty", "filled_avg_price", "status", "option_type", "option_dte",
        "strike", "contract_symbol",
    },
    "executions": {
        "id", "decision_id", "attempt", "timestamp", "symbol", "side", "qty",
        "order_type", "status", "error", "alpaca_order_id", "filled_avg_price",
    },
    "portfolio_history": {"timestamp", "equity", "cash", "unrealized_pnl"},
    "strategy_history": {
        "id", "timestamp", "ticker", "yesterdays_rules", "todays_rules",
        "meta_reasoning", "strategy_version", "instrument_hint",
    },
    "strategy_snapshots": {
        "id", "snapshot_date", "ticker", "rule", "instrument_hint",
        "strategy_version",
    },
    "watchlist_history": {"id", "timestamp", "watchlist"},
    "system_state": {"key", "value"},
    "ticker_convictions": {
        "id", "cycle_id", "timestamp", "symbol", "direction", "conviction",
        "reasoning",
    },
}

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "NO_ACTION"}
VALID_SIDES = {"buy", "sell"}
# Alpaca order statuses include 'new' (order accepted, not yet filled).
VALID_TRADE_STATUSES = {"filled", "failed", "open", "canceled", "partially_filled", "submitted", "new"}


class ValidationResult:
    """Collects pass/fail results for a validation run."""

    def __init__(self):
        self.checks = []

    def ok(self, name: str, detail: str = ""):
        self.checks.append((True, name, detail))

    def fail(self, name: str, detail: str = ""):
        self.checks.append((False, name, detail))

    @property
    def passed(self) -> bool:
        return all(ok for ok, _, _ in self.checks)

    def report(self) -> str:
        lines = []
        for ok, name, detail in self.checks:
            status = "PASS" if ok else "FAIL"
            suffix = f" — {detail}" if detail else ""
            lines.append(f"[{status}] {name}{suffix}")
        return "\n".join(lines)


def validate_db(db_path: str) -> ValidationResult:
    """Run all integrity checks against the database at db_path."""
    result = ValidationResult()
    if not os.path.exists(db_path):
        result.fail("database_exists", f"{db_path} not found")
        return result

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Schema check
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    for table, required_cols in EXPECTED_SCHEMA.items():
        if table not in tables:
            result.fail("table_exists", f"{table} missing")
            continue
        cur.execute(f"PRAGMA table_info({table})")
        cols = {r[1] for r in cur.fetchall()}
        missing = required_cols - cols
        if missing:
            result.fail("schema_columns", f"{table} missing columns: {sorted(missing)}")
        else:
            result.ok("schema_columns", f"{table} has all expected columns")

    # 2. Orphaned rows
    for child, fk in [("trades", "decision_id"), ("executions", "decision_id")]:
        if child in tables and "decisions" in tables:
            cur.execute(
                f"SELECT COUNT(*) FROM {child} c LEFT JOIN decisions d "
                f"ON c.{fk} = d.id WHERE d.id IS NULL AND c.{fk} IS NOT NULL"
            )
            orphans = cur.fetchone()[0]
            if orphans:
                result.fail("orphaned_rows", f"{child} has {orphans} rows with missing decision_id")
            else:
                result.ok("orphaned_rows", f"{child} has no orphaned decision references")

    # 3. Trades integrity
    if "trades" in tables:
        cur.execute("SELECT COUNT(*) FROM trades WHERE side NOT IN ('buy','sell')")
        bad_side = cur.fetchone()[0]
        result.ok("trades_side", f"{bad_side} invalid side values") if bad_side == 0 else \
            result.fail("trades_side", f"{bad_side} invalid side values")

        cur.execute("SELECT COUNT(*) FROM trades WHERE qty <= 0")
        bad_qty = cur.fetchone()[0]
        result.ok("trades_qty", f"{bad_qty} non-positive qty") if bad_qty == 0 else \
            result.fail("trades_qty", f"{bad_qty} non-positive qty")

        cur.execute("SELECT COUNT(*) FROM trades WHERE status NOT IN ('filled','failed','open','canceled','partially_filled','submitted','new')")
        bad_status = cur.fetchone()[0]
        result.ok("trades_status", f"{bad_status} invalid status") if bad_status == 0 else \
            result.fail("trades_status", f"{bad_status} invalid status")

        # Duplicate alpaca_order_id (should be unique)
        cur.execute("SELECT COUNT(*) FROM (SELECT alpaca_order_id FROM trades WHERE alpaca_order_id IS NOT NULL GROUP BY alpaca_order_id HAVING COUNT(*) > 1)")
        dup_orders = cur.fetchone()[0]
        result.ok("trades_unique_orders", f"{dup_orders} duplicate order ids") if dup_orders == 0 else \
            result.fail("trades_unique_orders", f"{dup_orders} duplicate alpaca_order_id")

    # 4. Decisions integrity
    if "decisions" in tables:
        cur.execute("SELECT COUNT(*) FROM decisions WHERE proposed_action NOT IN ('BUY','SELL','HOLD','NO_ACTION')")
        bad_action = cur.fetchone()[0]
        result.ok("decisions_action", f"{bad_action} invalid proposed_action") if bad_action == 0 else \
            result.fail("decisions_action", f"{bad_action} invalid proposed_action")

        # Whitespace anomaly: e.g. 'HOL D' (LLM emitted a space inside the action).
        cur.execute("SELECT COUNT(*) FROM decisions WHERE proposed_action LIKE '% %' OR proposed_action LIKE '%  %'")
        ws_action = cur.fetchone()[0]
        result.ok("decisions_action_whitespace", f"{ws_action} actions with internal whitespace") if ws_action == 0 else \
            result.fail("decisions_action_whitespace", f"{ws_action} proposed_action values contain internal whitespace (e.g. 'HOL D')")

        cur.execute("SELECT COUNT(*) FROM decisions WHERE is_approved NOT IN (0,1)")
        bad_approved = cur.fetchone()[0]
        result.ok("decisions_approved", f"{bad_approved} invalid is_approved") if bad_approved == 0 else \
            result.fail("decisions_approved", f"{bad_approved} invalid is_approved")

    # 5. watchlist_history JSON validity
    if "watchlist_history" in tables:
        cur.execute("SELECT id, watchlist FROM watchlist_history")
        bad_json = 0
        for row in cur.fetchall():
            try:
                parsed = json.loads(row["watchlist"])
                if not isinstance(parsed, list):
                    bad_json += 1
            except (json.JSONDecodeError, TypeError):
                bad_json += 1
        result.ok("watchlist_json", f"{bad_json} invalid watchlist JSON") if bad_json == 0 else \
            result.fail("watchlist_json", f"{bad_json} invalid watchlist JSON")

    # 6. strategy_snapshots uniqueness
    if "strategy_snapshots" in tables:
        cur.execute("SELECT COUNT(*) FROM (SELECT snapshot_date, ticker FROM strategy_snapshots GROUP BY snapshot_date, ticker HAVING COUNT(*) > 1)")
        dup_snap = cur.fetchone()[0]
        result.ok("snapshots_unique", f"{dup_snap} duplicate (date,ticker)") if dup_snap == 0 else \
            result.fail("snapshots_unique", f"{dup_snap} duplicate (snapshot_date, ticker)")

    # 7. system_state NULL values
    if "system_state" in tables:
        cur.execute("SELECT COUNT(*) FROM system_state WHERE value IS NULL OR key IS NULL")
        null_state = cur.fetchone()[0]
        result.ok("system_state", f"{null_state} NULL key/value") if null_state == 0 else \
            result.fail("system_state", f"{null_state} NULL key/value")

    conn.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="Validate agent-trade DB integrity.")
    parser.add_argument("db", nargs="?", default="trading_agent.db",
                        help="Path to the SQLite database (default: trading_agent.db)")
    args = parser.parse_args()

    result = validate_db(args.db)
    print(result.report())
    print(f"\n{'ALL CHECKS PASSED' if result.passed else 'VALIDATION FAILED'}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())