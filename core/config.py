import os
from pathlib import Path

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

# Database Path
DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
DATABASE_PATH = Path(__file__).parent.parent / "trading_agent.db"
