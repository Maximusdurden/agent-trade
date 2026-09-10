# Guardrail & Brain Fixes — 2026-09-10

**Date:** 2026-09-10
**Project:** `agent-trade`
**Related:** AMGN/HD trading incidents, TMCL-916/917/918/919

This document records the diagnosis and fixes for a cluster of related issues
surfaced on 2026-09-10 while analyzing AMGN and HD trading behavior.

---

## 1. Option-Intent Buy Rejection (Fix A)

### Symptom
The AI brain wanted a **long PUT** (bearish) for a symbol the strategist had
authorized for options, but the symbol was **not in `OPTIONS_UNIVERSE`**. The
guardrail silently downgraded the `instrument: "option"` intent to a **stock BUY**,
producing an unintended long position in a stock the brain believed was falling
(the AMGN "wanted a put, got stock" mismatch).

### Fix
`core/guardrails.py` now **rejects** an option-intent BUY outright when the symbol
is not options-eligible (not in `OPTIONS_UNIVERSE`, options disabled, conviction
below threshold, or neutral direction), instead of silently buying stock. A helper
`_option_intent_rejection_reason` produces a specific, human-readable rejection
reason for the decision stream.

---

## 2. Strategist Only Authorizes Options for Universe Members (Fix C)

### Symptom
The strategist emitted `instrument_hint: "option"` for symbols not in
`OPTIONS_UNIVERSE` (e.g. AMGN), producing a misleading rule the brain followed.

### Fix
`core/strategist.py` now downgrades `instrument_hint: "option"` to `None` (no
authorization) when the ticker is not in `OPTIONS_UNIVERSE`, with a warning.

---

## 3. Whole-Share Floor for Equity Buys (Fix F)

### Symptom
The brain proposed fractional-share equity BUYs (e.g. HD qty `0.00065`, AMGN qty
`0.00012`). Alpaca rejects fractional-share bracket (OCO) orders for equities with
`cost basis must be >= minimal amount of order 1`. This caused the TMCL-916/917/918/919
errors.

### Fix
`core/guardrails.py` now floors equity BUYs to whole shares before execution. If the
floored qty < 1, the buy is rejected with a clear message. Crypto keeps fractional
quantities (Alpaca supports fractional crypto).

---

## 4. VWAP-None Fallback in Brain Prompt

### Symptom
When VWAP fields were `None` (gated early in the session before `MIN_VWAP_BARS`
bars accumulate), the brain interpreted the VWAP dead-zone rule as "cannot trade
without VWAP" and HOLDed **every** ticker.

### Fix
`core/trading_brain.py` prompt now tells the brain: when VWAP fields are `None`,
the VWAP dead zone does **not** apply — make the decision using the other
indicators, and let the deterministic guardrail handle whipsaw protection. Missing
VWAP must never block a valid trade.

---

## 5. Brain Action Normalization

### Symptom
The validation framework surfaced decision id 813 with `proposed_action = 'HOL D'`
(the LLM emitted a space inside the action token).

### Fix
`core/trading_brain.py` `_normalize_decision()` now strips whitespace and coerces
any malformed action to `HOLD`, so the malformed value never reaches the DB.

---

## Verification

- `tests/test_instrument_routing.py` — updated low-conviction test + new
  `test_option_intent_symbol_not_in_universe_rejected`.
- `tests/test_per_ticker_decisions.py` — new `test_normalize_decision_strips_whitespace_action`.
- All relevant test suites pass (30+ tests).