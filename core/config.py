import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file (override pre-existing environment variables)
load_dotenv(override=True)

# Risk Management Parameters
MAX_TRADE_ALLOCATION_PCT = 0.10  # Max % of equity per trade
MAX_TICKER_ALLOCATION_PCT = 0.30  # Max % of equity per ticker (new)
DAILY_LOSS_LIMIT_PCT = 0.05  # Max daily equity drawdown before blocking buys
MIN_CASH_BUFFER_PCT = 0.20  # Minimum cash reserve % of equity
MIN_SELL_VALUE = float(os.getenv("MIN_SELL_VALUE", "50.0"))  # Min $ value of a SELL position; dust below this is fully liquidated

# Correlation / Concentration Guardrail
# Groups symbols whose returns are highly correlated. The guardrail caps the
# TOTAL dollar exposure (sum of current position values + any proposed buy) to
# MAX_CLUSTER_ALLOCATION_PCT of equity per cluster, so the portfolio can't
# become an oversized, undiversified bet on one correlated theme (e.g. a
# crypto-heavy book of BTC/ETH/SOL all moving together).
MAX_CLUSTER_ALLOCATION_PCT = float(os.getenv("MAX_CLUSTER_ALLOCATION_PCT", "0.40"))
# Cluster membership keyed by canonical upper symbol (crypto uses slash form).
# Symbols with no listed cluster are treated as their own singleton cluster.
CORRELATION_CLUSTERS = {
    "CRYPTO": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "DOGE/USD"],
    "TECH_MEGACAP": ["AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "AVGO", "INTC", "QCOM", "TXN"],
    "FINANCIALS": ["JPM", "BAC", "GS", "MS", "WFC"],
    "BROAD_ETFS": ["SPY", "QQQ", "DIA", "IWM"],
}

# Per-Ticker Loss / Whipsaw Circuit Breaker
# Blocks NEW BUYs on a symbol that has repeatedly lost money, so the strategy
# stops re-entering names it keeps bleeding on (e.g. INTC/AMD/SPY in the data).
# - MAX_CONSECUTIVE_LOSSES: if the symbol's most recent N closed round-trips are
#   ALL losses, block new BUYs (circuit breaker).
# - MAX_WHIPSAW_RATIO: if the symbol's share of <4h round-trips exceeds this AND
#   it has at least MIN_WHIPSAW_TRADES closed trades, block new BUYs (whipsaw trap).
# - CIRCUIT_BREAKER_LOOKBACK_DAYS: window over which round-trips are considered.
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MAX_WHIPSAW_RATIO = float(os.getenv("MAX_WHIPSAW_RATIO", "0.60"))
MIN_WHIPSAW_TRADES = int(os.getenv("MIN_WHIPSAW_TRADES", "4"))
CIRCUIT_BREAKER_LOOKBACK_DAYS = int(os.getenv("CIRCUIT_BREAKER_LOOKBACK_DAYS", "90"))
# Low win-rate circuit breaker: if a symbol has MIN_LOW_WIN_RATE_TRADES closed RTs
# in the lookback AND its realized win rate stays below MAX_LOW_WIN_RATE, block new
# BUYs. This catches chronic losers (e.g. KO at 0% and MS at 17% win rate) that
# never string 3 *consecutive* losses together but still bleed on net. SELLs to
# de-risk remain allowed.
MIN_LOW_WIN_RATE_TRADES = int(os.getenv("MIN_LOW_WIN_RATE_TRADES", "5"))
MAX_LOW_WIN_RATE = float(os.getenv("MAX_LOW_WIN_RATE", "0.25"))

# Crypto TP/SL Bracket Support
# When True, crypto BUYs get a bracket (take-profit + stop-loss) order like
# equities, so 24/7 crypto positions aren't left running unhedged. If Alpaca
# rejects the bracket for a crypto symbol, the client falls back to a plain
# market order (no TP/SL) so trading is never blocked.
CRYPTO_BRACKET_ENABLED = os.getenv("CRYPTO_BRACKET_ENABLED", "true").lower() == "true"
# Default TP/SL percentages for crypto BUYs when the brain doesn't supply them.
CRYPTO_TAKE_PROFIT_PCT = float(os.getenv("CRYPTO_TAKE_PROFIT_PCT", "0.05"))
CRYPTO_STOP_LOSS_PCT = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.03"))

