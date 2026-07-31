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
