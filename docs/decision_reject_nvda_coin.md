# Decision: Reject NVDA and COIN from the Options Universe

**Date:** 2026-10-09
**Status:** Rejected — no code change.
**Module:** `sideload/backtest_entry_ablation.py` (universe screen)
**JIRA:** [TMCL-1069](https://maximusdurden.atlassian.net/browse/TMCL-1069)

## Proposal evaluated

Add NVDA and/or COIN to the live options universe (Model A sweep-fade).

## Evidence (90-day ablation, $0.25 penetration threshold, live exit rules)

| Ticker | Model | Trades | Underlying Win% | Option Win% | Total PnL% | PF |
|---|---|---|---|---|---|---|
| NVDA | A | 35 | 40.0% | 37.1% | −46.7% | 0.89 |
| COIN | A | 11 | 36.4% | 36.4% | −84.4% | 0.68 |
| NVDA | B | 38 | 44.7% | 39.5% | −62.2% | 0.81 |
| COIN | B | 35 | 34.3% | 34.3% | −216.1% | 0.65 |

## Why rejected

- **Underlying win rate < 50% for both** (NVDA 40%, COIN 36.4%). Model A is a
  mean-reversion strategy — it needs the underlying to fade back after the sweep.
  NVDA and COIN don't mean-revert in the 09:30–10:15 window; they trend.
- **Stop bucket dominates.** COIN: 7 of 11 trades hit the −22% stop vs 4 hitting
  the +45% target. NVDA: 14 stops vs 6 targets. Premium decays faster than the
  underlying fades.
- **Model B (compressed ORB) is even worse** for both — not an entry-style
  artifact; these tickers don't fit either engine.
- Neither clears the PF 1.50 bar (best: NVDA 0.89, COIN 0.68).

## What we do instead

- Keep the TSLA-only universe.
- Re-screen candidates only if a new entry engine or exit rule set is validated
  that changes the underlying win-rate requirement.

## Reference

- `sprint_plan.md` → "Model A Conviction & Universe Findings"
- `sideload/backtest_entry_ablation.py` (run with `--symbol NVDA COIN --days 90`)