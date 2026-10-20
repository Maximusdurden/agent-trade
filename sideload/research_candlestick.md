# Candlestick Pattern Trading Research — What Works, What Doesn't

_Research compiled 2026-10-12 · analysis only, no live changes · for the sideload lane_

**Purpose:** Decide which candlestick patterns are worth implementing in Python for the
sideload lane (SPY/NVDA learning + practice), and whether sequencing/combining patterns
improves the odds. This document is the research foundation for a pattern-detection +
confluence backtest.

---

## 1. The honest academic baseline (read this first)

Before picking patterns, the rigorous literature is largely **skeptical** about candlestick
patterns as standalone signals on liquid large-caps at daily frequency. Every pattern we
considered has been tested systematically; the results are consistent:

| Study | Finding |
|---|---|
| **Marshall, Young & Rose (2006)**, J. Banking & Finance | Tested 35 candlestick patterns on DJIA components 1992–2002; after White's Reality Check (data-snooping bootstrap), **no pattern** generates statistically significant abnormal returns |
| **Horton (2009)**, Q. Rev. Econ & Finance | Same null across hundreds of US names |
| **Open-Alpha-Lab Study 76 (Rice-Paper)** | Engulfing, hammer, shooting star, doji on 15 US equities 2010–2026: **no edge** beyond a random-day baseline with the same directional assignment |
| **Study 403 (Hammer/Hanging Man)** | 11,816 hammer-shaped bars over 6 decades: best forward edge **+0.05% at 3 days, HAC t=0.92** — negative net of costs |
| **Study 405 (Doji)** | Doji **underperforms** the base rate at every horizon; a label-shuffle placebo beats it ~84% of the time at 5 days |
| **Study 186 (Morning Star)** | Fires **too late** — the third candle consumes the mean-reversion bounce a random day from the same window would capture |
| **Study 187 (Three Soldiers)** | Fires **near exhaustion** — a 3-day run-up is already above-average; short-horizon mean reversion dominates |
| **Study 408 (Three Black Crows)** | **Wrong-sign** — shorting after 3 red candles fights short-horizon mean reversion (Jegadeesh 1990; Lehmann 1990) |
| **Bulkowski (2008)**, Encyclopedia of Candlestick Charts | Three White Soldiers: ~82% bullish breakout rate but **unremarkable average forward returns** |

### The core insight

> Candlestick patterns alone, on liquid large-caps at daily frequency, **do not beat a
> random-day baseline net of costs**. The "reversal" they appear to show is mostly
> short-horizon mean reversion (Jegadeesh 1990; Lehmann 1990; Lo & MacKinlay 1990) wearing a
> candlestick costume.

**Why this matters for us:** it means the patterns are *not* a standalone edge. They are a
**filter / confirmation layer** on top of something else (trend, volume, regime, mean
reversion). The research doc in `../candlestick/CANDLESTICK_PATTERNS.md` documents all 61
patterns; this doc is about which ones to actually trade.

---

## 2. The 5 most promising patterns to try

Ranked by (a) strength of the underlying microstructure story, (b) rarity (less
over-traded), and (c) compatibility with confluence confirmation:

| # | Pattern | Why promising | Caveat / how to use |
|---|---|---|---|
| **1** | **Bullish/Bearish Engulfing** | The most-taught reversal; the *intraday* rejection is a real microstructure event. Best when combined with volume + support/resistance | Alone: null (Study 402). Needs confluence |
| **2** | **Hammer / Hanging Man** | Long lower wick = genuine intraday rejection of lows. The *within-day* information is real | Forward edge +0.05% (t=0.92) — only works with trend filter + confirmation |
| **3** | **Morning/Evening Star** | 3-candle structure captures the full reversal sequence; the doji variant adds indecision signal | Fires late (Study 186) — use as *exit* or *confirmation*, not entry |
| **4** | **Three White Soldiers / Black Crows** | Bulkowski: ~82% breakout rate. Strong trend-continuation signal | Fires near exhaustion — best used as *trend confirmation*, not reversal |
| **5** | **Abandoned Baby** | Bulkowski: 65% accuracy over 20 days, median -3.0% (bearish). Rare = less over-traded | Extremely rare (~50 occurrences/20 yrs on S&P 500) — low sample |