# Volatility-Based Position Sizing
# When True, BUY quantities are scaled down for high-volatility assets so the
# same dollar *risk* is taken regardless of asset. Uses ATR% (from data_provider)
# relative to a baseline: a symbol with ATR% above VOL_SIZING_BASELINE_ATR_PCT
# gets its max allocation scaled by (baseline / atr_pct), floored at
# VOL_SIZING_MIN_ALLOCATION_PCT of equity.
VOL_SIZING_ENABLED = os.getenv("VOL_SIZING_ENABLED", "true").lower() == "true"
VOL_SIZING_BASELINE_ATR_PCT = float(os.getenv("VOL_SIZING_BASELINE_ATR_PCT", "2.0"))
VOL_SIZING_MIN_ALLOCATION_PCT = float(os.getenv("VOL_SIZING_MIN_ALLOCATION_PCT", "0.02"))

# Intra-Day PnL Circuit Breaker
# Blocks NEW BUYs when the day's realized + unrealized PnL drops below
# INTRADAY_LOSS_LIMIT_PCT of equity (a real-time, intra-cycle version of the
# daily loss limit). SELLs are still allowed to de-risk. Uses the day's realized
# PnL from the DB (FIFO) plus current unrealized PnL from the account.
INTRADAY_LOSS_LIMIT_PCT = float(os.getenv("INTRADAY_LOSS_LIMIT_PCT", "0.04"))
INTRADAY_BREAKER_ENABLED = os.getenv("INTRADAY_BREAKER_ENABLED", "true").lower() == "true"

# Universe Guardrail
# When STRICT_UNIVERSE_ENABLED is True, a BUY to a symbol that is NOT in the
# latest screened watchlist is blocked UNLESS the symbol is currently held (so
# we can always manage/SELL it) or is crypto. This closes the "fallback-universe"
# hole that let untracked names like SPY/QQQ/TSLA/MS (static TRADING_UNIVERSE
# members never picked by the screener) keep getting bought and bleeding
# (-$540 across the equity desk from untracked symbols).
# Held positions remain tradable (SELL to exit, and a small BUY to top-up is
# still allowed because we must be able to manage an existing position).
STRICT_UNIVERSE_ENABLED = os.getenv("STRICT_UNIVERSE_ENABLED", "true").lower() == "true"

# Intraday VWAP gating
# VWAP is cumulative within the current trading day. Early in the session there
# are too few intraday bars for VWAP (and its bands / dist_pct) to be meaningful:
# with a single bar, vwap == typical_price and vwap_dist_pct collapses to ~0%,
# which the brain can mistake for "price hugging VWAP" confluence. To avoid this
# false signal, VWAP-derived fields are exposed as None until the current day has
# at least MIN_VWAP_BARS intraday bars.
MIN_VWAP_BARS = int(os.getenv("MIN_VWAP_BARS", "4"))
BRAIN_MODEL_TIER = os.getenv("BRAIN_MODEL_TIER", "daily_driver")
STRATEGIST_MODEL_TIER = os.getenv("STRATEGIST_MODEL_TIER", "heavyweight")
# Max output tokens for the brain's per-ticker decision JSON. The brain emits one
# verbose decision (with a long thought_process) per appraised ticker, so the
# default 2048-token cap truncates the JSON mid-response and forces a rule-based
# fallback. Bump this well above the expected response size.
BRAIN_MAX_OUTPUT_TOKENS = int(os.getenv("BRAIN_MAX_OUTPUT_TOKENS", "8192"))

