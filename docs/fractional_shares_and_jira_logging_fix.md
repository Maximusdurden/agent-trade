# Fix: Fractional Share Recommendations & Jira Error Logging

**Date:** 2026-08-07
**Project:** `agent-trade` (dashboard.agenttrade.us)
**Related:** `agent-jira-client` (Jira error logging)

This document records the diagnosis and fixes for two issues surfaced in the
System Activity Logs on the dashboard:

1. The agent recommending **fractional shares for stocks** (equities).
2. Errors **not being logged to Jira** despite the `agent-jira-client` integration.

---

## Part 1 — Fractional Shares for Stocks

### Symptom
The System Activity Logs show the agent recommending fractional share quantities
(e.g. `0.5`, `1.7`) for equities. Alpaca does not support fractional quantities for
bracket (OCO) orders on equities, and only supports fractional quantities for
*eligible* equities on plain market orders. This caused order rejections / fallbacks.

### Root Cause
- The LLM proposes fractional quantities (`TradingDecision.quantity: float` in
  `core/trading_brain.py`).
- `core/guardrails.py` `validate_and_adjust_decision()` **forced whole shares** for
  equities via `int(max_trade_value // current_price)` in three scaling paths
  (max-trade allocation, per-ticker allocation, cash buffer). Crypto used
  `round(..., 4)`.
- `core/alpaca_client.py` `execute_market_order()` rounded bracket (OCO) orders to
  whole shares via `int(qty)` and fell back to a plain market order if `< 1`. Plain
  market orders passed fractional qty through with no fallback if Alpaca rejected it.

### Fix
1. **`core/guardrails.py`** — All three equity scaling paths now preserve fractional
   quantities (`round(max_value / current_price, 4)`), matching crypto behavior. This
   keeps the agent's position sizing intact.
2. **`core/alpaca_client.py`** — Bracket (OCO) orders still round to whole shares
   (Alpaca requirement). Plain market equity orders now have a **fallback**: if Alpaca
   rejects a fractional qty, the order is retried with whole shares.

### Behavior After Fix
| Order type | Crypto | Equity |
|---|---|---|
| Bracket (OCO) | fractional preserved | rounded to whole shares |
| Plain market | fractional preserved | fractional, falls back to whole shares if rejected |

---

## Part 2 — Jira Error Logging

### Symptom
Errors logged by `agent-trade` (ERROR/CRITICAL) were not appearing as Jira tickets in
project `TMCL`, despite the `agent-jira-client` integration being wired in.

### Root Cause
The integration chain is:
`runner.py:95` → `core/logger_setup.py:setup_logging()` → registers
`JiraLoggingHandler` on the root logger → `agent_jira.jira_logger.JiraLogger.log_error()`.

Four distinct defects prevented errors from reaching Jira:

1. **Cloud Run missing credentials (biggest).**
   `deploy/deploy_cloud.ps1` `AllowedRuntimeKeys` included `JIRA_URL` and
   `JIRA_PROJECT_KEY` but **not** `JIRA_EMAIL` or `JIRA_API_TOKEN`. The Cloud Run job
   therefore had empty credentials → no `Authorization` header → Jira returned 401 →
   silent failure.

2. **Stale `default_logger` singleton.**
   `core/logger_setup.py` imported `default_logger` **by value** at module load.
   `setup_logger()` reassigns `jira_logger.default_logger` to a configured instance,
   but `logger_setup.default_logger` still pointed to the original unconfigured
   `JiraLogger()`. `JiraLoggingHandler.emit()` used the stale instance, which reads
   env vars `JIRA_USER_EMAIL`/`JIRA_SITE_URL` (empty in agent-trade, which uses
   `JIRA_EMAIL`/`JIRA_URL`) → no auth → silent failure.

3. **Env-var name mismatch.**
   `agent-trade/.env` uses `JIRA_EMAIL`/`JIRA_URL`; `jira_logger.py` defaults read
   `JIRA_USER_EMAIL`/`JIRA_SITE_URL`. The explicit `setup_logger(...)` call passes the
   correct names, but any path relying on module defaults (e.g. the stale singleton)
   found empty values.