**Key nuance:** The most promising patterns are the ones where the *microstructure story* is
strongest (engulfing, hammer) or where *rarity* preserves signal (abandoned baby) — but
**none work standalone**.

---

## 3. Does combining patterns in sequence help?

### What DOESN'T work: stacking multiple candlestick patterns

- Waiting for 2–3 candlestick patterns to align (e.g., hammer + engulfing + morning star)
  **reduces sample size to near-zero** and doesn't add independent information — they're all
  measuring the same mean-reversion effect.
- The Open-Alpha-Lab studies show each pattern's signal is largely the *same* base-rate
  effect wearing different costumes.

### What DOES work: cross-signal confluence (pattern + non-pattern confirmation)

The practical approach used in real trading engines is **score-based confluence** — the
candlestick pattern is ONE vote in a scoring system, not the whole signal:

```python
# The pattern that works: candlestick pattern as ONE vote in a scoring system
momentum_score = 0
if patterns.get('bullish_engulfing') or patterns.get('latest_hammer'):
    momentum_score += 1          # candlestick pattern = 1 vote
if rsi < 30: momentum_score += 1  # oversold = 1 vote
if price > sma_50: momentum_score += 1  # trend = 1 vote
if volume > avg_volume * 1.5: momentum_score += 1  # volume = 1 vote
# Only trade when momentum_score >= 2 (confluence)
```

### The evidence-backed confluence recipe

| Layer | What to add | Why it helps |
|---|---|---|
| **1. Trend filter** | Price vs. 50/200 SMA | Patterns only fire in trend direction (or at trend extremes) |
| **2. Volume confirmation** | Volume > 1.5× average on pattern candle | Confirms genuine participation, filters noise |
| **3. Support/Resistance** | Pattern at key S/R level | The academic studies show patterns work better at levels |
| **4. Oscillator** | RSI < 30 (bullish) / > 70 (bearish) | Confirms oversold/overbought condition |
| **5. Timeframe alignment** | Daily pattern + weekly trend | Multi-timeframe confluence (the MTFCM strategy pattern) |

### The sequencing insight from the research

The **most honest use of candlestick patterns** is:

1. **Use them as a FILTER, not a trigger** — e.g., "only take long entries when a bullish
   engulfing confirms a pullback in an uptrend"
2. **Use them as EXIT signals** — e.g., evening star after a rally = take profits (this is
   where the "fires late" weakness becomes a strength)
3. **Combine with mean-reversion** — the one effect that *does* show up in the data
   (Jegadeesh 1990) — patterns that fire after sharp moves capture this

---

## 4. Implementation plan (Python)

### 4.1 TA-Lib availability

**Checked 2026-10-12:** TA-Lib is **NOT installed** in the agent-trade venv
(`.venv`, Python 3.13.5). `pandas 3.0.5`, `numpy 2.5.1`, `yfinance`, `matplotlib`,
`mplfinance` are available.

**Options:**

| Option | Pros | Cons |
|---|---|---|
| **A. Install TA-Lib** (`pip install TA-Lib`) | All 61 patterns built-in (`CDLENGULFING`, `CDLHAMMER`, etc.); battle-tested | Requires C library on Windows (often painful); adds a heavy dep |
| **B. Hand-roll detectors in pandas** | Zero deps; full control over thresholds; matches project's existing indicator style | ~10 patterns to implement; more code to maintain |
| **C. Use `pandas_ta`** (pure Python) | Has candlestick pattern functions; pip-installable | Less battle-tested than TA-Lib; some patterns missing |

**Recommendation: Option B (hand-rolled pandas detectors)** for the 5 patterns above. The
project already computes indicators in pandas (`core/data_provider.py`), and we only need
~10 detectors (5 patterns × bullish/bearish). This avoids the TA-Lib C-library install
headache on Windows and keeps the code consistent with the existing style. If we later want
the full 61-pattern zoo, we can add TA-Lib then.

