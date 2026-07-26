import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from core import config
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.database import get_db_connection, log_watchlist

logger = logging.getLogger("Screener")

DEFAULT_TICKERS = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "TSLA", "UNH", "JNJ",
    "JPM", "XOM", "V", "PG", "AVGO", "HD", "MA", "LLY", "MRK", "ABBV",
    "PEP", "COST", "KO", "ADBE", "WMT", "MCD", "CSCO", "CRM", "BAC", "ACN",
    "TMO", "NFLX", "PFE", "ORCL", "AMD", "ABT", "NKE", "CMCSA", "DIS", "INTC",
    "CVX", "WFC", "QCOM", "TXN", "MS", "HON", "COP", "AMAT", "VZ", "RTX",
    "VRTX", "NEE", "AMGN", "IBM", "PM", "GE", "UNP", "SPY", "QQQ", "SOL/USD"
]

def load_screener_pool() -> list[str]:
    """Loads the broad candidate pool of tickers from screener_pool.json."""
    pool_path = config.SCREENER_POOL_PATH
    if pool_path.exists():
        try:
            with open(pool_path, "r") as f:
                tickers = json.load(f)
                if isinstance(tickers, list) and tickers:
                    return [t.upper() for t in tickers]
        except Exception as e:
            logger.error(f"Failed to read {pool_path}: {e}")
    logger.info("Falling back to default liquid tickers list.")
    return DEFAULT_TICKERS

def get_symbol_win_rates() -> dict[str, float]:
    """Calculates historical win rate per symbol from closed trades in the SQLite database."""
    win_rates = {}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, side, qty, filled_avg_price 
                FROM trades 
                WHERE status IN ('filled', 'partially_filled') 
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"Could not fetch trade history for screener feedback: {e}")
        return {}

    buy_queues = defaultdict(list)
    closed_pnls = defaultdict(list)

    for row in rows:
        sym = row["symbol"].upper()
        if sym == "SOLUSD":
            sym = "SOL/USD"
        side = row["side"].lower()
        qty = float(row["qty"])
        price = float(row["filled_avg_price"]) if row["filled_avg_price"] else 0.0

        if side == "buy":
            buy_queues[sym].append({"qty": qty, "price": price})
        elif side == "sell":
            temp_qty = qty
            trade_pnl = 0.0
            matched = False
            while temp_qty > 0 and buy_queues[sym]:
                oldest_buy = buy_queues[sym][0]
                buy_qty = oldest_buy["qty"]
                buy_price = oldest_buy["price"]

                if buy_qty <= temp_qty:
                    trade_pnl += buy_qty * (price - buy_price)
                    temp_qty -= buy_qty
                    buy_queues[sym].pop(0)
                    matched = True
                else:
                    trade_pnl += temp_qty * (price - buy_price)
                    oldest_buy["qty"] -= temp_qty
                    temp_qty = 0
                    matched = True
            
            if matched:
                closed_pnls[sym].append(trade_pnl)

    for sym, pnls in closed_pnls.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        total = len(pnls)
        win_rates[sym] = wins / total
        
    return win_rates

def calculate_technical_score(row: pd.Series) -> float:
    """
    Scores a symbol's setup based on its technical indicator values (daily granularity).
    Base score is 50. Returns a numeric score.
    """
    score = 50.0
    
    # 1. Momentum Setup (SMA and MACD)
    close = row.get("close", 0.0)
    sma_20 = row.get("sma_20", np.nan)
    sma_50 = row.get("sma_50", np.nan)
    macd_hist = row.get("macd_hist", np.nan)
    
    if not pd.isna(sma_20) and close > sma_20:
        score += 5.0
    else:
        score -= 3.0
        
    if not pd.isna(sma_50) and close > sma_50:
        score += 5.0
    else:
        score -= 3.0
        
    if not pd.isna(macd_hist) and macd_hist > 0:
        score += 5.0
    else:
        score -= 3.0
        
    # 2. Mean Reversion Setup (RSI and Bollinger Bands)
    rsi = row.get("rsi_14", np.nan)
    b_upper = row.get("bollinger_upper", np.nan)
    b_lower = row.get("bollinger_lower", np.nan)
    
    if not pd.isna(rsi):
        if rsi < 30:
            score += 15.0  # Strongly oversold pullback opportunity
        elif rsi < 40:
            score += 8.0   # Moderately oversold
        elif rsi > 70:
            score -= 10.0  # Strongly overbought extension
        elif rsi > 60:
            score -= 5.0   # Moderately overbought
        elif 45 <= rsi <= 55:
            score += 2.0   # Healthy consolidation
            
    if not pd.isna(b_upper) and not pd.isna(b_lower) and (b_upper - b_lower) > 0:
        pct_b = (close - b_lower) / (b_upper - b_lower)
        if pct_b <= 0.1:
            score += 15.0  # Deep pullback to Bollinger Lower Band
        elif pct_b <= 0.25:
            score += 7.0   # Pullback near Bollinger Lower Band
        elif pct_b >= 0.9:
            score -= 10.0  # Stretched near Bollinger Upper Band
            
    return score

