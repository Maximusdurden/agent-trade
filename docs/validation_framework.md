# Validation Framework

**Date:** 2026-09-10
**Project:** `agent-trade`
**Related:** TMCL-747 (Validation Framework epic), TMCL-767 (pre/post-change snapshot comparison)

This document describes the validation framework added to `agent-trade`, which
implements the **Testing & Validation Protocols** from `sprint_plan.md`:

1. Static code analysis (ruff/flake8).
2. Mock-mode dry-runs of the runner.
3. Unit regression suite (pytest).
4. Audit DB integrity.

---

## Tools

### `tools/validate_db.py` — DB Integrity Validation

Validates the trading SQLite database for structural and data-integrity issues.

```bash
python tools/validate_db.py trading_agent.db
```

Checks performed:
- All expected tables exist with the expected columns.
- No orphaned rows (trades/executions referencing missing decisions).
- Trades have valid `side`, `qty`, `status`, and unique `alpaca_order_id`.
- Decisions have valid `proposed_action` and `is_approved`.
- `proposed_action` has no internal-whitespace anomalies (e.g. `HOL D`).
- `watchlist_history` rows contain valid JSON arrays.
- `strategy_snapshots` have no duplicate `(snapshot_date, ticker)`.
- `system_state` has no NULL keys/values.

Read-only: never mutates the database. Exits non-zero if any check fails.

### `tools/snapshot_compare.py` — Pre/Post-Change Snapshot Comparison (TMCL-767)

Captures a snapshot of key trading state before a change, then compares it
against a snapshot taken after the change to detect unexpected drift.

```bash
# Capture a "before" snapshot
python tools/snapshot_compare.py capture before.json

# ... make your change / deploy ...

# Capture an "after" snapshot and compare
python tools/snapshot_compare.py capture after.json
python tools/snapshot_compare.py compare before.json after.json
```

Snapshot captures (read-only):
- Per-table row counts.
- Latest timestamp per time-stamped table.
- Latest watchlist.
- Latest `system_state` values.
- Latest strategy snapshot per ticker.
- Latest decision per ticker.
- Latest trade per symbol.

### `tools/run_validation.py` — Validation Runner

Ties together the full validation protocol.

```bash
python tools/run_validation.py                 # DB integrity only
python tools/run_validation.py --tests         # + pytest suite
python tools/run_validation.py --lint          # + ruff/flake8
python tools/run_validation.py --dry-run       # + mock runner dry-run
python tools/run_validation.py --all           # everything
```

---

## Related Fix: Brain Action Normalization

The validation framework surfaced a real data anomaly: decision id 813 had
`proposed_action = 'HOL D'` (the LLM emitted a space inside the action token).
`core/trading_brain.py` `_normalize_decision()` now strips whitespace and coerces
any malformed action to `HOLD`, so the malformed value never reaches the DB.
A regression test was added in `tests/test_per_ticker_decisions.py`.