### 4.2 Data source

The sideload lane uses **Alpaca historical bars** via `core.alpaca_client.AlpacaClient`
(`get_historical_bars` / `get_historical_bars_paginated`), matching `backtest_amd.py`.
For SPY/NVDA daily data, use the same client. `yfinance` is available as a fallback for
quick research.

### 4.3 Backtest methodology (critical — avoid the academic traps)

The academic studies fail because of **benchmark choice**. To do this right:

1. **Random-day baseline control** — compare pattern forward returns against a random draw
   of days with the same directional assignment. A pattern adds real signal only if it beats
   this baseline.
2. **Net of costs** — charge spread + slippage + borrow (for shorts). A coin-flip gross edge
   dies once you pay the spread twice.
3. **Multiple-comparisons discipline** — with 5 patterns × 2 directions × N horizons, a lone
   significant result is expected by chance. Use Bonferroni (threshold |t| ≥ 2.6 for 12
   tests) or report all results honestly.
4. **HAC / Newey-West t-stats** — nearby pattern signals share overlapping forward windows;
   use HAC standard errors.
5. **Out-of-sample** — split data (e.g., 2010–2020 train, 2021–2026 test). Do NOT tune
   thresholds on the test set.

### 4.4 Proposed module layout (sideload/)

```
sideload/
├── patterns.py              # ✅ DONE — hand-rolled candlestick detectors (28 patterns)
├── backtest_patterns.py     # NEXT — confluence backtest: pattern + trend + volume + RSI
├── research_candlestick.md  # THIS document
└── (existing files...)
```

### 4.5 Implementation status (2026-10-12)

**`patterns.py` is built and verified.** It implements **28 detectors** (pure pandas,
no TA-Lib):

- **Single-candle:** hammer, inverted hammer, hanging man, shooting star, marubozu,
  doji, dragonfly doji, gravestone doji
- **Two-candle:** bullish/bearish engulfing, bullish/bearish harami, piercing pattern,
  dark cloud cover, kicking
- **Three-candle:** morning/evening star, morning/evening doji star, three white
  soldiers, three black crows, abandoned baby, three inside up/down, three outside
  up/down
- **Multi-candle:** rising/falling three methods

**Key design decisions:**
- **Trend precondition with offset** — `_prior_trend(df, bars=5, direction, offset)`
  measures the trend on the window BEFORE the pattern's own candles (offset=1 for
  2-candle patterns, offset=2 for 3-candle patterns). Without this, the pattern's own
  candles contaminate the trend measurement and nothing fires.
- **Star definition** — a star candle's body must be ≤ 50% of the prior candle's body
  (the canonical Nison definition), not ≤ 30% of its own range.
- **Long body** — body ≥ 1.5× the 20-bar rolling average body.
- **Doji** — body ≤ 10% of the bar's range.

**Verified on real data (SPY daily, 2023–2026, 939 bars):**

| Pattern | Count | Notes |
|---|---|---|
| bullish_engulfing | 10 | Common |
| bearish_engulfing | 21 | Common |
| hammer | 4 | Rare (strict definition) |
| hanging_man | 7 | Rare |
| morning_star | 2 | Rare |
| evening_star | 7 | Rare |
| three_white_soldiers | 1 | Very rare (as expected) |
| three_black_crows | 0 | Very rare |
| abandoned_baby | 0 | Extremely rare (~50/20yr on S&P 500) |

**Usage:**
```python
from sideload.patterns import detect_all_patterns, detect_core_patterns
signals = detect_core_patterns(df)   # dict[str, pd.Series[bool]]
# or all 28:
signals = detect_all_patterns(df)
```

### 4.6 Confluence scoring (initial version)

```python
def confluence_score(df, i, pattern_signal):
    """Score 0-4: pattern + trend + volume + RSI."""
    score = 0
    if pattern_signal: score += 1
    if df['close'][i] > df['sma50'][i]: score += 1          # trend
    if df['volume'][i] > df['vol_avg20'][i] * 1.5: score += 1  # volume
    if df['rsi14'][i] < 30 or df['rsi14'][i] > 70: score += 1  # oscillator
    return score
# Trade only when score >= 2 (or >= 3 for higher conviction)
```