4. **Silent failure swallowing.**
   `jira_logger._make_request` caught all HTTP errors and returned `None`; `log_error`
   returned `None` without raising. Failures only printed to stderr, which is rarely
   monitored in a Scheduled Task or Cloud Run job.

### ⚠️ Critical Finding (verified 2026-08-07)
After applying the code fixes, an end-to-end verification (`verify_jira_logging.py`)
revealed the **actual blocker**: the Jira **API token is invalid/expired**.

- `GET /rest/api/2/myself` with the token from both `.env` files returns
  `401 Client must be authenticated to access this resource.`
- The token is identical in `agent-trade/.env` and `agent-jira-client/.env`.
- The Atlassian MCP works because it uses **OAuth**, not the API token — so the
  project `TMCL` is reachable, but the API-token client cannot authenticate.

**This is why errors were never logged:** every request returned 401, and the old
code silently swallowed it. The code fixes above are necessary but not sufficient —
**a new Jira API token must be generated** and placed in both `.env` files.

**Action required (manual, cannot be automated):**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Generate a new API token.
3. Update `JIRA_API_TOKEN` in BOTH:
   - `z:\python\projects\agent-trade\.env`
   - `z:\python\projects\agent-jira-client\.env`
4. Redeploy Cloud Run (so the new token is injected via `AllowedRuntimeKeys`).
5. Re-run `verify_jira_logging.py` to confirm a ticket is created in `TMCL`.

### ✅ Resolution (verified 2026-08-07)
A new API token was generated and placed in both `.env` files. Verification confirmed:

- `GET /rest/api/2/myself` with the new token returns **AUTH OK** (user: Derrick Swymer, `dswymer@gmail.com`).
- `verify_jira_logging.py` created ticket **TMCL-775** in project `TMCL` (status: To Do, type: Bug).
- The ticket contains the correct summary, environment metadata, traceback, and dedup fingerprint.

The Jira error-logging path is now **fully functional end-to-end**.

### ✅ Cloud Run Redeployed (verified 2026-08-07)
`deploy/deploy_cloud.ps1` was run successfully:
- Image built & pushed: `gcr.io/agenttrade-us/agent-trade:20260807-175050` (Cloud Build SUCCESS).
- Cloud Run job `agent-trade-job` updated.
- Cloud Scheduler `agent-trade-scheduler` recreated (every 15 min, NY time).
- Verified the deployed job env now includes `JIRA_EMAIL=dswymer@gmail.com` and the **new** `JIRA_API_TOKEN`, plus `JIRA_URL` and `JIRA_PROJECT_KEY=TMCL`.

Both the local Scheduled Task and Cloud Run paths now have valid Jira credentials.

### Fix
1. **`deploy/deploy_cloud.ps1`** — Added `JIRA_EMAIL` and `JIRA_API_TOKEN` to
   `AllowedRuntimeKeys` so the Cloud Run job receives credentials.
2. **`core/logger_setup.py`** — Imported the module (`import agent_jira.jira_logger as
   jira_logger`) and reference `jira_logger.default_logger` **dynamically** in
   `emit()`, so the freshly-configured singleton is always used.
3. **`agent_jira/jira_logger.py`** — `log_error` now prints explicit `[JiraLogger]
   ERROR:` messages to stderr when a comment or ticket creation fails, instead of
   silently returning `None`.

### Verification
- **Local:** Run a script that logs an `ERROR`; confirm a ticket is created/updated in
  Jira (project `TMCL`).
- **Cloud:** After redeploying, confirm the Cloud Run job env includes
  `JIRA_EMAIL`/`JIRA_API_TOKEN`, then trigger an error and confirm a ticket appears.
- **No silent failures:** stderr should show clear `[JiraLogger]` messages on any
  failure.

---

## Files Changed
- `z:\python\projects\agent-trade\core\guardrails.py`
- `z:\python\projects\agent-trade\core\alpaca_client.py`
- `z:\python\projects\agent-trade\core\logger_setup.py`
- `z:\python\projects\agent-trade\deploy\deploy_cloud.ps1`
- `z:\python\projects\agent-jira-client\agent_jira\jira_logger.py`
