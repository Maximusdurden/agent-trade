# Autonomous Alpaca AI Trading Agent

An autonomous, self-contained AI-powered paper trading agent built to trade stock indices (like **SPY** or **QQQ**) using the official **Alpaca API** and **Google Gemini**.

It includes a native, zero-setup **Mock Mode** fallback so you can try out the trading cycle and audit logs completely offline before inputting any API keys!

---

## Features

- **Modular Architecture**: Separate layers for Alpaca integration, data provider, guardrails, LLM brain, and database tracking.
- **Mock Fallback**: Runs automatically in Mock Mode if API keys or dependencies are missing, allowing instant testing.
- **Hard Risk Guardrails**: Deterministic guardrail layer that prevents buying with insufficient cash, restricts trade allocation size (max 10% of equity), enforces a daily loss threshold (2%), and prevents shorting.
- **Indicator Engine**: Custom calculations of technical indicators like SMA 20, SMA 50, RSI 14, MACD, and Bollinger Bands using Pandas—avoiding raw coordinate calculations by the LLM.
- **SQLite Audit DB**: Automatically records every market condition, LLM reasoning thought process, final decision, and filled order details for retro-analysis.
- **Flexible Execution**: Command-line arguments supporting single-cycle dry runs, live single executions, or continuous loop operations.

---

## File Structure

```text
agent-trade/
├── .env                  # Private credentials (git-ignored)
├── .env.template         # Placeholder file for environment variables
├── .gitignore            # Git ignore definitions
├── .agyrule              # Developer agent rule guidelines
├── requirements.txt      # Python dependencies
├── runner.py             # Core orchestrator and entry point for trading cycles
│
├── core/                 # Core Trading Engine and LLM Brain
│   ├── config.py         # Loads configurations and sets safety values
│   ├── database.py       # SQLite database schemas and logging utilities
│   ├── alpaca_client.py  # Alpaca API interface (with offline Mock fallback)
│   ├── data_provider.py  # Historical candle fetcher & technical indicator engine
│   ├── guardrails.py     # Deterministic safety risk checker and trade sizer
│   ├── trading_brain.py  # Formulates prompts and requests Gemini AI structured decisions
│   ├── strategist.py     # Generates daily trading guidelines using Gemini
│   └── logger_setup.py   # Application-wide logger configuration
│
├── dashboard/            # Web Monitoring and Visualization
│   └── dashboard.py      # Streamlit-based monitoring dashboard
│
├── deploy/               # Deployment Automation
│   └── create_task.ps1   # PowerShell Windows Scheduled Task registrar
│
├── tools/                # Administrative Utilities
│   └── get_correct_balances.py  # Alpaca cash ledger & balance backfill tool
│
└── tests/                # Code Quality and Sanity Verification Tests
    ├── test_alpaca.py    # Alpaca connectivity test script
    ├── test_db.py        # Database query verification script
    ├── test_fetch.py     # Dashboard HTTP status check script
    └── test_gemini.py    # Gemini LLM models diagnostic script
```

---

## Quick Start Guide

### 1. Set Up Environment
Create a Python virtual environment and install the required dependencies:

```bash
# Create a virtual environment
python -m venv venv

# Activate it (Windows)
.\venv\Scripts\activate

# Install libraries
pip install -r requirements.txt
```

### 2. Run a Mock Offline Dry-Run (No API Keys Needed!)
To see the system in action immediately without setting up any accounts, run:

```bash
python runner.py
```

*What happens under the hood?*
1. It detects missing API keys/libraries and starts in **Mock Mode** with a fake `$100,000` paper balance.
2. It generates mock market data for SPY and QQQ.
3. It triggers a rule-based quantitative strategy fallback to propose a trade.
4. It passes the decision through the **Risk Guardrails** to validate trade size.
5. It records the entire cycle into a local SQLite database (`trading_agent.db`).

---

### 3. Connect to Alpaca & Gemini