---

## 5. Next steps

1. **✅ Build `patterns.py`** — DONE (28 detectors, verified on synthetic + real SPY data)
2. **✅ Build `backtest_patterns.py`** — DONE (multi-symbol universe, weighted confluence,
   OOS in markdown — see findings below)
3. **✅ Compare** — pattern-only vs. pattern+confluence vs. random-day baseline — DONE
   (see Section 7: no pattern beats the baseline OOS)
4. **Decide** — see Section 7 recommendation: do NOT wire into live lane yet; the one
   actionable lead is the *contrarian* bearish_engulfing signal

---

## 7. Backtest findings (2026-10-12)

**Run:** `python -m sideload.backtest_patterns` (default 7-symbol universe)
**Universe:** SPY, QQQ, IWM, NVDA, AMD, AAPL, MSFT
**Data:** daily bars 2010-01-01 → 2026-10-01 (4,211 bars per symbol), net of 5 bps round trip
**Outputs:** `sideload/backtest_patterns.json`, `sideload/backtest_patterns_{SYMBOL}.md`,
`sideload/backtest_patterns_POOLED.md`

### The headline result

**Candlestick patterns alone do NOT beat the random-day baseline — confirming the
academic literature on our own 7-symbol universe.**

| Symbol | Baseline 5d (long) | Best pattern 5d | Verdict |
|---|---|---|---|
| SPY | +0.26% | morning_star +0.43% (n=19, t=0.80) | Not significant |
| NVDA | +0.82% | hammer +2.13% (n=20, t=2.23) | Beats baseline but small n |
| **POOLED** | **+0.51%** | **hammer +0.74% (n=173, t=1.92)** | **Near baseline, not significant** |

### Pooled universe findings (the key numbers)

**Full sample (2010-2026, pooled across 7 symbols):**

| Pattern | n | 5d mean | tstat | vs baseline (+0.51%) |
|---|---|---|---|---|
| bullish_engulfing | 358 | +0.52% | +2.27 | ⚠️ At baseline |
| bearish_engulfing | 627 | -0.42% | -2.22 | ❌ Underperforms |
| hammer | 173 | +0.74% | +1.92 | ⚠️ Near baseline |
| hanging_man | 236 | -0.37% | -1.15 | ❌ Underperforms |
| morning_star | 104 | -0.10% | -0.27 | ❌ Underperforms |
| evening_star | 97 | -0.41% | -0.96 | ❌ Underperforms |
| three_white_soldiers | 3 | +1.73% | — | Too few samples |
| three_black_crows | 4 | -1.96% | — | Too few samples |

**Out-of-sample (Test 2021-2026):**

| Pattern | n | 5d mean | tstat | OOS verdict |
|---|---|---|---|---|
| bullish_engulfing | 141 | +0.26% | +0.75 | ❌ Not significant |
| bearish_engulfing | 189 | -0.69% | -2.12 | ❌ Underperforms |
| hammer | 46 | +0.05% | +0.08 | ❌ Not significant |
| hanging_man | 63 | -0.60% | -1.04 | ❌ Underperforms |
| morning_star | 34 | -0.31% | -0.37 | ❌ Underperforms |
| evening_star | 44 | -0.18% | -0.41 | ❌ Underperforms |

### Key insights

1. **The drift dominates.** The random-day baseline (pooled 5d = +0.51%) captures the
   unconditional upward drift. Bullish patterns that "work" in-sample (hammer, bullish
   engulfing) are just riding the drift — their OOS performance collapses to ~zero.

2. **Bearish patterns consistently underperform** — shorting after a bearish pattern
   fights the upward drift (the "wrong-sign" finding from Study 408). bearish_engulfing
   is significantly NEGATIVE in the claimed direction (t=-2.22 full, -2.12 OOS).

