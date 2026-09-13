# Strategist Model A/B Experiment (gemini-2.5-flash vs Claude Sonnet)

**Date:** 2026-09-02 (updated 2026-09-12)
**Status:** Active. Re-deploy + let it run ~2-4 weeks.

## Why
The MetaStrategist (the model that *writes* the per-ticker trading rules) is the
weak link in strategy quality. Forensics showed the recent bad equity rules (MS
dip-add, KO noise-momentum) were **agent-authored rule-design errors**, not market
noise. We A/B the strategist model against a strong, more conservative candidate
(Claude Sonnet) and measure which one writes rules that actually perform — instead
of swapping blind.

> **2026-09-12 change:** the original control `deepseek/deepseek-r1` was removed
> because it is slow/503-prone on OpenRouter (TMCL-896..902), which exhausted the
> LLM budget and produced "empty rule" failures. The control is now the reliable
> `google/gemini-2.5-flash` (the daily-driver model the strategist already ran on).
> Also fixed a bug where `explicit_model` was never forwarded to the underlying
> completion call, so **both arms were silently running the same tier-default
> model** — the experiment was non-functional until this fix.

## How it works

1. `core/config.py` defines the experiment:
   ```python
   STRATEGIST_AB_MODELS = "google/gemini-2.5-flash,anthropic/claude-sonnet-5"  # default
   STRATEGIST_AB_LABEL  = "flash-vs-sonnet"
   ```
   A comma-separated list of **two** OpenRouter model ids.

2. `core/strategist.py::MetaStrategist._pick_ab_model()` alternates between them
   **per UTC date** (even/odd day → model A / model B). The chosen model is passed to
   `generate_structured(..., explicit_model=...)`, bypassing the tier→model map.

3. Every logged rule carries the authoring model in `strategy_history.strategy_version`:
   `v<timestamp>|model=anthropic-claude-sonnet-5` (or `google-gemini-2.5-flash`).

4. `tools/strategist_ab_report.py` re-attributes each closed round-trip to the model
   that authored the **active rule at entry time**, and reports win rate / PnL /
   expectancy grouped by model.

## Toggling
- **Default:** the two-model experiment is ON if `STRATEGIST_AB_MODELS` is unset
  (falls back to the hardcoded `google/gemini-2.5-flash,anthropic/claude-sonnet-5`).
- **Disable / single model:** set `STRATEGIST_AB_MODELS` to a single id, or the
  existing `STRATEGIST_MODEL_TIER` flow resumes (no `explicit_model`).
- **Swap the variant:** edit the env var (e.g. `anthropic/claude-sonnet-5` → another id)
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

## Brain (executor) A/B — separate experiment
The TradingBrain (high-frequency per-tick decision-maker) has its own A/B, since it
changes per-tick actions, not daily rules, and must be measured independently. See
`docs/brain_model_ab.md`. It uses `BRAIN_AB_MODELS` and stamps each decision with the
authoring model in the `decisions.model` column.