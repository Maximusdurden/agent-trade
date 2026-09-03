# Options DTE Widening & Wider-Window Fallback

**Date:** 2026-09-03
**Status:** Implemented & deployed.
**Resolves:** TMCL-889 (recurring "No suitable CALL option found for NVDA within DTE 30-45")

## Why
The agent repeatedly emitted `No suitable CALL option found for NVDA within DTE
30-45` (and similar for other underlyings). NVDA trades **weekly** expirations, so
on a rollover day the chain of valid, liquid contracts could fall just outside a
narrow 30-45 DTE window. Because `find_best_option()` returned `None`, the option
BUY was cancelled and an error ticket was created every cycle — even when a
perfectly good contract existed at DTE ~50-60.

dexter-trader's mature picker uses a wider default window **30-60** (see
`Z:\python\projects\dexter-trader\utilities\option_picker.py`), which is far less
prone to these gaps. This change brings agent-trade's options DTE handling in line
with that robustness.

## Current open item: OPRA (option quotes/bars) agreement

Option **historical bars** (the `_fetch_option_data` path) require the **OPRA**
(Options Price Reporting Authority) agreement to be **signed on the Alpaca account**.
If it is not signed, Alpaca rejects option-bar requests with
`{"message":"OPRA agreement is not signed"}`.

**Important:** this is running on a **paper** account, where the OPRA agreement
cannot be signed (it is a live-account PDF agreement; there is no in-app e-sign for
it on paper). Therefore option *historical bars* are **unavailable on paper, by
design**, and `_fetch_option_data` now **skips them gracefully** on paper accounts —
logging a one-time info notice instead of an error every cycle. Underlying-level
**stock** bars are still used for analysis, and option contract selection
(`find_best_option` / chain snapshots / latest quotes) still works on paper.

This means the OPRA-based error tickets should **stop** once the paper-skip is
deployed. If the account is later upgraded to **live**, sign the OPRA agreement
there to enable per-contract option bars.

## Appraisal-universe note (OCC exclusion)

Held OCC option contracts (e.g. `NVDA261016C00230000`) are **excluded** from the
runner's appraisal universe (`build_appraisal_universe` in `runner.py`) so the
brain loop appraises the **underlying** (NVDA) rather than trying to fetch stock
bars for a contract. This prevents both `invalid symbol` (old bug) and the OPRA
error (new, when it fires) from spamming error tickets.

## What changed

### 1. Widened the default DTE window (`core/config.py`)
```python
OPTIONS_DTE_MIN = int(os.getenv("OPTIONS_DTE_MIN", "30"))
OPTIONS_DTE_MAX = int(os.getenv("OPTIONS_DTE_MAX", "60"))   # was 45
```
The default primary window is now **30-60 DTE** (was 30-45), mirroring dexter.

### 2. Wider-window fallback (`core/option_executor.py`)
Added a new fallback in `_resolve_contract()`. If `find_best_option()` returns `None`
in the primary window AND the primary `dte_max` is below the fallback ceiling, it
retries once with the window widened up to `OPTIONS_DTE_FALLBACK_MAX` (default **90**,
the existing hard bound):
```python
OPTIONS_DTE_FALLBACK_MAX = int(os.getenv("OPTIONS_DTE_FALLBACK_MAX", "90"))
```
This ensures a transient weekly-clustering gap can't cancel a valid, high-conviction
option BUY.

### 3. New env var
- `OPTIONS_DTE_FALLBACK_MAX` (default `90`) — upper bound for the widened retry.

## Deployment note (`deploy/deploy_cloud.ps1`)
If you want `OPTIONS_DTE_FALLBACK_MAX` (or the widened defaults) to be configurable
at runtime, add it to the `$AllowedRuntimeKeys` whitelist in the deploy script. The
code defaults (`30`/`60`) work without it.

> **⚠️ Env overrides code (TMCL-894 root cause).** The production `.env` previously
> set `OPTIONS_DTE_MAX=45`, which **overrode** the widened 60 code default — so the
> deployed image still opened with a 30-45 window and re-emitted the 
> "No suitable CALL within DTE 30-45" error. When the code default is changed, you
> **must** also update the deployed `.env` (`OPTIONS_DTE_MAX=60`,
> `OPTIONS_DTE_FALLBACK_MAX=90`) or the running job will keep the old window. The
> error message always shows the *original* `dte_max` even after the fallback runs,
> so "30-45" in the message does **not** prove the fallback didn't happen.

## Related: options-aware strategy learning
This DTE widening complements the dedicated OPTIONS strategy track (see commit
`279a45b`), where the strategist tunes option-specific knobs (conviction threshold,
DTE, OTM%, allocation, max contracts) on a **separate curve** from stocks, stored
under the `OPTIONS/<UNDERLYING>` rule key. Together they both (a) reduce false
"no option found" failures and (b) let the agent learn from leveraged option PnL.

## Tests
`tests/test_options.py::TestsOptionExecutorWideWindowFallback`:
- `test_fallback_retries_with_wider_window` — confirms a primary miss triggers a
  widened fallback up to the hard max.
- `test_no_fallback_when_primary_succeeds` — confirms no unnecessary retry.

## Related: options risk controls (event gate + greeks caps)
Weighing whether to flatten at end-of-day exposed that the real overnight risk at
30-60 DTE is **vega/delta** (IV crush + gaps), not theta. Rather than an
unconditional EOD-flat, we implemented a **conditional** risk layer — see
`docs/options_risk_controls.md`: the event gate flattens before earnings/FOMC, and
vega/delta exposure caps bound the book's overnight greeks. `OPTIONS_EOD_FLAT` is
available but off by default (it fights the 30-60 DTE thesis and dilutes the
options-learning signal).

---

# Intraday Options Watch (stays locked onto open option positions)

**Added:** 2026-09-03.

## Why
Options are **"time bombs"** — theta/IV/DTE decay means risk changes *intraday*,
not just day-to-day. The original design only ran the options strategy track **once
after market close** (16:30 ET), so even a large unrealized PnL on a held option
wouldn't re-tune the strategy until the next day. That meant the strategist wasn't
"locked onto" the leveraged position when it mattered.

## What changed (`core/runner.py`)
Added `maybe_run_intraday_options_watch(alpaca_client, positions)`, called every
trading cycle. Behavior:
- **If no option position is held** → no-op (returns `False`).
- **While any option is held** → re-runs `MetaStrategist().run_option_strategy_refinement()`
  for the held underlyings every `OPTIONS_WATCH_COOLDOWN_MINUTES` (default **15**,
  matched to the 15-min trading loop).
- Cooldown is enforced via the DB `system_state` key `last_options_intraday_watch`
  (UTC), so a re-tune fires at most once per 15 minutes while options are open —
  never more often, and it's idempotent/harmless if it fails.

## New env var
- `OPTIONS_WATCH_COOLDOWN_MINUTES` (default `15`) — intraday options-watch cooldown
  while holding options. Added to the deploy whitelist.

## Behavior summary
- **Every loop:** brain watches PnL/positions (execution).
- **While holding an option (every ~30m):** strategist re-tunes option strategy —
  so it reacts to a sizable PnL / decaying DTE *as it happens*, not the next day.
- **Once per day after close:** full options strategy refresh (unchanged).

## Tests
`tests/test_runner_heartbeat.py::TestIntradayOptionsWatch`:
- no tune when no option held;
- tunes when an option is held and cooldown elapsed;
- skips when within the cooldown window.