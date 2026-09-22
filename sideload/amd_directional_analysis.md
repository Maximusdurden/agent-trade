# AMD Directional Analysis — AMD

_Generated 2026-09-21 14:37 · analysis only, no live changes_

**Direction definition:** `close` — higher = close > prior close

## Base rates

- Days: **981** (higher 509 / lower 472)
- **Higher rate: 51.9%**

## Amplitude (defining-price % change)

| bucket | mean % | median % |
|---|---|---|
| higher | 2.74 | 1.95 |
| lower | -2.33 | -1.77 |
| all | 0.30 (std 3.59) | — |

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
| 0 | 184 | 53.3% |
| 1 | 203 | 51.2% |
| 2 | 201 | 52.2% |
| 3 | 195 | 49.7% |
| 4 | 198 | 53.0% |

## Higher rate by regime

| regime | n | higher rate |
|---|---|---|
| BREAKOUT | 121 | 57.9% |
| RANGING | 94 | 53.2% |
| TRENDING_DOWN | 368 | 52.4% |
| TRENDING_UP | 397 | 49.1% |

## Feature means by bucket (higher vs lower)

| feature | higher mean | lower mean |
|---|---|---|
| rsi_14 | 53.445 | 53.738 |
| sma20_slope_pct | 0.144 | 0.155 |
| macd_hist | 0.057 | -0.008 |
| bollinger_pos | 0.117 | 0.084 |
| atr_pct | 4.459 | 4.409 |
| vwap_dist_sigma | 1.537 | 1.403 |
| gap_pct | 0.266 | 0.102 |
| intraday_range_pct | 4.018 | 3.937 |
| close_vs_open_pct | 0.070 | 0.155 |
| volume_pct_change | 11.176 | 8.150 |
| volume_vs_avg | 1.021 | 0.978 |
| overnight_share | 0.438 | 0.422 |
| streak | 2.043 | 1.985 |
| rel_strength_spy | 0.268 | 0.159 |

## Conditional probabilities — P(higher | condition)

Base rate: **51.9%** (lower 48.1%)

| condition | n | P(higher) | P(lower) | mean move % |
|---|---|---|---|---|
| rsi_14 < 40 | 132 | 60.6% | 39.4% | +0.65 |
| vwap_dist_sigma < 0.5 | 203 | 46.8% | 53.2% | -0.07 |
| volume_vs_avg > 1.2 | 234 | 56.8% | 43.2% | +0.61 |
| volume_vs_avg < 0.8 | 374 | 48.1% | 51.9% | +0.11 |
| rsi_14 < 45 | 266 | 55.6% | 44.4% | +0.42 |
| vwap_dist_sigma > 1.0 | 588 | 55.3% | 44.7% | +0.50 |
| bollinger_pos < -0.5 | 213 | 54.9% | 45.1% | +0.51 |
| regime == TRENDING_UP | 397 | 49.1% | 50.9% | +0.25 |
| bollinger_pos > 0.5 | 318 | 54.1% | 45.9% | +0.51 |
| macd_hist < 0 | 474 | 49.8% | 50.2% | +0.21 |
| macd_hist > 0 | 506 | 53.8% | 46.2% | +0.39 |
| atr_pct > median | 490 | 53.5% | 46.5% | +0.37 |
| rsi_14 > 60 | 300 | 53.3% | 46.7% | +0.46 |
| regime == RANGING | 94 | 53.2% | 46.8% | +0.63 |
| streak >= 3 | 251 | 50.6% | 49.4% | +0.30 |
| gap_pct < 0 | 447 | 51.0% | 49.0% | +0.41 |
| rel_strength_spy < 0 | 486 | 51.2% | 48.8% | +0.29 |
| gap_pct > 0 | 533 | 52.5% | 47.5% | +0.21 |
| volume_pct_change > 0 | 478 | 51.3% | 48.7% | +0.19 |
| regime == TRENDING_DOWN | 368 | 52.4% | 47.6% | +0.09 |
| rel_strength_spy > 0 | 494 | 52.4% | 47.6% | +0.31 |
| volume_pct_change < 0 | 502 | 52.4% | 47.6% | +0.41 |
| sma20_slope_pct < 0 | 436 | 52.1% | 47.9% | +0.18 |
| overnight_share > 0.5 | 377 | 51.7% | 48.3% | +0.27 |
| sma20_slope_pct > 0 | 543 | 51.7% | 48.3% | +0.40 |
| streak >= 2 | 496 | 52.0% | 48.0% | +0.33 |
| overnight_share < 0.5 | 603 | 51.9% | 48.1% | +0.32 |

