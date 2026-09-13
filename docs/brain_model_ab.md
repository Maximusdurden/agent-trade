# Brain (Executor) Model A/B Experiment (gemini-2.5-flash vs gemini-2.5-pro)

**Date:** 2026-09-12
**Status:** Active. Re-deploy + let it run ~2-4 weeks.

## Why
The TradingBrain (the executor) is the high-frequency decision-maker that appraises
every ticker every 15-minute cycle. It normally runs on `google/gemini-2.5-flash`
(the `daily_driver` tier). A stronger model (`gemini-2.5-pro`) may make better
per-tick calls, but it is cost-sensitive (many symbols × many cycles) and its
decisions are already heavily constrained by impartial guardrails (universe-only
entries, no-scale-in, no-noise-momentum, VWAP dead zone, day-direction lock). So we
measure it independently rather than swapping blind.

## How it works

1. `core/config.py` defines the experiment:
   ```python
   BRAIN_AB_MODELS = "google/gemini-2.5-flash,google/gemini-2.5-pro"  # default
   BRAIN_AB_LABEL  = "flash-vs-pro"
   ```
   A comma-separated list of **two** OpenRouter model ids.

2. `core/trading_brain.py::TradingBrain._pick_ab_model()` alternates between them
   **per UTC date** (even/odd day → model A / model B). The chosen model is passed to
   `generate_structured(..., explicit_model=...)`, bypassing the tier→model map.

3. Every logged decision carries the authoring model in the `decisions.model`
   column (stamped by `TradingBrain._stamp_model` and persisted by the runner via
   `database.log_decision(..., model=...)`). Even non-experiment runs stamp the
   resolved tier default, so all decisions are attributable.

## Toggling
- **Default:** the two-model experiment is ON if `BRAIN_AB_MODELS` is unset
  (falls back to the hardcoded `google/gemini-2.5-flash,google/gemini-2.5-pro`).
- **Disable / single model:** set `BRAIN_AB_MODELS` to a single id, or the
  existing `BRAIN_MODEL_TIER` flow resumes (no `explicit_model`).
- **Swap the variant:** edit the env var and re-deploy. Toggling is config-only —
  no code change.

Env vars are whitelisted in `deploy/deploy_cloud.ps1` so they persist on Cloud Run.

## Reading results
Query the `decisions` table grouped by `model`:
```sql
SELECT model, COUNT(*) AS ticks,
       SUM(CASE WHEN is_approved=1 AND proposed_action IN ('BUY','SELL') THEN 1 ELSE 0 END) AS executed,
       AVG(conviction) AS avg_conviction
FROM decisions
GROUP BY model;
```
For realized PnL attribution, join executed decisions to closed round-trips (the
same approach `tools/strategist_ab_report.py` uses for rules, but keyed on the
decision's `model` at entry time).

> Caveat: with a small number of round-trips the split is not statistically
> significant. Let it run long enough to accumulate ticks per model.

## Guardrails are impartial
The strict-universe / anti-scale-in / low-win-rate / VWAP / day-direction guardrails
apply **to both arms equally**, so any measured difference reflects the *model's*
decision quality, not risk control.

## Relationship to the strategist A/B
The strategist A/B (`docs/strategist_model_ab.md`) changes the *daily rules*; this
brain A/B changes the *per-tick actions*. They are independent and can run
concurrently, but results should be read separately.