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
8. **Target**: track realized AMD PnL daily; confirm the lane converges toward
   **$100/day net**.