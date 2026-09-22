# AMD Directional Analysis — AMD

_Generated 2026-09-21 14:37 · analysis only, no live changes_

**Direction definition:** `gap` — higher = OPEN > prior close (gap-up day)

## Base rates

- Days: **981** (higher 534 / lower 447)
- **Higher rate: 54.4%**

## Amplitude (defining-price % change)

| bucket | mean % | median % |
|---|---|---|
| higher | 1.58 | 1.06 |
| lower | -1.47 | -0.97 |
| all | 0.19 (std 2.48) | — |

## Overnight vs intraday decomposition

_What fraction of the daily move happens overnight (gap) vs intraday._

| bucket | mean overnight_share |
|---|---|
| higher | 0.44 |
| lower | 0.42 |

_overnight_share = |gap| / (|gap| + |intraday|). 1.0 = move is all overnight; 0.0 = all intraday. A high share means the daily direction is set by the gap (hard to predict from prior-day indicators)._

## Higher rate by day of week

| day | n | higher rate |
|---|---|---|
| 0 | 184 | 54.3% |
| 1 | 203 | 48.8% |
| 2 | 201 | 56.7% |
| 3 | 195 | 57.9% |
| 4 | 198 | 54.5% |

## Higher rate by regime

| regime | n | higher rate |
|---|---|---|
| BREAKOUT | 121 | 67.8% |
| RANGING | 94 | 57.4% |
| TRENDING_DOWN | 368 | 52.4% |
| TRENDING_UP | 397 | 51.6% |

## Feature means by bucket (higher vs lower)

| feature | higher mean | lower mean |
|---|---|---|
| rsi_14 | 53.600 | 53.569 |
| sma20_slope_pct | 0.137 | 0.164 |
| macd_hist | 0.067 | -0.025 |
| bollinger_pos | 0.120 | 0.080 |
| atr_pct | 4.454 | 4.412 |
| vwap_dist_sigma | 1.535 | 1.397 |
| gap_pct | 0.123 | 0.264 |
| intraday_range_pct | 3.991 | 3.964 |
| close_vs_open_pct | 0.145 | 0.070 |
| volume_pct_change | 9.520 | 9.956 |
| volume_vs_avg | 1.007 | 0.993 |
| overnight_share | 0.437 | 0.422 |
| streak | 1.944 | 1.933 |
| rel_strength_spy | 0.198 | 0.236 |

## Conditional probabilities — P(higher | condition)

Base rate: **54.4%** (lower 45.6%)

| condition | n | P(higher) | P(lower) | mean move % |
|---|---|---|---|---|
| rsi_14 < 40 | 132 | 62.1% | 37.9% | +0.39 |
| bollinger_pos > 0.5 | 318 | 58.5% | 41.5% | +0.42 |
| overnight_share > 0.5 | 377 | 57.8% | 42.2% | +0.23 |
| vwap_dist_sigma < 0.5 | 203 | 51.2% | 48.8% | -0.10 |
| regime == RANGING | 94 | 57.4% | 42.6% | +0.46 |
| regime == TRENDING_UP | 397 | 51.6% | 48.4% | +0.18 |
| rsi_14 > 60 | 300 | 57.0% | 43.0% | +0.27 |
| volume_vs_avg > 1.2 | 234 | 56.8% | 43.2% | +0.21 |
| vwap_dist_sigma > 1.0 | 588 | 56.8% | 43.2% | +0.30 |
| macd_hist < 0 | 474 | 52.1% | 47.9% | +0.10 |
| macd_hist > 0 | 506 | 56.7% | 43.3% | +0.28 |
| overnight_share < 0.5 | 603 | 52.4% | 47.6% | +0.17 |
| regime == TRENDING_DOWN | 368 | 52.4% | 47.6% | -0.01 |
| rsi_14 < 45 | 266 | 56.0% | 44.0% | +0.22 |
| streak >= 3 | 223 | 52.9% | 47.1% | -0.07 |
| volume_vs_avg < 0.8 | 374 | 52.9% | 47.1% | +0.11 |
| sma20_slope_pct > 0 | 543 | 55.4% | 44.6% | +0.28 |
| sma20_slope_pct < 0 | 436 | 53.4% | 46.6% | +0.08 |
| volume_pct_change < 0 | 502 | 55.4% | 44.6% | +0.26 |
| bollinger_pos < -0.5 | 213 | 53.5% | 46.5% | +0.20 |
| volume_pct_change > 0 | 478 | 53.6% | 46.4% | +0.12 |
| atr_pct > median | 490 | 53.9% | 46.1% | +0.20 |
| rel_strength_spy > 0 | 494 | 54.9% | 45.1% | +0.18 |
| streak >= 2 | 464 | 54.1% | 45.9% | +0.20 |
| rel_strength_spy < 0 | 486 | 54.1% | 45.9% | +0.21 |