3. **The pooling worked** — rare patterns accumulated cross-sectional samples:
   - three_white_soldiers: 1 → 3 signals
   - three_black_crows: 0-1 → 4 signals
   - abandoned_baby: still 0 (extremely rare, ~50/20yr on S&P 500)
   - hammer: 19-29 → 173 signals (statistically usable)
   - morning_star: 8-23 → 104 signals

4. **Weighted confluence (trend=2, volume=1, RSI=1) prevents signal starvation** —
   n_conf1+ and n_conf2+ buckets now have meaningful counts (e.g., hammer n_conf1+=128,
   n_conf2+=104). The conf3 bucket (trend+volume+RSI all aligned) shows the strongest
   returns in several patterns (hammer 5d conf3=+2.07% vs conf<1=-0.01%) — but sample
   sizes are still small.

5. **No pattern passes the OOS bar.** The Bonferroni threshold (|t| ≥ 2.64) is not met
   by any pattern in the OOS test period. The best OOS performer is bearish_engulfing
   (t=-2.12) — and it's negative, meaning the pattern is a *contrarian* signal (the
   opposite of folklore).

### Recommendation

- **Do NOT wire any pattern into the live lane.** No pattern beats the random-day
  baseline net of costs out-of-sample.
- **The one actionable insight:** bearish_engulfing is significantly NEGATIVE in the
  claimed direction — i.e., after a bearish engulfing, price tends to RISE (mean
  reversion). This is a potential *contrarian* signal worth investigating: buying after
  bearish engulfing instead of shorting.
- **Next step options:**
  1. Test the contrarian hypothesis (buy after bearish_engulfing, short after
     bullish_engulfing) with the same academic methodology
  2. Add more symbols (S&P 500 constituents) to accumulate samples for the rare patterns
  3. Test on intraday data (1h/15min) where patterns may behave differently
  4. Accept the null and use patterns only as a discretionary visual aid, not a
     systematic signal

---

## 8. Contrarian bearish_engulfing test (2026-10-12)

**Hypothesis:** A bearish engulfing has a statistically significant NEGATIVE forward
return in its claimed bearish direction (t=-2.12 OOS). If price consistently RISES after
the pattern fires, going LONG (the contrarian trade) may beat the unconditional upward
drift — especially when the engulfing occurs ABOVE the 50-SMA (a liquidity grab /
pullback entry).

**Implementation:** Added `bearish_engulfing_contrarian` to `patterns.py` — it delegates
to the exact same `bearish_engulfing` detector but maps its direction to `bullish` in
`PATTERN_DIRECTIONS`. This makes the confluence scorer look for bullish confirmation
(price > SMA50, volume surge, RSI < 40) and compares against the bullish random-day
baseline automatically.

### Results (pooled 7-symbol universe, net of 5 bps)

**Full sample (2010-2026):**

| Signal | n | 5d mean | tstat | 10d mean | tstat |
|---|---|---|---|---|---|
| bearish_engulfing (folklore) | 626 | -0.43% | -2.31 | -1.01% | -3.94 |
| **bearish_engulfing_contrarian** | **626** | **+0.33%** | **+1.78** | **+0.91%** | **+3.55** |
| bullish baseline | — | +0.51% | +20.13 | +1.00% | +27.06 |

**Out-of-sample (Test 2021-2026):**

| Signal | n | 5d mean | tstat | 10d mean | tstat |
|---|---|---|---|---|---|
| bearish_engulfing (folklore) | 189 | -0.73% | -2.27 | -1.24% | -2.44 |
| **bearish_engulfing_contrarian** | **189** | **+0.63%** | **+1.96** | **+1.14%** | **+2.24** |
| bullish baseline | — | +0.51% | +20.13 | +1.00% | +27.06 |

### Verdict

**The contrarian signal is directionally correct but does NOT beat the bullish baseline
out-of-sample.**

- The contrarian long after bearish engulfing is **positive** (OOS 5d +0.63%, 10d
  +1.14%) — confirming the folklore direction is wrong.
- BUT it **underperforms the unconditional bullish baseline** (OOS 5d +0.63% vs +0.51%
  baseline is roughly at parity; 10d +1.14% vs +1.00% baseline is marginally above but
  t=2.24 < Bonferroni 2.64).
