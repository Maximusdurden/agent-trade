# Strategist Model A/B Experiment (deepseek-r1 vs Claude Sonnet)

**Date:** 2026-09-02
**Status:** Implemented (code), ready to observe. Re-deploy + let it run ~2-4 weeks.

## Why
The MetaStrategist (the model that *writes* the per-ticker trading rules) runs on
`deepseek/deepseek-r1`. Forensics showed the recent bad equity rules (MS dip-add, KO
noise-momentum) were **agent-authored rule-design errors**, not market noise. Since the
weak link is strategy-writing, we want to A/B the strategist model against a strong,
more conservative candidate (Claude Sonnet) and measure which one writes rules that
actually perform — instead of swapping blind.

## How it works

1. `core/config.py` defines the experiment:
   ```python
   STRATEGIST_AB_MODELS = "deepseek/deepseek-r1,anthropic/claude-sonnet-4"  # default
   STRATEGIST_AB_LABEL  = "r1-vs-sonnet"
   ```
   A comma-separated list of **two** OpenRouter model ids.

2. `core/strategist.py::MetaStrategist._pick_ab_model()` alternates between them
   **per UTC date** (even/odd day → model A / model B). The chosen model is passed to
   `generate_structured(..., explicit_model=...)`, bypassing the tier→model map.

3. Every logged rule carries the authoring model in `strategy_history.strategy_version`:
   `v<timestamp>|model=anthropic-claude-sonnet-4` (or `deepseek-deepseek-r1`).

4. `tools/strategist_ab_report.py` re-attributes each closed round-trip to the model
   that authored the **active rule at entry time**, and reports win rate / PnL /
   expectancy grouped by model.

## Toggling
- **Default:** the two-model experiment is ON if `STRATEGIST_AB_MODELS` is unset
  (falls back to the hardcoded `deepseek/deepseek-r1,anthropic/claude-sonnet-4`).
- **Disable / single model:** set `STRATEGIST_AB_MODELS` to a single id, or the
  existing `STRATEGIST_MODEL_TIER` flow resumes (no `explicit_model`).
- **Swap the variant:** edit the env var (e.g. `anthropic/claude-sonnet-4` → another id)
  and re-deploy. Toggling is config-only — no code change.

Env vars are whitelisted in `deploy/deploy_cloud.ps1` so they persist on Cloud Run.

## Reading results
Run after ~2-4 weeks of the SAME models alternating:
```powershell
python tools/strategist_ab_report.py
```
`reports/strategist_ab_report.md` shows per-model: round-trips, net PnL, win rate,
avg hold, largest win/loss, plus a per-ticker split.

> Caveat: with a small number of round-trips the split is not statistically
> significant. Let it run long enough to accumulate RTs per model (each model authors
> rules ~50% of days, but rules persist across days, so attribution is per-entry).

## Guardrails are impartial
The strict-universe / anti-scale-in / low-win-rate guardrails apply **to both arms
equally**, so any measured difference reflects the *model's* rule-quality, not risk
control.

## Other model levers worth considering (not yet implemented)
- **Brain model** (currently `gemini-2.5-flash` `daily_driver`) is your high-frequency
  decision-maker. A `gemini-2.5-pro` brain is a separate A/B worth doing after the
  strategist one, since it changes per-tick actions, not just daily rules.
- **Reasoning vs speed:** `deepseek-r1` is a reasoning model (slower, more "confident").
  Claude Sonnet and Gemini Pro are typically more conservative on risk prose. The A/B
  directly tests whether "more conservative strategist" beats "more confident strategist."