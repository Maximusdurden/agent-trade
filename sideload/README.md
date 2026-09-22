# AMD Sideloaded Trading Lane

A dedicated, **paper-trading** lane that owns **AMD exclusively** and learns AMD
daily on its own. It reuses agent-trade's indicator engine, brain, guardrails,
and options stack, but with AMD-tuned rules. It writes to the **same database**
so AMD flows automatically to both **treatmotivated.capital** (blog) and
**dashboard.agenttrade.us** (decisions/thoughts) — zero changes to either.

## Two sub-lanes (P3 split, 2026-09-19)

Stocks and options are split into **dedicated lanes** (the analyst's
recommendation + the user's "sideload options" idea):

| Lane | Runner | Owns | Instruments |
|---|---|---|---|
| **AMD stock lane** | `runner_sideload.py` | AMD shares | Stocks (long + short via long puts) |
| **AMD options lane** | `runner_options.py` | AMD options | Options (long calls + long puts) |

- **Core product is now stocks-only** (`OPTIONS_ENABLED=false` in `.env`).
  Options moved OUT of core into the dedicated AMD-options lane.
- The options lane applies AMD-specific tuning (DTE 14-45, OTM 1-8%, 5% alloc,
  conviction 0.7) via `config_sideload.apply_sideload_options_overrides()`.

## Positioning

This lane is an **AMD expert / king / god**. It:
- Owns AMD exclusively (reserved from the normal lane — the two never fight).
- Only enters on **high-conviction, backtest-validated setups**.
- **Skips low-confidence days** rather than forcing trades.
- Is measured on **consistent expectancy**, not on trading every day.

The north star: *when you see the agent "in on AMD", you KNOW it's going to win.*

## Decisions (locked)

| Decision | Value |
|---|---|
| Trading | **Paper** (same Alpaca account) |
| Database | **Same DB** (blog + dashboard pick AMD up automatically) |
| AMD ownership | **Exclusive to sideload** (normal lane excludes it) |
| Instruments | **Stocks + options, split into dedicated lanes** (both directions) |
| Learning | **Daily, self-directed** (no user intervention) |
| Backtest | **Grid-only** (deterministic rule search, no ML model) |
| Data source | **Alpaca** historical bars |
| Intervals | Any **>= 5m** (prefer longer: 1h/1d) |
| Benchmark | Must **outperform indices** (SPY/QQQ) |
| Account | **$10K paper**, target **$100/day net** to start |
| Scheduling | Cloud Scheduler (learning) + 15-min loop (trading) |

## How AMD reaches the blog + dashboard (zero changes)

- **Blog**: `core/feedback.compute_closed_round_trips()` → `tools/build_blog_db.py`
  → `realized_trades` mirror → treatmotivated.capital. Any `trades` row with
  `symbol=AMD` shows up like any other ticker.
- **Dashboard**: reads `database.get_recent_decisions()` and renders
  `thought_process` + `proposed_symbol`. Any `log_decision` call with AMD shows
  up. The sideload lane tags cycles with a `sideload_amd-*` `cycle_id` prefix so
  AMD decisions are attributable separately.

## Modules

```
sideload/
├── __init__.py
├── config_sideload.py        # AMD-tuned config overrides (env-driven, SL_*)
├── jira_logging.py           # shared Jira error-logging helper
├── runner_sideload.py        # AMD STOCK trading cycle (paper, same DB)
├── runner_options.py         # AMD OPTIONS trading cycle (P3: dedicated lane)
├── backtest_amd.py           # deterministic grid-search backtest (grid-only)
├── learn_amd.py              # daily learning agent (edge -> tuned rule)
├── publish_fact_of_day.py    # "Dexter's AMD Fact of the Day" blog post
├── analyze_amd_direction.py  # what drives higher vs lower days (daily, analysis)
├── analyze_amd_intraday.py   # intraday: open-to-9:45 momentum -> open-to-close
└── README.md
```

### 0. `jira_logging.py`
Shared Jira error-logging helper. Every sideload script calls
`setup_jira_logging()` in `main()` (installs the global Jira handler + exception
hook) and `log_exception_to_jira()` inside `except` blocks (explicit ticket with
context metadata). No-ops when the Jira library is unavailable or in test/CI.

### 1. `config_sideload.py`
Loads base `core.config` then applies AMD-tuned overrides. All knobs are
env-driven with an `SL_` prefix so they can be tuned at runtime without a
redeploy. Key overrides:
- `SL_MAX_ALLOCATION_PCT` (0.10) — 10% of equity per AMD position.
- `SL_RSI_ENTRY_MAX` (45) — buy only on RSI pullback to support.
- `SL_RSI_EXIT_OVERBOUGHT` (65) — take profit when overbought.
- `SL_VWAP_DEAD_ZONE_SIGMA` (1.0) — no trades inside the VWAP dead zone.
- `SL_MIN_EDGE_SIGMA` (0.5) — require a real edge vs noise.
- `SL_MAX_HOLD_HOURS` (72) — force-exit stale positions.
- `SL_OPTIONS_ENABLED` (False) — stocks only until expert.
- `SL_INTERVALS` (`5min,15min,1h,1d`) — intervals for the backtest.
- `SL_DAILY_TARGET_USD` (100) — the daily net PnL target.

Also exposes `amd_expert_instruction()` — the system-level prompt block that
positions the brain as the AMD expert/king/god.

### 2. `runner_sideload.py`
The AMD-only trading cycle. Reuses the runner's decision → guardrail → execute
flow scoped to AMD:
1. Fetch AMD bars → `DataProvider` indicators.
2. `TradingBrain.make_decision(..., expert_instruction=amd_expert_instruction())`.
3. `RiskGuardrails.validate_and_adjust_decision` (AMD-tuned knobs).
4. `log_decision` / `log_trade` / `log_ticker_conviction` → **same DB**.
5. Paper order via `AlpacaClient.execute_market_order` (with TP/SL bracket).
6. Broker-order reconciliation + GCS sync.

```bash
python -m sideload.runner_sideload --once --dry-run   # AMD decision, no order
python -m sideload.runner_sideload --once             # place a paper AMD order
python -m sideload.runner_sideload --loop             # continuous 15-min loop
```

### 3. `backtest_amd.py`
The **deterministic grid-search backtest** (grid-only, no ML model). Pulls AMD
history from Alpaca, computes the same indicators the live lane uses, and sweeps
**all combos of all variables**:
`interval, RSI entry max, RSI exit overbought, MACD filter, VWAP dead-zone,
min edge sigma, ATR sizing baseline, max hold hours, trailing-stop giveback,
time-of-day window, regime filter`.

Three passes:
1. **Coarse grid** (2–3 values/var) → promising regions.
2. **Fine grid** around the coarse winners.
3. **Walk-forward out-of-sample validation** (train past, test future) → kills
   overfitting. Only configs that hold up out-of-sample are shippable.

```bash
python -m sideload.backtest_amd --coarse       # coarse grid first
python -m sideload.backtest_amd --fine         # fine grid around winners
python -m sideload.backtest_amd --walkforward  # out-of-sample validation
python -m sideload.backtest_amd --all          # run all three passes
```

Outputs: `sideload/backtest_coarse.json`, `backtest_fine.json`,
`backtest_validated.json`.

### 4. `learn_amd.py`
The daily learning agent. Reads the validated backtest winners, computes realized
AMD PnL vs the `$100/day` target, and writes a **tuned strategy rule** to the
`sideload_amd_strategy` table that the sideload brain reads each cycle. Emits a
"what the agent learned today" summary for the fact-of-the-day publisher.

```bash
python -m sideload.learn_amd            # run the daily learning pass
python -m sideload.learn_amd --dry      # print what would be written
```

### 5. `publish_fact_of_day.py`
Publishes **"Dexter's AMD Fact of the Day"** — a daily WordPress post in Dexter's
voice summarizing what the agent learned about AMD, with fresh AMD news for
context. Reuses `core/brain._apply_persona` (Dexter voice), `core/wordpress`
(publish), `core/discord_notifier` (notify), and `AlpacaClient.get_news`.

```bash
python -m sideload.publish_fact_of_day            # publish today's post
python -m sideload.publish_fact_of_day --dry      # print, don't publish
```

### 6. `analyze_amd_direction.py`
The **directional analysis** — the first step of the "scale the sideload method to
other tickers" plan. Builds a per-trading-day dataset of AMD daily bars with
**every indicator the core product computes** (RSI, SMA, MACD, Bollinger, ATR,
volume, regime), then analyzes what separates days that finish **higher**
(close > prior close) from days that finish **lower**.

- **Higher/lower**: configurable via `--direction`:
  - `close` (default): higher if close > prior close (amplitude = close-to-close %).
  - `gap`: higher if OPEN > prior close (gap-up day; amplitude = open-vs-prior-close %).
- **Descriptive stats** (primary): base rates, amplitude, per-feature bucket
  means, and conditional probabilities `P(higher | condition)` vs base rate.
- **Expectancy**: each conditional also reports `mean move %` (the defining-price
  move) — the KPI that drives PnL, not just win rate.
- **Short-side signals**: every conditional reports `P(lower)`, `lower lift`, and
  `downside mean move %` (mean move on LOWER days only) — so we can identify
  setups to **short AMD**, not just go long. A dedicated short-side section ranks
  conditions by downside expectancy, and short-side hypotheses are added to the
  candidate rules.
- **Classifier cross-check** (optional): walk-forward logistic regression
  (pure numpy, no ML dependency) — trains on past, tests on future, no lookahead.
- **News**: DEFERRED (NewsAPI free tier caps ~100 results/request — not enough
  for 3-4 yrs). Technical + volume analysis first; news can be backfilled later.

### KPIs computed (beyond the core indicators)

- `overnight_share` — fraction of the daily move that happens overnight (gap) vs
  intraday: `|gap| / (|gap| + |intraday|)`. High = direction set by the gap.
- `rel_strength_spy` — AMD daily move minus SPY daily move. **⚠️ INVALIDATED by
  target leakage** (used day-t close to predict day-t close). The intraday
  `rel_strength_945` (open-to-9:45 AMD vs SPY) is the valid, leakage-free version.
- `streak` — consecutive higher/lower days (momentum vs mean-reversion).
- `volume_vs_avg` — volume / 20-day average volume (cleaner than day-over-day).
- `gap_pct`, `close_vs_open_pct`, `intraday_range_pct` — overnight/intraday split.

> **⚠️ Leakage note:** all predictors in this module are lagged to t−1. The daily
> analysis finds **no exploitable edge** (classifier at coin-flip). Use the
> **intraday** module (`analyze_amd_intraday.py`) for the real signal.

```bash
python -m sideload.analyze_amd_direction --build-dataset   # build per-day dataset
python -m sideload.analyze_amd_direction --analyze         # descriptive analysis
python -m sideload.analyze_amd_direction --analyze --classifier  # + classifier
python -m sideload.analyze_amd_direction --all             # everything
python -m sideload.analyze_amd_direction --all --direction gap   # gap-up definition
python -m sideload.analyze_amd_direction --all --symbol NVDA --days 1000  # other tickers
```

Outputs (in `sideload/`):
- `data/amd_directional_dataset.csv` / `.json` — per-day dataset (`close` def).
- `data/amd_gap_directional_dataset.csv` / `.json` — per-day dataset (`gap` def).
- `data/amd_directional_buckets.json` / `amd_gap_directional_buckets.json` — per-bucket feature stats + conditionals.
- `data/amd_directional_classifier.json` / `amd_gap_directional_classifier.json` — classifier results (if run).
- `amd_directional_analysis.md` / `amd_gap_directional_analysis.md` — written findings + candidate rule hypotheses
  for `backtest_amd.py` to test (analysis only, no live changes).

> **Note:** the defining variable is excluded from predictors. For `--direction gap`,
> `gap_pct` IS the label, so it's not shown as a conditional or fed to the
> classifier (otherwise it'd be a tautological 100%/0% signal).

### 7. `analyze_amd_intraday.py`
The **intraday directional analysis** — the leakage-free continuation. Predicts
the **9:45-to-close return** (STRICTLY post-entry, no overlap with the signal)
from **open-to-9:45 ET momentum** (a genuinely knowable-at-entry signal), plus
AMD-vs-SPY 9:45 relative strength and lagged daily state.

**⚠️ Overlap-leakage correction (2026-09-21):** The original 69.7% finding used
an *open-to-close* target, which included the 9:30–9:45 move that the feature
also measured — a sub-component overlap. With the corrected **9:45-to-close**
target, the edge **collapses to coin-flip** (classifier OOS 50.6% vs 49.7% base,
+1.0%). **There is no directional edge in morning momentum.**

**Amplitude (volatility) hypothesis:** morning velocity does weakly predict range
expansion — `corr(|open_to_945|, remaining_range) = 0.289`, and the `|vel| > 1.0%`
bucket has median remaining range 3.33% vs 2.88% baseline. This is **below the
0.35 threshold** for a volatility playbook and **above 0.20** (not abandoned), so
it's a weak-but-present signal worth a closer look, not a tradeable edge yet.

```bash
python -m sideload.analyze_amd_intraday --all                # build + analyze + classifier
python -m sideload.analyze_amd_intraday --all --symbol NVDA   # other tickers
```

Outputs:
- `data/amd_intraday_directional_dataset.csv` / `.json`
- `data/amd_intraday_directional_buckets.json` / `amd_intraday_directional_classifier.json`
- `amd_intraday_directional_analysis.md`

### 8. `backtest_orb.py`
The **Opening Range Breakout (ORB) backtest** on the underlying equity — the
final test of the 9:45 AM intraday window. Avoids options entirely (no IV crush,
no double bid-ask spread). Per day: arm only on high morning velocity
(`|open_to_945| > threshold`), place OCO stop-entries outside the 9:30-9:45
range, hard stop (range or mid), exit at 2R target or 4:00 PM close.

```bash
python -m sideload.backtest_orb --all     # default config
python -m sideload.backtest_orb --grid    # sweep filter/stop/exit params
```

**Result (2026-09-21):** Best config across 108 grid combos is **$3.81/trade
expectancy, 56.3% win rate, 199 trades** (vel≥1.0%, range stop, 3R). **No config
clears $5/trade expectancy.** The 9:45 AM intraday window is **too efficiently
priced** — the ORB edge is marginal and below a meaningful threshold.

Outputs: `data/amd_orb_default.json`, `data/amd_orb_grid.json`.

### 9. `backtest_ema.py`
The **walk-forward test of the dexter-trader EMA crossover strategy** on AMD.
Dexter's backtester selects params by in-sample max PnL (selection bias); this
re-tests the same strategy (fast/slow EMA crossover on HLC3, 200-period daily
trend filter, trailing stop) with **walk-forward out-of-sample validation**.

```bash
python -m sideload.backtest_ema --all     # dexter's fixed AMD params (13/23/4%)
python -m sideload.backtest_ema --grid    # small param sweep
```

**Result (2026-09-21):** Dexter's AMD params (13/23/4%) rank **5th of 18** with
**$232/trade OOS expectancy, 50% win rate, 36 trades**. The top configs
(fast 17/slow 27/4%) reach $304/trade but with only **50-57% win rate** — the
expectancy is driven by a few large trend winners, not a consistent edge. The
4% trailing stop dominates exits (32 of 36). **Promising but not robust** — the
win rates are near coin-flip and the expectancy is concentrated in a handful of
big winners. Worth a deeper look, not a confirmed edge.

Outputs: `data/amd_ema_default.json`, `data/amd_ema_grid.json`.

## Normal-lane reservation

AMD is reserved from the normal lane so the two lanes never fight over it:
- `core/config.py::SIDELOAD_RESERVED_SYMBOLS` (default `{"AMD"}`).
- `runner.py::build_appraisal_universe` excludes reserved symbols.
- `core/screener.py::run_screener` excludes reserved symbols from the pool.

The sideload lane adds AMD back to its own universe at cycle time
(`config_sideload.apply_sideload_overrides`).

## Scheduling

- **Daily learning** (`learn_amd.py` + `publish_fact_of_day.py`): Cloud Scheduler
  (matches the roster jobs), off-hours.
- **Intraday trading** (`runner_sideload.py --loop`): 15-min loop like the normal
  lane.

### Cloud Run jobs (deploy/deploy_sideload.ps1)

Deploys two jobs from one image:
- **`sideload-daily`** → `run_amd_daily.py` — pulls DB from GCS, runs the
  backtest (coarse→fine→walk-forward), runs the learning agent, publishes the
  fact-of-day. Scheduled ~9:30pm NY.
- **`sideload-trader`** → `run_amd_trader.py` — bounded intraday trading loop
  (default 1 cycle per trigger). Scheduled every 15 min Mon-Fri.

```bash
.\deploy\deploy_sideload.ps1
```

Both entrypoints wire Jira error logging via `sideload/jira_logging.py`.

## Verification checklist

1. **Dry-run**: `python -m sideload.runner_sideload --once --dry-run` produces an
   AMD decision with no order.
2. **Paper trade**: `python -m sideload.runner_sideload --once` places a paper AMD
   order; confirm it lands in `trades` with `symbol=AMD`.
3. **Dashboard**: after a cycle, `dashboard.agenttrade.us` shows the AMD thought
   card (thought_process + AMD ticker).
4. **Blog**: after a closed round-trip, `treatmotivated.capital` shows AMD like
   any other ticker.
5. **Backtest**: `python -m sideload.backtest_amd --all` sweeps the grid, reports
   top configs by expectancy, and walk-forward validates them. Confirms AMD beats
   SPY/QQQ.
6. **Learning**: `python -m sideload.learn_amd` writes a tuned strategy rule.
7. **Fact of the Day**: `python -m sideload.publish_fact_of_day --dry` prints the
   Dexter-voiced post.
8. **Directional analysis**: `python -m sideload.analyze_amd_direction --all`
   builds the dataset, runs descriptive + classifier analysis, and writes
   `amd_directional_analysis.md` + supporting CSVs/JSON.
9. **Target**: track realized AMD PnL daily; confirm the lane converges toward
   **$100/day net**.