1. Create a free **Alpaca** account and grab your API keys from your [Alpaca Dashboard](https://app.alpaca.markets/). Ensure you are in the **Paper Trading** view.
2. Create a free **Google Gemini** API key from [Google AI Studio](https://aistudio.google.com/).
3. Open the `.env` file in the `agent-trade` folder and paste your keys:

```ini
ALPACA_API_KEY=your_actual_alpaca_key_here
ALPACA_SECRET_KEY=your_actual_secret_key_here
ALPACA_PAPER=True

LLM_PROVIDER=gemini
GEMINI_API_KEY=your_actual_gemini_key_here
GEMINI_MODEL=gemini-2.5-flash
```

---

### 4. Running the Live Agent

Once keys are in `.env`, run the agent using these terminal options:

#### A. Single Dry-Run Cycle (Safest First test)
Fetches live Alpaca data, computes real indicators, prompts Gemini, runs through guardrails, but **skips** sending the final order to Alpaca.
```bash
python runner.py --once --dry-run
```

#### B. Single Live Trade Execution
Executes the cycle once and places a real order in your Alpaca paper account if the decision is approved by guardrails.
```bash
python runner.py --once
```

#### C. Continuous Auto-Trading Loop
Enters a background loop, running the trading analysis cycle every 15 minutes (or as configured in `config.py`):
```bash
python runner.py --loop
```

---

## Safety and Risk Guardrails

To prevent the LLM from making erratic or high-risk trading decisions, the deterministic `guardrails.py` enforces these rules:
* **Max Allocation Size**: No single trade value can exceed **10% of total portfolio equity**. If the LLM requests more, the guardrails automatically **scale down** the share quantity to the maximum allowed safe value instead of failing.
* **Daily Drawdown Limit**: If portfolio value drops **2%** from the starting equity, all buying activities are blocked. Only sell orders (to liquidate/risk-reduce) are allowed.
* **Cash Buffer**: The agent is blocked from buying if cash falls below a **5% cash reserve cushion** of total equity.
* **Strict Universe**: The agent is blocked from trading any ticker outside of `config.TRADING_UNIVERSE` (`["SPY", "QQQ"]` by default).

---

## Database Auditing

All logs are stored in `trading_agent.db`. You can view them using any SQLite viewer.
* **`decisions`**: Stores every single tick analysis: the calculated RSI/MACD indicators at that moment, the portfolio cash/equity balance, the raw LLM thought process, proposed action, and whether it was approved or rejected by guardrails.
* **`trades`**: Stores actual executions with Alpaca order IDs, filled average price, timestamps, and order status.
* **`portfolio_history`**: Tracks equity, cash, and PnL trends over time to construct charts.

---

## Windows Scheduled Task Deployment (Production)

To run the agent-trade system continuously in production without keeping a terminal open, you can deploy it as a Windows Scheduled Task. 

An automated registrar script is provided in [deploy/create_task.ps1](file:///Z:/python/projects/agent-trade/deploy/create_task.ps1) that registers a task named **`AgentTradeRunner`**.

### How the task operates:
- **Interval:** Executes the trading cycle every **15 minutes, 24 hours a day, 7 days a week**.
- **Weekday Behavior:** Trades both **stocks/ETFs** and **cryptocurrency** during normal market hours (09:00 - 16:30 ET).
- **Off-Hours & Weekend Behavior:** Automatically detects that the US equity market is closed, filters the trading universe to **crypto-only** (`SOL/USD`), and safely continues trading crypto around the clock without attempting off-hours stock trades.

### Automatic Setup (Recommended)

1. Open PowerShell as an **Administrator**.
2. Run the deployment script:
   ```powershell
   Set-ExecutionPolicy Bypass -Scope Process -Force
   .\deploy\create_task.ps1
   ```
3. To manually trigger a run right away to test it:
   ```powershell
   Start-ScheduledTask -TaskName "AgentTradeRunner"
   ```

### Manual Setup via Task Scheduler GUI

If you prefer to configure it manually:
1. Open **Task Scheduler** (`taskschd.msc`).
2. Click **Create Basic Task...** on the right side.
3. Set **Triggers** to **Weekly**, select all days of the week, and set it to repeat every 15 minutes indefinitely.
4. Set **Action** to **Start a program**:
   - **Program/script:** `cmd.exe`
   - **Arguments:** `/c "set BYPASS_MARKET_WINDOW=True&&Z:\python\projects\agent-trade\venv\Scripts\python.exe Z:\python\projects\agent-trade\runner.py --once"`
   - **Start in:** `Z:\python\projects\agent-trade`
   
> [!IMPORTANT]
> When setting environment variables via `cmd.exe /c`, ensure there are **no spaces** around the `&&` separator (e.g., `BYPASS_MARKET_WINDOW=True&&Z:\python\...`). Otherwise, Windows will append a trailing space to the variable value, preventing Python from correctly reading it!

---

## 🚀 Upcoming Sprint Enhancements & Roadmap

We are currently executing an optimization sprint alongside our subagents to introduce advanced quant capabilities, broaden our market reach, and establish automated reporting. Progress is tracked via [sprint_plan.md](sprint_plan.md).

### 1. Broadened Universe & Batch Processing
We are transitioning `agent-trade` from a small static list of 10 tickers to a scalable **Screener Candidate Pool of 50-100 high-liquidity stocks and crypto**. To do this without hitting Alpaca API rate limits, we are implementing batch historical data fetching and high-speed group-by indicator computation in Pandas.

### 2. Autonomous Dynamic AI Screener
A new multi-stage screening pipeline (`core/screener.py`) will automatically execute at the beginning of each cycle to select the top 3-5 candidates for the active watchlist:
- **Liquidity filtering**: Filters out illiquid assets.
- **Technical scoring**: Ranks companies based on Mean Reversion (Bollinger Bands/RSI) and Momentum (SMA/MACD) setups.
- **SQLite Performance Feedback Loop**: Queries past trade outcomes to apply a booster (`1.2x`) for tickers with win rates $>60\%$, or a soft penalty (`0.7x`) for tickers with win rates $<40\%$.

### 3. Intraday VWAP Indicators
We are adding Volume Weighted Average Price (VWAP) and VWAP standard deviation bands as core technical indicators to enable:
- **Intraday Support & Resistance**: Finding key confluences for entry.
- **Mean Reversion boundaries**: Identifying overbought and oversold thresholds.
- **Execution Quality tracking**: Verifying we buy below VWAP and sell above VWAP.

### 4. End-of-Day Discord Reports
An independent command-line interface `--eod-report` will be executed once daily as a Scheduled Task at **16:35 ET** (weekdays) to deliver Slack-style Discord performance alerts:
- **Message A (Daily Summary)**: Color-coded embed reporting daily buys, sells, realized PnL, and net equity shifts (Green for gain, Red for loss).
- **Message B (Detailed Breakdown)**: Chronological table of tickers traded and individual PnL outcomes for the day.

