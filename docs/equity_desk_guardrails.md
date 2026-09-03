# Equity-Desk Loss Guardrails

**Date:** 2026-09-02
**Project:** `agent-trade`
**Status:** Implemented, tested, and deployed to production (`gcr.io/agenttrade-us/agent-trade:20260902-200746`)

## Why these exist

A forensic analysis of realized equity round-trips (measured 2026-07-07 → 2026-08-31) found
the equity desk was net negative for three separable reasons. Each is a distinct hole in the
decision/guardrail pipeline:

| Root cause | Evidence | PnL impact |
|---|---|---|
| **Fallback-universe buying** — the fallback/static-`TRADING_UNIVERSE` path traded names the screener *never* endorsed (SPY, QQQ, AMD, INTC, TSLA never appeared in **any** of 3,911 watchlist snapshots, yet still got bought and bled) | 58 round-trips, **8.6% win rate** vs 41.2% for watched names | **-$540** |
| **Averaging down** — a "buy-the-dip" rule kept **adding** to a held MS position as it sagged, then capitulated at the monthly low | MS approved 18 BUYs on 8/06 alone, held 2+ weeks, dumped at low | **-$226** |
| **Re-entering chronic losers** — KO (0% win) and MS (17% win) never strung 3 *consecutive* losses, so the old consecutive-loss breaker never fired | 29–32 round-trips each at very low win rate | **-$128 / -$226** |

## The guardrails

All live in `core/guardrails.py` (`RiskGuardrails.validate_and_adjust_decision`), gated by
env-tunable settings in `core/config.py`. They check **BUY** decisions; SELL (de-risk) is never blocked.

### 1. Strict-Universe Guardrail

`STRICT_UNIVERSE_ENABLED` (default `true`)

A **new** BUY is blocked unless the symbol is:
- in the latest screener watchlist, **or**
- crypto (24/7, and the profitable book), **or**
- currently held (so positions can always be managed/top-upped).

This closes the fallback-universe hole: the static `TRADING_UNIVERSE` can no longer auto-approve
buying a name the screener rejected. It does **not** block SELLs of held positions.

### 2. Anti-Scale-In Guardrail

Blocks **adding** to a held position when the current price is **below** its average entry
price (i.e., averaging down into a falling knife). Crypto is exempt (it scales differently
and is profitable). SELLs are never blocked.

**Screener-endorsed exemption:** a symbol that is in the *latest* screener watchlist is
actively endorsed (see Strict-Universe guardrail) and is therefore allowed to be added even
when slightly below entry. The anti-scale-in guardrail only blocks averaging-down into held
names the screener is **not** currently endorsing — the actual MS "buy-the-dip" failure mode.
This lets a high-conviction (e.g. 70%+) add to a currently-watchlisted name like a held PG go
through instead of being wrongly rejected.

### 3. Low Win-Rate Circuit Breaker

`MIN_LOW_WIN_RATE_TRADES` (default `5`) · `MAX_LOW_WIN_RATE` (default `0.25`)

If a symbol has at least `MIN_LOW_WIN_RATE_TRADES` closed round-trips in the circuit-breaker
lookback window **and** its realized win rate is below `MAX_LOW_WIN_RATE`, new BUYs are blocked —
even if it never strings the `MAX_CONSECUTIVE_LOSSES` consecutive losses the older breaker needs.

This is what catches "chronic losers" like KO (0%) and MS (17%) that bleed slowly.

## Feedback / self-correction

`core/feedback.py` (`feedback_text`) feeds the MetaStrategist **decayed** per-symbol stats
(win rate, profit factor, expectancy, whipsaw share) before it writes each day's rules. When a
symbol is flagged as a chronic loser, a `CHRONIC LOSER FLAG` line is injected telling the
strategist to **rewrite** (not restate) the rule with a materially more conservative entry, or
to recommend removing the symbol from the active universe.

## Tuning

All knobs are read from environment variables (see `core/config.py`) and were whitelisted in
the Cloud Run deploy (`deploy/deploy_cloud.ps1`), so they can be overridden per environment:

```
STRICT_UNIVERSE_ENABLED=true
MIN_LOW_WIN_RATE_TRADES=5
MAX_LOW_WIN_RATE=0.25
```

Defaults are the values tested against the 2026 data and are safe to keep.

## Tests

- `tests/test_universe_guardrail.py` — strict-universe behavior (watched OK, untracked blocked, crypto allowed, empty-watchlist blocks).
- `tests/test_anti_scale_in.py` — anti-scale-in (unwatched avg-down blocks, watched avg-down allowed, above-entry add allowed, crypto exempt).
- `tests/test_circuit_breaker.py` — added low win-rate cases.

## Related analysis

The evidence behind these changes lives in the (untracked) forensics outputs:
- `reports/equity_desk_diagnosis.md` — full narrative brief with per-ticker root causes.
- `reports/equity_desk_dataset.csv` — 189 reconstructed round-trips with attribution columns.
- `feedback/equity_lessons.md` — "do-not-do-X" playbook for the agents.
- `tools/equity_trade_forensics.py`, `tools/pull_cloud_db.py` — reusable read-only tooling.