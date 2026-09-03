# Options Risk Controls — Event Gate, Greeks Exposure Caps, EOD-Flat

**Date:** 2026-09-03
**Status:** Implemented.
**Module:** `core/option_risk.py`, integrated via `core/option_lifecycle.py::risk_sweep`
and `runner.py` (step 1c, before the brain appraises the options book).

## Why

Options are leveraged, time-sensitive instruments. A **30-60 DTE** contract's
risk profile is dominated by *vega* (overnight IV moves) and *delta* (directional
gaps), not just by theta (which is small at 30-60 DTE). The danger is holding a
high-vega/high-delta book *through* an overnight event (earnings, FOMC, macro)
where an IV crush or a pre-market gap can wipe out premium even though the
underlying *thesis* is intact.

These controls are the deterministic, LLM-independent counterpart to the softer
LLM brain loop — they **only ever close (SELL-to-close)** and **never open risk**.
They run *every* cycle before the brain appraises positions, so nothing can hold
through a high-risk event or over-concentrated overnight exposure without being
flattened first.

## The three controls

### 1. Event Gate (`OPTIONS_EVENT_GATE_ENABLED`, default on)

Flattens a held option when its underlying has a **high-impact scheduled event**
at or before the option's expiration, during the session *before* the event
(T-1):

- **Earnings** (via `data_provider.get_earnings_dates` / yfinance calendar).
- **FOMC / macro** (`OPTIONS_EVENT_GATE_INCLUDE_FOMC`, default on) on the next
  FOMC decision day from a synthetic 2026 calendar.

This captures nearly all of the overnight-gap/IV-crush benefit of an end-of-day
flatten **without** killing the multi-week 30-60 DTE theta collection on normal
days. Fails **open** (does nothing) if the earnings data source is unavailable,
so a data outage can't false-trigger closures.

### 2. VEGA / DELTA Exposure Caps (`OPTIONS_VEGA_CAP_MV_PCT`, `OPTIONS_DELTA_CAP_PCT`)

Two independent dollar-based limits across the WHOLE options book, as a % of
equity:

- **Vega cap:** aggregate `|vega|` dollars (price change per +1 vol point) must
  not exceed `equity * OPTIONS_VEGA_CAP_MV_PCT` (default **2%**).
- **Delta cap:** aggregate `|delta|` dollars (directional change per +1 point in
  the underlying) must not exceed `equity * OPTIONS_DELTA_CAP_PCT` (default
  **15%**).

Greeks are read from the latest option quote. When greeks are **unavailable**
(e.g. paper-skipped option bars), the position is appraised by **premium market
value** as a fallback dollar bound, so the cap still enforces a hard limit.

### 3. End-Of-Day Flat (`OPTIONS_EOD_FLAT`, default OFF)

The "flatten the whole options book at the end of each session" arm. **Off by
default** because it:
- fights the 30-60 DTE multi-week swing thesis,
- compounds bid/ask spread cost on a daily round-trip, and
- dilutes the strategist's *separate* options-learning signal (PnL gets dominated
  by spread + overnight noise, not directional/vega skill).

When enabled, it flattens every open option position in the final intraday cycle
before the 4pm ET close.

## Config / envs

| Env | Default | Meaning |
|---|---|---|
| `OPTIONS_EVENT_GATE_ENABLED` | `true` | Master switch for the event gate (earnings + FOMC). |
| `OPTIONS_EVENT_GATE_INCLUDE_FOMC` | `true` | Include synthetic FOMC decision days in the gate. |
| `OPTIONS_VEGA_CAP_MV_PCT` | `0.02` | Max aggregate `|vega|` exposure as % of equity (0 = off). |
| `OPTIONS_DELTA_CAP_PCT` | `0.15` | Max aggregate `|delta|` exposure as % of equity (0 = off). |
| `OPTIONS_EOD_FLAT` | `false` | Flatten the whole options book at end of session. |

> These keys are whitelisted in `deploy/deploy_cloud.ps1` so they deploy via the
> env-string step (`.env` is gitignored).

## Runner wiring

In `runner._run_trading_cycle_impl`, after the pre-expiry auto-close sweep
(step 1b) a new step **1c** runs `OptionLifecycle.risk_sweep(account_state)` which
calls `option_risk.rebalance_book` + `eod_flat_decisions` and closes any flagged
positions (logging each as `option_risk`).

## Tests

- `tests/test_option_risk.py` — event gate (no-event, earnings-imminent, FOMC,
  disabled) + exposure caps (vega, delta, premium fallback).
- `tests/test_options.py::TestsOptionLifecycleRiskSweep` — end-to-end:
  earnings-imminent triggers a real `risk_sweep` close.