# Options Trading Configuration
# Kill-switch: when False, the brain never outputs option decisions and guardrails reject them.
OPTIONS_ENABLED = os.getenv("OPTIONS_ENABLED", "false").lower() == "true"
# Curated liquid options universe (long calls/puts only). Subset of the most actively-traded options.
OPTIONS_UNIVERSE = [
    "NVDA", "TSLA", "AAPL", "SPY", "QQQ",
    "AMZN", "MSFT", "META", "GOOGL", "AMD",
]
# Default DTE (days-to-expiry) window for option selection.
OPTIONS_DTE_MIN = int(os.getenv("OPTIONS_DTE_MIN", "30"))
OPTIONS_DTE_MAX = int(os.getenv("OPTIONS_DTE_MAX", "45"))
# Hard safety bounds on DTE. The agent may override within these bounds only.
OPTIONS_DTE_HARD_MIN = int(os.getenv("OPTIONS_DTE_HARD_MIN", "14"))
OPTIONS_DTE_HARD_MAX = int(os.getenv("OPTIONS_DTE_HARD_MAX", "90"))
# Max % of equity allocated to a single option position (cost = ask * 100 * contracts).
OPTIONS_MAX_ALLOCATION_PCT = float(os.getenv("OPTIONS_MAX_ALLOCATION_PCT", "0.05"))
# Max number of option contracts per ticker.
OPTIONS_MAX_CONTRACTS_PER_TICKER = int(os.getenv("OPTIONS_MAX_CONTRACTS_PER_TICKER", "5"))
# Conviction threshold (0.0-1.0): conviction >= threshold routes to the option path (leverage).
# Below threshold routes to the stock path. Deterministic mapping prevents stock<->option whipsaw.
OPTIONS_CONVICTION_THRESHOLD = float(os.getenv("OPTIONS_CONVICTION_THRESHOLD", "0.7"))
# Auto-close: close option positions when DTE <= this value to avoid exercise/assignment.
OPTIONS_AUTO_CLOSE_DTE = int(os.getenv("OPTIONS_AUTO_CLOSE_DTE", "3"))
# OTM% window for strike selection (1% - 10% out-of-the-money).
OPTIONS_OTM_PERCENT_MIN = float(os.getenv("OPTIONS_OTM_PERCENT_MIN", "0.01"))
OPTIONS_OTM_PERCENT_MAX = float(os.getenv("OPTIONS_OTM_PERCENT_MAX", "0.10"))

# Max number of executable (non-HOLD) decisions allowed per cycle.
# Staged rollout: start at 1 (mirrors old single-decision behavior), then raise
# to 3-5 once multi-decision behavior is trusted on paper.
MAX_TRADES_PER_CYCLE = int(os.getenv("MAX_TRADES_PER_CYCLE", "1"))

# Trading Universe
TRADING_UNIVERSE = [
    "SPY", "QQQ", "DIA", "IWM",  # ETFs
    "AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA",  # Tech
    "JPM", "BAC", "GS", "MS",  # Financials
    "SOL/USD", "BTC/USD", "ETH/USD", "XRP/USD"  # Crypto
]

# API Configuration
API_KEY = os.getenv("TRADING_API_KEY")
API_SECRET = os.getenv("TRADING_API_SECRET")
API_BASE_URL = os.getenv("TRADING_API_URL", "https://api.alpaca.markets")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", API_KEY)
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", API_SECRET)
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
LOG_FILE = os.getenv("LOG_FILE", "trading_agent.log")

# Base Directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Database Path
DB_PATH = BASE_DIR / "data" / "trades.db"

db_filename = os.getenv("DATABASE_FILENAME", "trading_agent.db")
if Path(db_filename).is_absolute():
    DATABASE_PATH = Path(db_filename)
else:
    DATABASE_PATH = BASE_DIR / db_filename

# Screener Configuration
SCREENER_POOL_PATH = BASE_DIR / "screener_pool.json"

# Dashboard & Security Configurations
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_SALT = os.getenv("SESSION_SALT", "age_desk_secure_salt_change_me")
TRADING_INTERVAL_MINUTES = int(os.getenv("TRADING_INTERVAL_MINUTES", "15"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# LLM & Gemini configurations
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# LLM Configurations
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()

# Jira Configurations
JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")

# GCS Configurations
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

def is_config_valid() -> tuple[bool, str]:
    """Helper to check if configuration has all necessary components."""
    if not ALPACA_API_KEY or ALPACA_API_KEY == "your_alpaca_api_key_here":
        return False, "Alpaca API Key is missing or default."
    if not ALPACA_SECRET_KEY or ALPACA_SECRET_KEY == "your_alpaca_secret_key_here":
        return False, "Alpaca Secret Key is missing or default."
    if LLM_PROVIDER == "gemini":
        if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
            return False, "Gemini API Key is missing."
    return True, "Config is valid."
