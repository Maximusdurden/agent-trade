# Decision: Reject Dynamic Conviction Sizing for Model A Options

**Date:** 2026-10-09
**Status:** Rejected — no code change.
**Module:** `sideload/runner_options_multiticker.py` (Model A entry), `sideload/backtest_entry_ablation.py`
**JIRA:** [TMCL-1068](https://maximusdurden.atlassian.net/browse/TMCL-1068)

## Proposal evaluated

Scale capital based on setup conviction — a TSLA setup with $1.00+ sweep
penetration and a large pre-market range has higher expectancy than a borderline
$0.26 breach, so size up on high-conviction days.

## Evidence (252-day backtest, live exit rules 45/22/30-min)

| Penetration bucket | n | Win% | Avg PnL% | PF |
|---|---|---|---|---|
| $0.25–0.50 | 16 | 31.2% | −6.20 | 0.64 |
| $0.50–1.00 | 28 | 39.3% | +0.57 | 1.03 |
| $1.00–2.00 | 42 | 50.0% | +5.11 | 1.36 |
| $2.00+ | 15 | 33.3% | −2.67 | 0.85 |

- `corr(penetration, pnl) = 0.043` — penetration depth is **not** a continuous signal.
- The $2.00+ collapse is real (15 trades, 10 stops / 4 targets / 1 time): deep
  sweeps on TSLA are **trend days, not fade days**. Sizing up there would have
  doubled losses on the worst bucket.

## Why rejected

The edge is a **binary gate, not a dial**. Expectancy does not scale with
penetration depth; it exists in a narrow band ($1.00–2.00) and collapses beyond
it. Any sizing multiplier keyed to conviction would have concentrated capital in
the worst-performing buckets.

## What we do instead

- Keep sizing flat (existing `$500` base / `$650` elastic cap).
- Use penetration as a **filter**: the $1.00 minimum threshold isolates the
  profitable band (see TMCL-1066, pending Monday baseline).
- PM-range ceiling ($4.00) as a second filter (see TMCL-1067).

## Reference

- `sprint_plan.md` → "Model A Conviction & Universe Findings"
- `sideload/forward_validate_conviction.py` (walk-forward tooling)