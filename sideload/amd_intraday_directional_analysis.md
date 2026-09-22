# AMD Intraday Directional Analysis — AMD

_Generated 2026-09-21 15:02 · analysis only, no live changes_

**Signal:** open-to-9:45 ET momentum (knowable at entry) → **target:** open-to-close return.

## Base rates

- Days: **499** (higher 241 / lower 258)
- **Higher rate: 48.3%**

## Amplitude (9:45-to-close % change — post-entry target)

| bucket | mean % | median % |
|---|---|---|
| higher | 1.78 | 1.33 |
| lower | -1.65 | -1.26 |
| all | 0.00 (std 2.45) | — |

## Feature means by bucket (higher vs lower)

| feature | higher mean | lower mean |
|---|---|---|
| open_to_945_pct | 0.064 | 0.071 |
| rsi_945 | 51.667 | 52.672 |
| rel_strength_945 | 0.053 | 0.067 |
| gap_pct | 0.216 | 0.084 |
| prev_day_move_pct | 0.265 | 0.519 |
| prev_rsi | 54.989 | 52.819 |
| volume_vs_avg | 1.002 | 1.045 |

## Conditional probabilities — P(higher | condition)

Base rate: **48.3%** (lower 51.7%)

| condition | n | P(higher) | P(lower) | mean move % |
|---|---|---|---|---|
| rsi_945 < 40 | 146 | 52.1% | 47.9% | +0.09 |
| gap_pct < 0 | 167 | 46.1% | 53.9% | +0.02 |
| prev_rsi < 40 | 58 | 50.0% | 50.0% | +0.11 |
| prev_day_move_pct > 0 | 208 | 46.6% | 53.4% | -0.05 |
| rel_strength_945 < 0 | 248 | 46.8% | 53.2% | -0.12 |
| rel_strength_945 > 0 | 251 | 49.8% | 50.2% | +0.13 |
| open_to_945_pct > 0.5 | 184 | 49.5% | 50.5% | +0.15 |
| prev_day_move_pct < 0 | 178 | 47.2% | 52.8% | -0.02 |
| open_to_945_pct < 0 | 239 | 49.0% | 51.0% | -0.02 |
| gap_pct > 0 | 220 | 47.7% | 52.3% | -0.07 |
| open_to_945_pct < -0.5 | 171 | 48.0% | 52.0% | -0.07 |
| rsi_945 > 60 | 183 | 48.6% | 51.4% | +0.10 |
| volume_vs_avg > 1.2 | 100 | 48.0% | 52.0% | +0.10 |
| open_to_945_pct > 0 | 258 | 48.1% | 51.9% | +0.03 |

_Low-n conditions (<20) are omitted. `mean move %` is the open-to-close expectancy._

## Short-side signals — P(lower | condition) ranked by downside expectancy

| condition | n | P(lower) | lower lift vs base | downside mean move % |
|---|---|---|---|---|
| rsi_945 < 40 | 146 | 47.9% | -3.8% | -1.77 |
| gap_pct < 0 | 167 | 53.9% | +2.2% | -1.69 |
| prev_rsi < 40 | 58 | 50.0% | -1.7% | -1.92 |
| prev_day_move_pct > 0 | 208 | 53.4% | +1.7% | -1.65 |
| rel_strength_945 < 0 | 248 | 53.2% | +1.5% | -1.73 |
| rel_strength_945 > 0 | 251 | 50.2% | -1.5% | -1.57 |
| open_to_945_pct > 0.5 | 184 | 50.5% | -1.2% | -1.61 |
| prev_day_move_pct < 0 | 178 | 52.8% | +1.1% | -1.80 |
| open_to_945_pct < 0 | 239 | 51.0% | -0.7% | -1.68 |
| gap_pct > 0 | 220 | 52.3% | +0.6% | -1.74 |
| open_to_945_pct < -0.5 | 171 | 52.0% | +0.3% | -1.86 |
| rsi_945 > 60 | 183 | 51.4% | -0.3% | -1.45 |

_`downside mean move %` = mean 9:45-to-close move on LOWER days only (expected short gain)._

## Amplitude (volatility) hypothesis — does morning velocity predict range expansion?

_Pivot from directional to volatility trading: even if direction is a coin-flip, morning momentum may predict a LARGER remaining range (tradeable via straddles/breakouts)._

- **corr(|open_to_945|, remaining_range):** 0.289
- **corr(|open_to_945|, |9:45-to-close move|):** 0.204

| |open_to_945| bucket | n | median remaining range % | median |move| % |
|---|---|---|---|
| baseline (all days) | 499 | 2.88 | 1.3 |
| |vel| < 0.25% | 76 | 2.73 | 1.27 |
| 0.25-0.5% | 68 | 2.7 | 1.26 |
| 0.5-1.0% | 124 | 2.51 | 1.07 |
| |vel| > 1.0% | 231 | 3.33 | 1.52 |

_Decision gate: corr < 0.20 → abandon the 9:45 open-breakout framework; >= 0.35 → design a volatility playbook (strangle/breakout bracket)._

## Classifier cross-check (walk-forward logistic regression)

- Out-of-sample n: **310**
- **OOS accuracy: 50.6%** vs base rate 49.7%
- Accuracy minus base: **+1.0%**

### Feature weights (final fit; sign = direction of influence)

| feature | weight |
|---|---|
| open_to_945_pct | +0.3082 |
| rel_strength_945 | -0.2272 |
| prev_rsi | +0.2261 |
| day_of_week | -0.2016 |
| prev_day_move_pct | -0.1444 |
| volume_vs_avg | -0.0881 |
| gap_pct | +0.0586 |
| rsi_945 | -0.0421 |

_Positive weight => pushes toward a HIGHER close; negative => LOWER._