_Low-n conditions (<20) are omitted; treat small-n rows with caution. `mean move %` is the expectancy (defining-price move) — the KPI that drives PnL._

## Short-side signals — P(lower | condition) ranked by downside expectancy

_For shorting AMD: conditions where P(lower) is high AND the downside move is large._

| condition | n | P(lower) | lower lift vs base | downside mean move % |
|---|---|---|---|---|
| rsi_14 < 40 | 132 | 37.9% | -7.7% | -1.26 |
| bollinger_pos > 0.5 | 318 | 41.5% | -4.1% | -1.50 |
| overnight_share > 0.5 | 377 | 42.2% | -3.4% | -1.48 |
| vwap_dist_sigma < 0.5 | 203 | 48.8% | +3.2% | -1.59 |
| regime == RANGING | 94 | 42.6% | -3.0% | -1.29 |
| regime == TRENDING_UP | 397 | 48.4% | +2.8% | -1.50 |
| rsi_14 > 60 | 300 | 43.0% | -2.6% | -1.45 |
| volume_vs_avg > 1.2 | 234 | 43.2% | -2.4% | -1.81 |
| vwap_dist_sigma > 1.0 | 588 | 43.2% | -2.4% | -1.35 |
| macd_hist < 0 | 474 | 47.9% | +2.3% | -1.50 |
| macd_hist > 0 | 506 | 43.3% | -2.3% | -1.43 |
| overnight_share < 0.5 | 603 | 47.6% | +2.0% | -1.46 |

_`downside mean move %` is the mean defining-price move on LOWER days only — the expected gain from a short. Rank by |lower lift| first, then downside size._

## Classifier cross-check (walk-forward logistic regression)

- Out-of-sample n: **784**
- **OOS accuracy: 50.9%** vs base rate 56.0%
- Accuracy minus base: **-5.1%**

### Feature weights (final fit; sign = direction of influence)

| feature | weight |
|---|---|
| bollinger_pos | +0.4259 |
| rsi_14 | -0.3387 |
| vwap_dist_sigma | +0.2310 |
| sma20_slope_pct | -0.1149 |
| rel_strength_spy | -0.0809 |
| day_of_week | +0.0590 |
| overnight_share | +0.0576 |
| atr_pct | +0.0524 |
| macd_hist | -0.0293 |
| volume_pct_change | -0.0279 |
| streak | -0.0171 |
| volume_vs_avg | -0.0026 |
| intraday_range_pct | -0.0020 |

_Positive weight => pushes toward a HIGHER close; negative => LOWER._

## Candidate rule hypotheses for backtest_amd.py

_These are hypotheses to TEST in the grid backtest — not live changes._

Top single-condition signals (|lift| vs base):

- `rsi_14 < 40` → 62.1% P(higher) (+7.7% lift, n=132) → favors a **HIGHER** close.
- `bollinger_pos > 0.5` → 58.5% P(higher) (+4.1% lift, n=318) → favors a **HIGHER** close.
- `overnight_share > 0.5` → 57.8% P(higher) (+3.4% lift, n=377) → favors a **HIGHER** close.
- `vwap_dist_sigma < 0.5` → 51.2% P(higher) (-3.2% lift, n=203) → favors a **LOWER** close.
- `regime == RANGING` → 57.4% P(higher) (+3.0% lift, n=94) → favors a **HIGHER** close.
- `regime == TRENDING_UP` → 51.6% P(higher) (-2.8% lift, n=397) → favors a **LOWER** close.

Suggested grid additions to `backtest_amd.py` (test, don't ship):

- Add a `gap_pct` gate (e.g. only enter when `gap_pct > 0`).
- Add a `bollinger_pos` gate (e.g. only enter when `bollinger_pos > 0`).
- Add an `rsi_14` floor (e.g. skip when `rsi_14 < 40`).
- Add a `macd_hist` sign filter (e.g. only when `macd_hist > 0`).

Short-side (short AMD) hypotheses:

- `rsi_14 < 40` → 37.9% P(lower) (-7.7% lower lift, n=132) → short setup.
- `bollinger_pos > 0.5` → 41.5% P(lower) (-4.1% lower lift, n=318) → short setup.
- `overnight_share > 0.5` → 42.2% P(lower) (-3.4% lower lift, n=377) → short setup.
- `vwap_dist_sigma < 0.5` → 48.8% P(lower) (+3.2% lower lift, n=203) → short setup.
- `regime == RANGING` → 42.6% P(lower) (-3.0% lower lift, n=94) → short setup.