- The 10d full-sample t=3.55 looks strong, but the OOS t=2.24 does not clear the
  Bonferroni bar — the edge is not robust out-of-sample.

**Confluence insight:** The conf1 bucket (trend-aligned, i.e., engulfing above the
50-SMA) shows the strongest contrarian returns (full-sample 10d conf1=+4.72%, n=527
conf1+). This supports the "liquidity grab above the SMA" thesis — but the sample is
dominated by conf1+ (508/626), so it's not a differentiated sub-population.

### Bottom line

- **The folklore bearish_engulfing signal is confirmed wrong** (significantly negative
  in its claimed direction, both full-sample and OOS).
- **The contrarian long is positive but not a standalone edge** — it roughly matches the
  unconditional drift. No Bonferroni-clearing OOS t-stat.
- **Do not wire it into the live lane.** The honest conclusion: on this universe and
  timeframe, bearish engulfing carries no exploitable directional information beyond the
  market's drift.

---

## 9. Intraday (1-hour bar) backtest (2026-10-12)

**Motivation:** Daily patterns proved largely redundant to unconditional market drift.
Moving to 1-hour bars increases sample counts (each symbol has ~3,415 hourly bars vs
~4,211 daily bars, but signals fire 5-10x more often) and aligns with short-term intraday
rejections. New file: `backtest_patterns_intraday.py` (reuses the shared machinery from
`backtest_patterns.py`).

**Methodology (unchanged academic discipline):**
- Data: yfinance 1h bars, ~2-year lookback (2024-10-13 → 2026-09-30, capped by yfinance's
  730-day intraday limit), filtered strictly to regular market hours 09:30-16:00 ET.
- Horizons in **bars**: `HORIZONS = [1, 2, 4, 8]` (~1h, 2h, half-day, full-day holds).
- **Session-aware masking:** forward windows that cross the session close are dropped
  (no unexecutable overnight holds). Consequence: the 8h horizon has ~0 valid bars —
  an 8-bar hold always spans the 6.5h session close. Effective max intraday horizon is
  ~6 bars.
- OOS split: Train < 2025-10-01, Test >= 2025-10-01. Same 5 bps cost, HAC t-stats,
  Bonferroni |t| >= 2.64.

### Pooled results (7-symbol universe, net of 5 bps)

**Intraday random-bar baseline is tiny:** +0.011% at 1h, +0.037% at 2h, +0.072% at 4h,
+0.107% at 8h. The 5 bps round-trip cost is a huge fraction of the 1h forward move —
weak signals die fast.

| Pattern | n (1h) | 1h mean | tstat | 2h mean | tstat | 4h mean | tstat |
|---|---|---|---|---|---|---|---|
| bullish_engulfing | 484 | -0.004% | -0.06 | -0.034% | -0.43 | +0.195% | +1.19 |
| bearish_engulfing | 478 | -0.090% | -2.31 | -0.045% | -0.79 | -0.045% | -0.42 |
| bearish_engulfing_contrarian | 478 | -0.010% | -0.27 | -0.055% | -0.96 | -0.055% | -0.52 |
| hammer | 95 | +0.069% | +0.80 | +0.047% | +0.50 | +0.526% | +1.65 |
| hanging_man | 122 | +0.110% | +1.41 | +0.129% | +1.16 | +0.094% | +0.59 |
| morning_star | 59 | -0.210% | -2.04 | -0.129% | -1.67 | +0.586% | +0.94 |
| evening_star | 43 | -0.250% | -1.62 | -0.286% | -1.64 | -0.635% | -1.94 |
| three_white_soldiers | 2 | -0.380% | +0.00 | +0.418% | +0.00 | — | — |
| three_black_crows | 0 | — | — | — | — | — | — |

**Out-of-sample (Test 2025H2-2026):**