_Low-n conditions (<20) are omitted; treat small-n rows with caution. `mean move %` is the expectancy (defining-price move) — the KPI that drives PnL._

## Short-side signals — P(lower | condition) ranked by downside expectancy

_For shorting AMD: conditions where P(lower) is high AND the downside move is large._

| condition | n | P(lower) | lower lift vs base | downside mean move % |
|---|---|---|---|---|
| rsi_14 < 40 | 132 | 39.4% | -8.7% | -2.55 |
| vwap_dist_sigma < 0.5 | 203 | 53.2% | +5.1% | -2.28 |
| volume_vs_avg > 1.2 | 234 | 43.2% | -5.0% | -2.95 |
| volume_vs_avg < 0.8 | 374 | 51.9% | +3.8% | -2.00 |
| rsi_14 < 45 | 266 | 44.4% | -3.8% | -2.32 |
| vwap_dist_sigma > 1.0 | 588 | 44.7% | -3.4% | -2.27 |
| bollinger_pos < -0.5 | 213 | 45.1% | -3.0% | -2.11 |
| regime == TRENDING_UP | 397 | 50.9% | +2.8% | -2.37 |
| bollinger_pos > 0.5 | 318 | 45.9% | -2.2% | -2.40 |
| macd_hist < 0 | 474 | 50.2% | +2.1% | -2.35 |
| macd_hist > 0 | 506 | 46.2% | -1.9% | -2.31 |
| atr_pct > median | 490 | 46.5% | -1.6% | -2.67 |

_`downside mean move %` is the mean defining-price move on LOWER days only — the expected gain from a short. Rank by |lower lift| first, then downside size._

## Classifier cross-check (walk-forward logistic regression)

- Out-of-sample n: **784**
- **OOS accuracy: 51.5%** vs base rate 51.7%
- Accuracy minus base: **-0.1%**

### Feature weights (final fit; sign = direction of influence)

| feature | weight |
|---|---|
| rsi_14 | -0.6426 |
| bollinger_pos | +0.4991 |
| vwap_dist_sigma | +0.2367 |
| sma20_slope_pct | +0.1211 |
| atr_pct | +0.0748 |
| volume_vs_avg | +0.0597 |
| macd_hist | -0.0581 |
| gap_pct | +0.0541 |
| intraday_range_pct | -0.0491 |
| overnight_share | +0.0433 |
| rel_strength_spy | -0.0424 |
| day_of_week | -0.0164 |
| volume_pct_change | +0.0157 |
| streak | +0.0021 |

_Positive weight => pushes toward a HIGHER close; negative => LOWER._

## Candidate rule hypotheses for backtest_amd.py

_These are hypotheses to TEST in the grid backtest — not live changes._

Top single-condition signals (|lift| vs base):

- `rsi_14 < 40` → 60.6% P(higher) (+8.7% lift, n=132) → favors a **HIGHER** close.
- `vwap_dist_sigma < 0.5` → 46.8% P(higher) (-5.1% lift, n=203) → favors a **LOWER** close.
- `volume_vs_avg > 1.2` → 56.8% P(higher) (+5.0% lift, n=234) → favors a **HIGHER** close.
- `volume_vs_avg < 0.8` → 48.1% P(higher) (-3.8% lift, n=374) → favors a **LOWER** close.
- `rsi_14 < 45` → 55.6% P(higher) (+3.8% lift, n=266) → favors a **HIGHER** close.
- `vwap_dist_sigma > 1.0` → 55.3% P(higher) (+3.4% lift, n=588) → favors a **HIGHER** close.

Suggested grid additions to `backtest_amd.py` (test, don't ship):

- Add a `gap_pct` gate (e.g. only enter when `gap_pct > 0`).
- Add a `bollinger_pos` gate (e.g. only enter when `bollinger_pos > 0`).
- Add an `rsi_14` floor (e.g. skip when `rsi_14 < 40`).
- Add a `macd_hist` sign filter (e.g. only when `macd_hist > 0`).

Short-side (short AMD) hypotheses:

- `rsi_14 < 40` → 39.4% P(lower) (-8.7% lower lift, n=132) → short setup.
- `vwap_dist_sigma < 0.5` → 53.2% P(lower) (+5.1% lower lift, n=203) → short setup.
- `volume_vs_avg > 1.2` → 43.2% P(lower) (-5.0% lower lift, n=234) → short setup.
- `volume_vs_avg < 0.8` → 51.9% P(lower) (+3.8% lower lift, n=374) → short setup.
- `rsi_14 < 45` → 44.4% P(lower) (-3.8% lower lift, n=266) → short setup.

