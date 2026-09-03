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