# Sprint Plan: Agent-Trade Enhancements & Optimization

This document outlines the research, system designs, and JIRA ticket breakdown for the upcoming sprint. In this sprint, we will work alongside our **developer (`dev`)** and **reviewer (`reviewer`)** subagents to build, test, and validate these premium trading functionalities.

---

## 📋 Table of Contents
1. [Sprint Strategy & Subagent Roles](#sprint-strategy--subagent-roles)
2. [Research & Technical Architecture](#research--technical-architecture)
   - [Epic 1: Universe Broadening & Batch Data Fetching](#epic-1-universe-broadening--batch-data-fetching)
   - [Epic 2: Autonomous Dynamic AI Screener](#epic-2-autonomous-dynamic-ai-screener)
   - [Epic 3: VWAP Indicator Integration](#epic-3-vwap-indicator-integration)
   - [Epic 4: End-of-Day Discord Reports](#epic-4-end-of-day-discord-reports)
   - [Epic 5: Cloud Resilience, Weekend Smart Sleep & Discord Kill Switch](#epic-5-cloud-resilience-weekend-smart-sleep--discord-kill-switch)
3. [JIRA Epic & Ticket Breakdown](#jira-epic--ticket-breakdown)
4. [Testing & Validation Protocols](#testing--validation-protocols)

---

## 👥 Sprint Strategy & Subagent Roles

To guarantee production-grade, fully tested, and resilient code, we will implement a dual-subagent pair programming cycle:

*   **Developer Agent (`dev`)**: Executes implementations, writes core logic, sets up database migrations, configures APIs, and develops robust unit tests under `tests/`.
*   **Reviewer Agent (`reviewer`)**: Audits pull requests, verifies mathematical/financial accuracy of indicators, tests edge cases (such as API timeouts, market holidays, and zero-volume states), runs unit tests, and validates performance metrics in Mock Mode.

Subagents will use the JIRA tickets below to communicate requirements, progress, and blockers by editing this file to mark status (`[ ] TO DO`, `[ ] IN PROGRESS`, `[ ] DONE`) and adding notes/comments inline.

---

## 🔍 Research & Technical Architecture

### Epic 1: Universe Broadening & Batch Data Fetching
*   **The Challenge**: The current implementation of `agent-trade` loops sequentially through a hardcoded `config.TRADING_UNIVERSE` of 10 tickers, performing individual API calls for historical candles. If we expand this to a wider universe (e.g., 50–100 companies), sequential fetching will trigger **Alpaca API rate limits** (200 requests/minute for standard paper accounts) and introduce severe latency (up to 2 minutes of block time per cycle).
*   **Candidate Pool Sourcing & Maintenance**: To avoid hardcoding, the broad candidate pool of 50-100 companies will be loaded from a JSON configuration file `screener_pool.json` in the project root. This file can be:
    1. **Manually Maintained**: Simply add or remove tickers by editing the JSON file.
    2. **Automatically Maintained (Semi-Dynamic)**: We will write a weekly maintenance script `tools/update_screener_pool.py` that downloads the current NASDAQ-100 or S&P 100 constituent tickers (e.g., via a robust scraping of Wikipedia's public lists) and automatically overwrites `screener_pool.json` with the highest-liquidity names. This provides zero-maintenance dynamic updates with a rock-solid offline fallback!
*   **The Solution**: Refactor `AlpacaClient` to use **batch requests**. Alpaca's `StockHistoricalDataClient` and `CryptoHistoricalDataClient` natively support passing a list of symbols:
    ```python
    request_params = StockBarsRequest(
        symbol_or_symbols=["AAPL", "MSFT", "GOOG", ...],
        timeframe=tf,
        start=start_time,
        end=datetime.now()
    )
    bars = self.data_client.get_stock_bars(request_params)
    ```
    This returns a combined multi-index DataFrame. We will update `DataProvider` to compute indicators efficiently in a single vectorized pass using Pandas `groupby` and `apply`:
    ```python
    df = df.groupby(level='symbol', group_keys=False).apply(self._add_technical_indicators)
    ```

### Epic 2: Autonomous Dynamic AI Screener
*   **The Mandate**: "Risk-averse, professional financial quantitative trading." Consuming a broad universe of 100+ stocks directly into the LLM Brain prompt would exceed optimal context windows, dilute attention, and increase token costs.
*   **The Solution**: An autonomous, two-stage **Screener Engine** (`core/screener.py`):
    ```mermaid
    flowchart TD
        A[Broad Candidate Pool: 100+ Stocks/Crypto] --> B[Stage 1: Liquidity Filter\nMin Daily Vol > $10M]
        B --> C[Stage 2: Technical Score\nEMA, RSI, MACD, Bollinger Bands]
        C --> D[Stage 3: SQLite Feedback Loop\nBooster for high win rate, Penalty/Blacklist for high losses]
        D --> E[Stage 4: Selection\nRank & pick top 3-5 watchlist candidates]
        E --> F[Watchlist fed to Execution LLM Brain]
    ```
*   **SQLite Feedback Loop (Soft Score Penalty)**: The screener queries the `trades` and `decisions` tables to analyze past performance. If a ticker has an active historical win rate of $>60\%$ for the agent's strategy, its screening score is multiplied by a booster (e.g., `1.2x`). If its win rate is underperforming ($<40\%$), it is penalized using a soft scoring multiplier (e.g., `0.7x`) rather than being hard blacklisted, allowing it to remain eligible if technical indicators are extremely compelling.

### Epic 3: VWAP Indicator Integration
*   **The Challenge**: VWAP (Volume Weighted Average Price) is an intraday-only technical indicator that resets at market open. We need to compute it dynamically using 15-minute intraday bars.
*   **The Solution**: In `DataProvider`, calculate intraday VWAP for each bar by grouping the historical data by date. To maintain optimal API performance and rate-limit compliance, calculations will utilize the existing 15-minute historical bar data (Option A) rather than spinning up heavy independent 1-minute data queries. For each calendar day, VWAP will reset dynamically using the typical price:
    $$\text{Typical Price} = \frac{\text{High} + \text{Low} + \text{Close}}{3}$$
    $$\text{VWAP} = \frac{\sum (\text{Typical Price} \times \text{Volume})}{\sum \text{Volume}}$$
    ```python
    # Calculate intraday VWAP in pandas
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['tp_vol'] = df['typical_price'] * df['volume']
    
    # Cumulative sums resetting daily
    df['cum_tp_vol'] = df.groupby(df.index.date)['tp_vol'].cumsum()
    df['cum_vol'] = df.groupby(df.index.date)['volume'].cumsum()
    df['vwap'] = df['cum_tp_vol'] / df['cum_vol']
    ```
*   **Applications in Strategy**:
    1.  **Dynamic Support/Resistance**: Price bouncing off VWAP confirms strong intraday levels.
    2.  **Mean Reversion**: Establish VWAP Standard Deviation Bands. When price stretches $\ge \pm 2\sigma$ from VWAP, flag it as extremely overbought/oversold.
    3.  **Execution Quality Metric**: Target buying below VWAP and selling above it.

### Epic 4: End-of-Day Discord Reports
*   **The Challenge**: Retrieve the `DISCORD_WEBHOOK_URL` from the neighboring `dexter-trader` project and implement daily performance reports.
*   **The Solution**: 
    - Copy the webhook variable from `Z:\python\projects\dexter-trader\.env` to `Z:\python\projects\agent-trade\.env`.
    - Create a clean `core/discord_notifier.py` client using `requests`.
    - Retrieve daily statistics and send reports using a modular command-line argument `--eod-report` in `runner.py`. This will be triggered once daily as an independent Windows Scheduled Task at **16:35 ET** (Option A) to guarantee clean isolation from the main 15-minute execution loop:
      - **Message A (Daily Summary)**: Total Buys/Sells count, total cash used, Realized PnL of completed trades, and Net Equity Change PnL. Displays a **Green Embed** sidebar for profitable days, and a **Red Embed** sidebar for unprofitable days.
      - **Message B (Detailed Breakdown)**: Chronological table of tickers bought and sold, showing average fill prices, share sizes, and realized PnL per trade.

### Epic 5: Cloud Resilience, Weekend Smart Sleep & Discord Kill Switch
*   **The Mandate**: "Establish high-efficiency sleep schedules and safety triggers for our cloud-deployed agent."
*   **The Weekend Smart Sleep Challenge**: In the cloud, the `agent-trade` loop runs via a Windows Scheduled Task (or daemon) 24/7. However, during weekends, the US equity market is closed, leaving only cryptocurrency (`SOL/USD`) active. If the portfolio holds no active cryptocurrency positions over the weekend, running the analysis loop every 15 minutes is a waste of cloud resources and OpenRouter API tokens.
*   **The Weekend Smart Sleep & Daily Cadence Challenge**: In the cloud, the `agent-trade` loop runs via a Windows Scheduled Task (or daemon) 24/7. However, during weekends, the US equity market is closed, leaving only cryptocurrency (`SOL/USD`) active. If the portfolio holds no active cryptocurrency positions over the weekend, running the analysis loop every 15 minutes is a waste of cloud resources and OpenRouter API tokens. Additionally, you need automated, high-fidelity daily check-ins to monitor when the bot starts and stops its daily tasks.
*   **The Weekend Smart Sleep & Daily Cadence Solution**: 
    - **Daily Morning Start Message**: Every weekday morning at **09:00 ET**, the first execution cycle of the day will transmit a friendly Discord message outlining our daily start: `"Hey, we're starting for the day! Here's what we have:"` with a breakdown of starting equity, cash, and active open positions.
    - **Daily Evening Shutdown Message**: Every weekday evening at market close (**16:30 ET**), the final equity cycle will transmit: `"We're shutting down the equity desk for the day! Here's what we have:"` showing closing equity, cash, and open positions.
    - **Smart Friday Dynamic Routing**: On Friday evening at 16:30 ET, when checking open positions:
      - If `has_crypto` is `False`, the message displays: `"We're shutting down the equity desk for the day, and we hold no crypto positions over the weekend. See you next Monday morning! 💤"` and writes a local state file `.weekend_skip.json` specifying that execution should be skipped until **Monday 09:00 ET**.
      - If `has_crypto` is `True`, it displays: `"We're shutting down the equity desk for the day. Equity market is closed, but we are holding crypto positions over the weekend. Crypto desk remains active! 🪙"`, continuing normal cycles over the weekend. If crypto is liquidated to 0 during the weekend, the bot immediately writes the skip state, posts a final hibernation alert, and shuts down until Monday morning.
    - If the skip flag is active, the runner bypasses all heavy logic, API queries, and LLM calls, immediately exiting and conserving resources.
    - During weekday trading hours, any stale `.weekend_skip.json` is automatically deleted.
*   **The Discord Kill Switch Challenge**: We need a way to stop the live trading loop in case of emergency directly from Discord (identical to how the user manages `dexter-trader`).
*   **The Discord Kill Switch Solution**:
    - Extend the existing `dexter-trader/core/discord_bot.py` with three additional commands: `!agentkill`, `!agentresume`, and `!agentstatus`.
    - These commands will read and write a `kill_switch.json` file in GCS (`agenttrade-us-data-bucket`).
    - Integrate a kill switch check at the beginning of `agent-trade/runner.py`. If GCS shows `kill_switch.json` is set to `"HALTED"`, the runner immediately exits.
    - Update `agent-trade/dashboard/dashboard.py` to check and display these statuses with reactive, glowing color-coded UI badges.

---

## 🎟️ JIRA Epic & Ticket Breakdown

### 🎯 Epic 1: Broadened Universe & Batch Processing [AT-EP1]
> **Goal**: Enable the agent to look at a broad universe of 50-100 companies by transitioning to batch API fetching and multi-index vectorized computations.

#### **AT-1: Research and Implement Batch Fetching in AlpacaClient**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Refactor `AlpacaClient.get_historical_bars` to support passing a list of symbols instead of a single string. It must query Alpaca's batch endpoints and return a clean, sorted, multi-index pandas DataFrame.
*   **Acceptance Criteria**:
106:     - Supports both single-symbol and multi-symbol lists.
107:     - Successfully routes stock symbols to `StockHistoricalDataClient` and crypto to `CryptoHistoricalDataClient`.
108:     - Implements retry logic and exception handling for invalid symbols.
*   **Sub-tasks**:
    - `[DONE] AT-1.1 [DEV]`: Refactor `AlpacaClient` historical bars methods.
    - `[DONE] AT-1.2 [REVIEWER]`: Validate multi-index format and rate-limit compliance.
*   **Comments**:

#### **AT-2: Batch Technical Indicator Computation in DataProvider**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Refactor `DataProvider` to process multi-index batch DataFrames. Implement group-by calculations for SMA, RSI, MACD, and Bollinger Bands.
*   **Acceptance Criteria**:
    - Correctly partitions indicators by ticker.
    - Outputs identical indicator values as the sequential computation when tested on the same inputs.
*   **Sub-tasks**:
    - `[DONE] AT-2.1 [DEV]`: Implement `groupby` transformations on DataProvider.
    - `[DONE] AT-2.2 [REVIEWER]`: Code-review vector logic and verify math accuracy.
*   **Comments**:

---

### 🎯 Epic 2: Autonomous Dynamic AI Screener [AT-EP2]
> **Goal**: Develop an autonomous screening engine that runs at the beginning of each cycle to select the top 3-5 assets based on technical setups and SQLite historic

#### **AT-3: Implement Technical Screening Engine**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 5 Story Points
*   **Description**: Create `core/screener.py` to filter a broad list of 100 tickers sourced from `screener_pool.json`. Filter out low-liquidity assets (average daily volume $< \$10\text{M}$) and score the remaining tickers based on Mean Reversion (Bollinger/RSI) and Momentum (SMA/MACD) setups. Implement `tools/update_screener_pool.py` as a weekly task to automatically update `screener_pool.json` from Wikipedia index lists.
*   **Acceptance Criteria**:
    - Liquidity filter successfully ignores illiquid tickers.
    - `screener_pool.json` loads correctly and is written automatically via the update script.
    - Technical scoring output is sorted descending, returning the top N candidates.
*   **Sub-tasks**:
    - `[DONE] AT-3.1 [DEV]`: Create `core/screener.py` technical scoring pipeline and Wikipedia index scraping helper.
    - `[DONE] AT-3.2 [REVIEWER]`: Verify quantitative mathematical weights and sorting.
*   **Comments**:
 
#### **AT-4: SQLite Feedback Loop & Watchlist Logging**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 5 Story Points
*   **Description**: Connect `core/screener.py` to `trading_agent.db`. Extract historical trade outcomes per ticker. Multiply technical scores by a **Booster Factor** (e.g. `1.2x`) for win rates $> 60\%$ or a **Soft Penalty Factor** (e.g. `0.7x`) for win rates $< 40\%$. Log final chosen watchlist to a new database table `watchlist_history`.
*   **Acceptance Criteria**:
    - Queries database safely without blocking active trading threads.
    - Correctly applies a soft `0.7x` penalty instead of a hard blacklist.
    - Correctly handles new tickers with zero trade history (defaults to neutral weight `1.0`).
    - Creates and migrates `watchlist_history` table automatically on start.
*   **Sub-tasks**:
    - `[DONE] AT-4.1 [DEV]`: Code SQLite history lookup and soft penalty weight adjustments.
    - `[DONE] AT-4.2 [REVIEWER]`: Stress test database query locks and migrate schema safely.
*   **Comments**:

#### **AT-5: Integrate Watchlist Screener into Runner Loop**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Integrate the screener into `runner.py`. In each 15-minute cycle, run the screener to pick the top 3-5 watchlist tickers, and pass *only* these tickers to the `TradingBrain` to formulate trading actions.
*   **Acceptance Criteria**:
    - Runner successfully overrides static universe with screened watchlist.
    - Generates compact, focused prompts for Gemini, lowering token latency.
*   **Sub-tasks**:
    - `[DONE] AT-5.1 [DEV]`: Update `runner.py` execution sequence.
    - `[DONE] AT-5.2 [REVIEWER]`: Conduct end-to-end dry-run tests in Mock Mode.
*   **Comments**:

---

### 🎯 Epic 3: VWAP Indicator Integration [AT-EP3]
> **Goal**: Add VWAP as a high-conviction decision data point for support/resistance, mean reversion, and execution quality auditing.

#### **AT-6: Add Dynamic Intraday VWAP Calculation to DataProvider**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Implement intraday resetting VWAP and VWAP Standard Deviation Bands in `DataProvider._add_technical_indicators`.
*   **Acceptance Criteria**:
    - VWAP resets to current open typical price on the first bar of each calendar day.
    - Computes upper and lower standard deviation bands ($\pm 1\sigma$ and $\pm 2\sigma$).
*   **Sub-tasks**:
    - `[DONE] AT-6.1 [DEV]`: Write dynamic daily rolling VWAP algorithm in Pandas.
    - `[DONE] AT-6.2 [REVIEWER]`: Verify calculation correctness using static unit test vectors.
*   **Comments**:

#### **AT-7: VWAP-Based Prompts & Strategist Integration**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Update `TradingBrain` prompt template and `MetaStrategist` guidelines to pass VWAP price distance indicators and establish rules based on VWAP pullbacks or deviations.
*   **Acceptance Criteria**:
    - Prompt safely accepts and displays VWAP indicators.
    - LLM output structured JSON conforms to schema when parsing new indicators.
*   **Sub-tasks**:
    - `[DONE] AT-7.1 [DEV]`: Update `TradingBrain` prompt context and Pydantic models.
    - `[DONE] AT-7.2 [REVIEWER]`: Audit LLM response schema parsing resilience.
*   **Comments**:

---

### 🎯 Epic 4: End-of-Day Discord Reports [AT-EP4]
> **Goal**: Provide automated daily Slack-style Discord performance alerts to monitor the paper portfolio's success.

#### **AT-8: Create Discord Notification Client**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 2 Story Points
*   **Description**: Implement `core/discord_notifier.py` to transmit messages and rich color-coded embeds to the Discord webhook retrieved from `.env`.
*   **Acceptance Criteria**:
    - Sends rich messages containing color codes (Green for PnL $> 0.0$, Red for PnL $\le 0.0$).
    - Gracefully catches connection errors without halting the parent application.
*   **Sub-tasks**:
    - `[DONE] AT-8.1 [DEV]`: Build webhook client using `requests`.
    - `[DONE] AT-8.2 [REVIEWER]`: Validate embed layouts and check payload sizes.
*   **Comments**:

#### **AT-9: Daily Performance Auditor & CLI Integration**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Build a database query to calculate the daily total trade counts, tickers traded, individual trade PnLs, and overall net equity change. Integrate `--eod-report` command-line argument into `runner.py`.
*   **Acceptance Criteria**:
    - Running `python runner.py --eod-report` compiles and sends the Discord notification.
    - Handles holidays and weekend schedules gracefully (no trades executed, sends empty report or holds dispatch).
*   **Sub-tasks**:
    - `[DONE] AT-9.1 [DEV]`: Write daily performance summarizer and add CLI flag.
    - `[DONE] AT-9.2 [REVIEWER]`: Verify timezone alignments (Eastern Time) for daily closing balances.
*   **Comments**:

---

### Epic 5: Cloud Resilience, Weekend Smart Sleep & Discord Kill Switch [AT-EP5]
> Goal: Optimize cloud runner resources by sleeping when idle on weekends and implementing a GCS-based Discord kill switch.

#### **AT-10: Weekend Smart Sleep State Controller**
*   **Status**: `[ ] TO DO`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Implement timezone-aware Friday close detection in `runner.py` (Friday 16:30 ET - Monday 09:00 ET). Check for active crypto holdings. If none, write `.weekend_skip.json` to skip runs. Trigger a Discord notification of hibernation.
*   **Acceptance Criteria**:
    - Detects Friday close (after 16:30 ET) and weekend window correctly.
    - Correctly identifies active crypto holdings (symbols with `/`, `USD`, or `SOL`).
    - Writes `.weekend_skip.json` and skips further cycles when holdings are empty.
    - Auto-deletes skip file on Monday morning.
*   **Sub-tasks**:
    - `[ ] AT-10.1 [DEV]`: Code timezone-aware weekend check and state persistence.
    - `[ ] AT-10.2 [REVIEWER]`: Verify weekend transition boundary edge-cases with Mock clock.

#### **AT-11: Integrate GCS Kill Switch Check in Runner**
*   **Status**: `[ ] TO DO`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: In `runner.py`, add a check at the start of `run_trading_cycle` to read `kill_switch.json` from `GCS_BUCKET_NAME`. If state is `"HALTED"`, exit immediately.
*   **Acceptance Criteria**:
    - Reads kill switch from GCS cleanly with local fallback if GCS is unavailable.
    - Halts trading loop if state is `"HALTED"`.
*   **Sub-tasks**:
    - `[ ] AT-11.1 [DEV]`: Implement GCS blob downloader and halt check.
    - `[ ] AT-11.2 [REVIEWER]`: Code-review safety logic and test failure recovery.

#### **AT-12: Implement Discord Kill Switch Commands in Dexter Bot**
*   **Status**: `[ ] TO DO`
*   **Type**: Story | **Estimate**: 3 Story Points
*   **Description**: Add `!agentkill`, `!agentresume`, and `!agentstatus` commands to `dexter-trader/core/discord_bot.py`.
*   **Acceptance Criteria**:
    - Authorizes only the predefined Discord Owner ID.
    - Modifies `kill_switch.json` on GCS (`agenttrade-us-data-bucket`) correctly.
*   **Sub-tasks**:
    - `[ ] AT-12.1 [DEV]`: Extend discord_bot.py commands.
    - `[ ] AT-12.2 [REVIEWER]`: Execute end-to-end testing from Discord interface.

#### **AT-13: Enhance Streamlit Dashboard with Status Badges**
*   **Status**: `[ ] TO DO`
*   **Type**: Story | **Estimate**: 2 Story Points
*   **Description**: Update `agent-trade/dashboard/dashboard.py` to check for active weekend skip or GCS kill switch states, and display color-coded status badges on the control panel.
*   **Acceptance Criteria**:
    - Dashboard reads skip file and kill switch state in its background status cache.
    - UI updates in real-time with "HIBERNATING" or "HALTED" status badges.
*   **Sub-tasks**:
#### **AT-14: Daily Morning & Evening Discord Status Report Cadence**
*   **Status**: `[ ] TO DO`
*   **Type**: Story | **Estimate**: 2 Story Points
*   **Description**: In `runner.py`, add automated timezone-aware morning (09:00 ET) and evening (16:30 ET) Discord report triggers. Post account equity, cash, and open position summaries with friendly human-readable greetings and custom weekend sleep notifications.
*   **Acceptance Criteria**:
    - Dispatches a starting morning digest at 09:00 ET with starting portfolio metrics.
    - Dispatches an evening digest at 16:30 ET with closing metrics.
    - On Friday evening at 16:30 ET, dynamically appends weekend sleep or active crypto desk details.
*   **Sub-tasks**:
    - `[ ] AT-14.1 [DEV]`: Program status report formatter and timing trigger hooks in runner.py.
    - `[ ] AT-14.2 [REVIEWER]`: Verify timezone formatting, message styling, and dynamic weekend notifications.

---

### Epic 6: Broker Synchronization, Watchlist Alignment & Curve Backfilling [AT-EP6]
> **Goal**: Harmonize guardrails with dynamic watchlists and expand portfolio metrics and trade execution history tracking starting from July 1 forward.

#### **AT-15: Risk Guardrail Watchlist Integration**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 2 Story Points | **Key**: `TMCL-696`
*   **Description**: Refactor guardrails in `core/guardrails.py` to permit trading of any symbol that is present either in `config.TRADING_UNIVERSE` or on the latest dynamic screened watchlist.
*   **Acceptance Criteria**:
    - Symbol COP is permitted when it is in the latest watchlist.
    - Non-watchlist and non-universe symbols are rejected.
    - Normalization handles `SOL/USD` vs `SOLUSD` cleanly.
*   **Sub-tasks**:
    - `[DONE] TMCL-697 [DEV]`: Implement database query helper and refactor `core/guardrails.py`.
    - `[DONE] TMCL-698 [REVIEWER]`: Verify watchlist validation with unit tests.

#### **AT-16: Rebuilt Ledger Database Backfill Extension**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 2 Story Points | **Key**: `TMCL-699`
*   **Description**: Modify `rebuild_ledger.py` to backfill the entire day-over-day cash/equity history from July 1st forward into `portfolio_history`.
*   **Acceptance Criteria**:
    - Runs successfully and inserts all dates from July 1st into `portfolio_history`.
    - Dashboard chart loads complete historical curve.
*   **Sub-tasks**:
    - `[DONE] TMCL-700 [DEV]`: Update `rebuild_ledger.py` database backfill loop.
    - `[DONE] TMCL-701 [REVIEWER]`: Validate table consistency and verify dashboard load.

#### **AT-17: Historical Broker Trade Syncer**
*   **Status**: `[DONE]`
*   **Type**: Story | **Estimate**: 3 Story Points | **Key**: `TMCL-702`
*   **Description**: Extract historic completed fills from Alpaca activity logs and populate the database `trades` table.
*   **Acceptance Criteria**:
    - Re-running the ledger rebuilder populates `trades` cleanly.
    - "Broker-Side Executed Orders" panel displays all filled orders.
*   **Sub-tasks**:
    - `[DONE] TMCL-703 [DEV]`: Code Activities API parser and database populator.
    - `[DONE] TMCL-704 [REVIEWER]`: Verify trade history counts and check for duplicate rows.

---

## 🧪 Testing & Validation Protocols

Before declaring any ticket complete, the following quality checks must be passed:

1.  **Static Code Analysis**: All files must pass `ruff` / `flake8` lints and comply with `antigravity` styling parameters.
2.  **Mock Mode Dry-Runs**: Run execution loops offline using Mock Mode:
    ```bash
    python runner.py --once --dry-run
    ```
    Verify no exceptions occur and indicators compile correctly.
3.  **Unit Regression Suite**: Execute the regression tests to confirm zero broken features:
    ```bash
    pytest tests/
    ```
4.  **Audit DB Integrity**: Run diagnostic checks on `trading_agent.db` using sqlite tools to confirm `watchlist_history` is written correctly and trades register expected timestamps.

---
