import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file (override pre-existing shell/IDE environment variables)
load_dotenv(override=True)

# Project Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_FILENAME", "trading_agent.db")

# Alpaca Credentials & URLs
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() in ("true", "1", "yes")

if ALPACA_PAPER:
    ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
else:
    ALPACA_BASE_URL = "https://api.alpaca.markets"

# LLM Configurations
# Default provider can be 'gemini'. Others can be added later.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Discord Configurations
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Screener Configuration
SCREENER_POOL_PATH = BASE_DIR / "screener_pool.json"

# Secure Dashboard Authentication Settings
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_SALT = os.getenv("SESSION_SALT", "age_desk_secure_salt_change_me")



# Trading Parameters
# The list of tickers we allow our agent to trade. SPY, QQQ, and SOL/USD.
TRADING_UNIVERSE = ["SPY", "QQQ", "SOL/USD", "NVDA", "TSLA", "AMD", "GOOG", "INTC", "MSFT", "XOM"]

# Risk Guardrails
# Maximum percentage of total portfolio equity that can be allocated to any single trade
MAX_TRADE_ALLOCATION_PCT = 0.10  # 10%
# Maximum portfolio daily loss threshold (e.g., stop trading if equity falls 2% from daily open)
DAILY_LOSS_LIMIT_PCT = 0.02  # 2%
# Maximum cash balance buffer to hold (do not let cash go below this percentage of total equity)
MIN_CASH_BUFFER_PCT = 0.10  # 10%

# Timing Config (in minutes)
# How often the runner should execute its trading decision cycle
TRADING_INTERVAL_MINUTES = 15

# Log configuration
LOG_FILE = BASE_DIR / "trading.log"

def is_config_valid() -> tuple[bool, str]:
    """Helper to check if configuration has all necessary components."""
    if not ALPACA_API_KEY or ALPACA_API_KEY == "your_alpaca_api_key_here":
        return False, "Alpaca API Key is missing or default. Please set ALPACA_API_KEY in your .env file."
    if not ALPACA_SECRET_KEY or ALPACA_SECRET_KEY == "your_alpaca_secret_key_here":
        return False, "Alpaca Secret Key is missing or default. Please set ALPACA_SECRET_KEY in your .env file."
    if LLM_PROVIDER == "gemini":
        if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
            return False, "Gemini API Key is missing. Please set GEMINI_API_KEY in your .env file."
    return True, "Config is valid."
