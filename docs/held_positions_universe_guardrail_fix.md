# Fix: Held Positions Rejected by Universe Guardrail (KO Scenario)

**Date:** 2026-08-07
**Project:** `agent-trade` (dashboard.agenttrade.us)

## Symptom
The System Activity Logs showed the agent recommending a trade for **KO** (Coca-Cola)
that was **rejected** with:

```
Rejected: Symbol 'KO' is not in the allowed trading universe (...) nor in the latest watchlist (['XOM', 'MCD', 'CSCO', 'GE', 'HON']).
```

This was confusing because KO was clearly being traded successfully in other cycles.

## Root Cause
A **race/consistency bug** between the runner's in-memory appraisal universe and the
guardrail's universe check.

1. The runner's `build_appraisal_universe()` builds the universe from
   `screened_symbols + positions.keys()`. This means **currently-held positions are
   always appraised**, even if they are not in the static `TRADING_UNIVERSE` or the
   latest top-N watchlist.
2. The guardrail's `validate_and_adjust_decision()` universe check only allowed symbols
   in `config.TRADING_UNIVERSE` **or** the latest row of `watchlist_history`
   (`get_latest_watchlist_raw()`). It did **not** consider held positions.

In the KO case:
- KO was a **held position** (`Active Positions: ['KO', 'MS', 'SOL/USD']`).
- KO was **not** in `TRADING_UNIVERSE` and had fallen out of the latest top-5 watchlist
  (`['XOM', 'MCD', 'CSCO', 'GE', 'HON']`).
- The runner appraised KO (because it's held), the brain recommended `BUY 8.5 KO`,
  but the guardrail rejected it because KO wasn't in the universe or watchlist.

This is a genuine bug: a symbol you already hold should always be tradable (for
position management / SELL, and for adding to the position / BUY).

## Fix
`core/guardrails.py` — the universe check now also adds **currently-held positions**
(both raw and normalized forms) to the allowed set:

```python
# Add currently-held positions (both raw and normalized forms)
for pos_symbol in current_positions:
    allowed_symbols.add(pos_symbol.upper())
    allowed_symbols.add(pos_symbol.upper().replace('/', ''))
```

## Verification
`verify_ko_fix.py` confirms:
- **BUY KO (held position):** Approved ✅ (was rejected before)
- **SELL KO (held position):** Approved ✅
- **BUY ZZZZ (not held, not in universe):** Still correctly rejected ✅

## Files Changed
- `z:\python\projects\agent-trade\core\guardrails.py`
- `z:\python\projects\agent-trade\verify_ko_fix.py` (verification script)

## Note
This fix is in the source code but has **not yet been redeployed to Cloud Run**. To
make it live in the cloud, re-run `deploy/deploy_cloud.ps1`.

## ✅ Deployed to Cloud Run (verified 2026-08-07)
`deploy/deploy_cloud.ps1` was re-run successfully:
- Image built & pushed: `gcr.io/agenttrade-us/agent-trade:20260807-200716` (Cloud Build SUCCESS).
- Cloud Run job `agent-trade-job` updated to the new image.
- Cloud Scheduler `agent-trade-scheduler` recreated (every 15 min, NY time).
- Verified the deployed job is running the new image and JIRA env vars remain intact.

The KO guardrail fix is now **live in production**.