def run_screener(client: AlpacaClient, data_provider: DataProvider, watchlist_limit: int = 5, candidates: list[str] = None) -> list[str]:
    """
    Runs the autonomous screener cycle:
    1. Loads candidates from screener_pool.json if none provided.
    2. Batch fetches historical daily bars.
    3. Vector-computes technical indicators.
    4. Filters out symbols with average daily dollar volume <= $10M.
    5. Calculates technical setup scores.
    6. Adjusts scores using the SQLite trade feedback loop (win rate booster/penalty).
    7. Sorts and returns the top watchlist_limit symbols.
    8. Logs chosen watchlist to watchlist_history table.
    """
    logger.info("Starting Autonomous AI Screener execution...")
    
    # 1. Load candidates
    if candidates is None:
        candidates = load_screener_pool()
    logger.info(f"Loaded {len(candidates)} candidates from pool configuration.")
    
    # 2. Fetch daily bars in batch (requires 50 daily bars to calculate 50 SMA)
    try:
        df = client.get_historical_bars(candidates, limit=50, timeframe_str="day")
    except Exception as e:
        logger.error(f"Screener batch bar fetching failed: {e}")
        # Return fallback trading universe
        return config.TRADING_UNIVERSE[:watchlist_limit]
        
    if df.empty or not isinstance(df.index, pd.MultiIndex):
        logger.warning("Empty or flat dataframe returned for screener batch. Returning fallback universe.")
        return config.TRADING_UNIVERSE[:watchlist_limit]
        
    # 3. Vectorized Technical Indicators
    try:
        df = data_provider._add_technical_indicators(df)
    except Exception as e:
        logger.error(f"Screener indicator calculation failed: {e}")
        return config.TRADING_UNIVERSE[:watchlist_limit]
        
    # Get win rates for SQLite Feedback Loop
    win_rates = get_symbol_win_rates()
    
    scored_symbols = []
    
    # Group by level 0 (symbol)
    for symbol, group_df in df.groupby(level=0):
        if len(group_df) < 20:
            continue
            
        # 4. Stage 1: Liquidity Filter
        # Calculate average daily dollar volume (volume * close) over last 30 bars
        recent_bars = group_df.tail(30)
        dollar_volume = recent_bars["volume"] * recent_bars["close"]
        avg_dollar_vol = float(dollar_volume.mean())
        
        # Min Daily Dollar Volume threshold: $10,000,000 (10M)
        # For mock client, we can relax or skip this filter, but we also mock volume to be large
        if avg_dollar_vol < 10000000.0:
            logger.debug(f"Filtering out {symbol}: Illiquid (Avg Dollar Vol: ${avg_dollar_vol:,.2f})")
            continue
            
        # 5. Stage 2: Technical Setup Scoring
        latest_row = group_df.iloc[-1]
        tech_score = calculate_technical_score(latest_row)
        
        # 6. Stage 3: SQLite Feedback Loop
        multiplier = 1.0
        win_rate = win_rates.get(symbol, None)
        if win_rate is not None:
            if win_rate >= 0.60:
                multiplier = 1.2
                logger.info(f"SQLite Booster applied to {symbol}: {multiplier}x (Win Rate: {win_rate*100:.1f}%)")
            elif win_rate < 0.40:
                multiplier = 0.7
                logger.info(f"SQLite Penalty applied to {symbol}: {multiplier}x (Win Rate: {win_rate*100:.1f}%)")
                
        final_score = tech_score * multiplier
        scored_symbols.append((symbol, final_score, avg_dollar_vol))
        
    if not scored_symbols:
        logger.warning("No symbols passed the liquidity and scoring filter. Using fallback universe.")
        return config.TRADING_UNIVERSE[:watchlist_limit]
        
    # Sort descending by score
    scored_symbols.sort(key=lambda x: x[1], reverse=True)
    
    # Select top N watchlist candidates
    final_watchlist = [item[0] for item in scored_symbols[:watchlist_limit]]
    
    logger.info(f"Screener complete. Top selected candidates:")
    for sym, score, vol in scored_symbols[:watchlist_limit]:
        wr_str = f"{win_rates[sym]*100:.1f}%" if sym in win_rates else "N/A"
        logger.info(f" - {sym}: Score = {score:.2f} | Avg Daily Vol = ${vol:,.2f} | DB Win Rate = {wr_str}")
        
    # 8. Log chosen watchlist to database
    try:
        log_watchlist(final_watchlist)
        logger.info("Watchlist successfully logged to watchlist_history.")
    except Exception as db_err:
        logger.error(f"Failed to log watchlist to DB: {db_err}")
        
    return final_watchlist