| Pattern | n (1h) | 1h mean | tstat | 2h mean | tstat | 4h mean | tstat |
|---|---|---|---|---|---|---|---|
| bullish_engulfing | 238 | -0.015% | -0.16 | -0.088% | -0.72 | +0.062% | +0.24 |
| bearish_engulfing | 238 | -0.155% | -2.58 | -0.149% | -1.66 | -0.192% | -1.39 |
| bearish_engulfing_contrarian | 238 | +0.055% | +0.91 | +0.049% | +0.55 | +0.092% | +0.67 |
| hammer | 43 | +0.024% | +0.40 | -0.054% | -0.59 | -0.043% | -0.34 |
| hanging_man | 53 | +0.161% | +1.19 | +0.081% | +0.48 | +0.098% | +0.39 |
| morning_star | 27 | -0.357% | -1.60 | -0.281% | -2.26 | +0.330% | +1.69 |
| evening_star | 24 | -0.272% | -2.08 | -0.476% | -2.56 | -0.650% | -1.88 |

### Findings

1. **No pattern clears Bonferroni OOS on intraday either.** The strongest OOS t-stats are
   bearish_engulfing 1h (t=-2.58, in the *wrong* direction for its folklore claim) and
   evening_star 2h (t=-2.56) — both below the 2.64 bar, and both negative in their claimed
   bearish direction (i.e., price rises after they fire).
2. **The contrarian thesis does NOT transfer to intraday.** On daily bars the contrarian
   long was positive (t=+2.24 OOS); on 1h bars it's flat-to-negative (OOS 1h +0.055%,
   t=+0.91). The "liquidity grab above the SMA" effect, if real, is a daily-frequency
   phenomenon — at 1h the engulfing candle is just noise.
3. **Cost friction dominates at 1h.** The intraday baseline is +0.011% at 1h; 5 bps is
   ~45% of the entire expected move. Any signal must clear ~5 bps just to break even —
   none do.
4. **Rare patterns stay rare even intraday.** three_white_soldiers fired 2x pooled,
   three_black_crows 0x, abandoned_baby 0x. The 3-candle continuation patterns are
   genuinely rare events, not a data-granularity artifact.
5. **h=8 is structurally unusable** — session-aware masking correctly eliminates all
   overnight-straddling holds, but that means an 8-bar (full-day) hold is never executable
   within a single session. Effective intraday horizon ceiling is ~6 bars.

### Bottom line

- **Intraday candlestick patterns do not beat the intraday random-bar baseline net of
  costs.** Same conclusion as daily, with more samples and tighter horizons.
- The microstructure story (engulfing = intraday rejection) does not survive contact with
  transaction costs at 1h granularity.
- **Do not wire any pattern into the live lane on intraday data.** If anything, the
  intraday evidence strengthens the "patterns are a discretionary visual aid" conclusion.

---

## 6. References

- Marshall, Young & Rose (2006), *Candlestick Technical Trading Strategies: Can They Create
  Value for Investors?*, Journal of Banking & Finance 30(8): 2303–2323
- Horton (2009), *Stars, Crows, and Doji*, Quarterly Review of Economics and Finance 49(2)
- Caginalp & Laurent (1998), *The Predictive Power of Price Patterns*, Applied Mathematical
  Finance 5(3–4)
- Lo, Mamaysky & Wang (2000), *Foundations of Technical Analysis*, Journal of Finance 55(4)
- Jegadeesh (1990), *Evidence of Predictable Behavior of Security Returns*, Journal of
  Finance 45(3)
- Lehmann (1990), *Fads, Martingales, and Market Efficiency*, QJE
- Bulkowski (2008), *Encyclopedia of Candlestick Charts*, Wiley
- White (2000), *A Reality Check for Data Snooping*, Econometrica 68(5)
- Sullivan, Timmermann & White (1999), *Data-Snooping, Technical Trading Rule Performance,
  and the Bootstrap*, Journal of Finance
- Open-Alpha-Lab studies 76, 186, 187, 402, 403, 404, 405, 408 (github.com/Guillain-RDCDE/Open-Alpha-Lab)
- Companion reference: `../candlestick/CANDLESTICK_PATTERNS.md` (all 61 patterns with
  definitions, images, and accuracy stats)

---

*Research only — no live changes. For educational purposes; not financial advice.*