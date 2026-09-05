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
- **Automated Daily Blog (Treat Motivated Capital)**: Ported from `dexter-trader`, the blog publisher turns each day's round-trips into a polished WordPress post (market intro, per-ticker blurbs, a Dexter performance card, and a yearly calendar). The voice is **persona-swappable** — change `BLOG_PERSONA` and the next post is written in a completely different voice with no code edits. See [docs/blog_and_personas.md](docs/blog_and_personas.md).

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
│   └── dashboard.py      # Autonomously serves live Alpaca account equity curve
│                         #   + broker-side executed orders (dexter cutover)
│
├── deploy/               # Deployment Automation
│   ├── deploy_cloud.ps1  # Cloud Run job + Cloud Scheduler deploy
│   ├── deploy_blog.ps1   # Blog Cloud Run job + scheduler deploy
│   ├── deploy_dashboard.ps1 # Cloud Run dashboard deploy (reads dexter creds from .env)
│   ├── create_task.ps1   # PowerShell Windows Scheduled Task registrar
│   ├── Dockerfile.blog   # Blog image (entrypoint run_blog.py)
│   ├── cloudbuild_blog.yaml # Blog cloud build config
│   └── _create_blog_secrets.py # Creates WP/LLM Secret Manager secrets
│
├── docs/                 # Technical documentation & incident notes
    ├── blog_and_personas.md        # Full blog layer + how to make your own persona
    ├── strategist_model_ab.md      # strategist model A/B experiment (r1 vs Sonnet)
│   ├── equity_desk_guardrails.md   # the 2026-09-02 loss-guardrail design
│   ├── held_positions_universe_guardrail_fix.md
│   └── fractional_shares_and_jira_logging_fix.md
│
├── feedback/             # Agent-facing "do-not-do-X" learning briefs
│   └── equity_lessons.md # equity-desk loss playbook for Screener/MetaStrategist
│
├── tools/                # Administrative Utilities
│   ├── blog_update.py         # Dexter blog orchestrator (pull→bridge→grade→publish)
│   ├── build_blog_db.py       # bridge: agent-trade round-trips → realized_trades mirror
│   ├── publish_strategy_change.py # one-off notice-post publisher
│   ├── liquidate_account.py   # dry-run-by-default account liquidation tool
│   ├── equity_trade_forensics.py  # read-only loss forensics (equity desk)
│   ├── pull_cloud_db.py           # pull authoritative cloud DB from GCS
│   └── get_correct_balances.py    # Alpaca cash ledger & balance backfill tool
│
├── core/                 # (blog support lives alongside the trading engine)
│   ├── personas.py       # swappable blog voices (dexter/oracle/rookie/pirate/derrick)
│   ├── brain.py          # persona-swappable LLM layer (intro + ticker blurbs)
│   ├── wordpress.py      # WordPress REST publisher + sidebar/calendar/Discord
│   ├── cards.py          # performance card + yearly calendar + sidebar image
│   ├── grader.py         # D-TAG trade grading (letter grades on posts)
│   ├── blog_stats.py     # dashboard/round-trip stats → dexter DataFrame
│   └── seo.py            # per-post SEO audit + metadata generation
│
├── run_blog.py           # Blog Cloud Run job entrypoint (publishes daily post)
│
└── tests/                # Code Quality and Sanity Verification Tests
    ├── test_universe_guardrail.py # strict-universe guardrail tests
    ├── test_anti_scale_in.py      # anti-averaging-down guardrail tests
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
* **Strict Universe**: The agent is blocked from trading any ticker outside of `config.TRADING_UNIVERSE` (`["SPY", "QQQ"]` by default), the latest screener watchlist, or a currently-held position.

### Equity-Desk Loss Guardrails (added 2026-09-02)

Forensics on realized round-trips (7/7 → 8/31) showed the equity desk lost money in three
distinct patterns: (1) the fallback/static-universe path kept **buying untracked names** the
screener never endorsed (-$540, 8.6% win rate on names like SPY/QQQ/AMD/INTC/TSLA that never
appeared in the watchlist), (2) the agent **averaged down** into a held losing position
(MS dip-add, -$226), and (3) it kept **re-entering chronic losers** with very low win rates
(KO 0%, MS 17%). Three deterministic guardrails now close these holes:

* **Strict-Universe Guardrail** (`STRICT_UNIVERSE_ENABLED`, default `true`): a NEW buy is
  blocked unless the symbol is in the latest screener watchlist, is crypto, or is currently
  held. The fallback path can no longer open positions in names the screener never picked.
* **Anti-Scale-In Guardrail**: blocks **adding** to a held position when the current price is
  below its average entry price (averaging down = falling-knife accumulation). Crypto is exempt.
* **Low Win-Rate Circuit Breaker** (`MIN_LOW_WIN_RATE_TRADES=5`, `MAX_LOW_WIN_RATE=0.25`):
  if a symbol has ≥5 closed round-trips in the lookback window with a realized win rate below
  25%, new BUYs are blocked even without a string of consecutive losses.

These work alongside the existing per-ticker (consecutive-loss + whipsaw-trap) and intra-day
PnL circuit breakers. The MetaStrategist is also told, via structured feedback, to **rewrite**
(not restate) rules for any "chronic loser" symbol. See
[docs/equity_desk_guardrails.md](docs/equity_desk_guardrails.md) for details and tuning.

---

## Database Auditing

All logs are stored in `trading_agent.db`. You can view them using any SQLite viewer.
* **`decisions`**: Stores every single tick analysis: the calculated RSI/MACD indicators at that moment, the portfolio cash/equity balance, the raw LLM thought process, proposed action, and whether it was approved or rejected by guardrails.
* **`trades`**: Stores actual executions with Alpaca order IDs, filled average price, timestamps, and order status.
* **`portfolio_history`**: Tracks equity, cash, and PnL trends over time to construct charts.

---

## Daily Blog & Personas (Treat Motivated Capital)

Agent-trade includes a **portable, persona-swappable daily blog publisher** that
turns each day's round-trips into a WordPress post on
[Treat Motivated Capital](https://treatmotivated.capital). It was ported from
the retired `dexter-trader` project so the blog "keeps Dexter's voice" while the
data now comes from agent-trade.

### How the blog pipeline works

```
fresh trading_agent.db (GCS)  →  build_blog_db (bridge)  →  grade (D-TAG)
   →  intro + ticker blurbs (LLM)  →  SEO metadata  →  publish to WordPress
   →  sidebar widget  →  performance calendar  →  Discord notify
```

Each voice the blog writes in is just a **system prompt in
[core/personas.py](core/personas.py)** — meaning the tone is a *runtime setting*,
not code. Set `BLOG_PERSONA` and the next post is written in that voice.

### Quick start (local dry-run — builds + grades, does NOT publish)

```powershell
> python -m tools.blog_update --dry --local-db
```

### Real publish

```powershell
> python -m tools.blog_update --local-db          # publish from local DB
> python -m tools.blog_update                     # pull DB from GCS first
```

### Write in YOUR OWN voice

Add a persona key to `PERSONAS` in `core/personas.py`, then set
`BLOG_PERSONA=your_key`. Read
[Using a different persona for your own daily reporting](docs/blog_and_personas.md)
for the full walkthrough, the built-in personas (dexter, oracle, rookie,
pirate, derrick), the branding rule, and the Cloud Run deployment
(`deploy/deploy_blog.ps1`).

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

