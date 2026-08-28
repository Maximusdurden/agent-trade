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
from core.feedback import compute_closed_round_trips, symbol_stats, feedback_text

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

def get_symbol_feedback() -> dict[str, dict]:
    """Decay-weighted per-symbol performance feedback for the screener.

    Preferred over ``get_symbol_win_rates`` because it summarizes expectancy,
    profit factor and whipsaw exposure (not just raw win rate), which the
    screener scoring uses to make a more robust candidate selection.
    """
    trips = compute_closed_round_trips()
    by_symbol = defaultdict(list)
    for t in trips:
        by_symbol[t["symbol"]].append(t)
    return {sym: symbol_stats_from_trips(sym, ts) for sym, ts in by_symbol.items()}


def symbol_stats_from_trips(symbol: str, trips: list[dict]) -> dict:
    """Aggregate decay-weighted stats for one symbol given its round-trips."""
    from core.feedback import _age_weight, holding_bucket, WHIPSAW_HOURS, DEFAULT_HALF_LIFE_DAYS

    w_total = 0.0
    w_wins = 0.0
    w_pnl = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    whipsaw_weight = 0.0

    for t in trips:
        w = _age_weight(t["close_ts"], DEFAULT_HALF_LIFE_DAYS)
        w_total += w
        w_pnl += w * t["pnl"]
        if t["pnl"] > 0:
            w_wins += w
            gross_win += w * t["pnl"]
        else:
            gross_loss += w * abs(t["pnl"])
        if holding_bucket(t["holding_hours"]) == "under_4h_whipsaw":
            whipsaw_weight += w

    win_rate = (w_wins / w_total * 100.0) if w_total > 0 else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    whipsaw_ratio = (whipsaw_weight / w_total) if w_total > 0 else 0.0

    return {
        "symbol": symbol,
        "n_trades": len(trips),
        "win_rate": round(win_rate, 2),
        "avg_pnl": round(w_pnl / w_total, 2) if w_total > 0 else 0.0,
        "expectancy": round(w_pnl, 2),
        "profit_factor": profit_factor,
        "whipsaw_ratio": round(whipsaw_ratio, 2),
    }


def get_symbol_win_rates() -> dict[str, float]:
    """Calculates historical win rate per symbol from closed trades in the SQLite database.

    Compatibility wrapper retained for older callers; prefer ``get_symbol_feedback``.
    Returns win rates as FRACTIONS (0.0-1.0) matching the original contract.
    """
    fb = {}
    try:
        fb = get_symbol_feedback()
    except Exception as e:
        logger.warning(f"Could not compute symbol feedback for win rates: {e}")
    return {sym: (stats.get("win_rate", 0.0) / 100.0) for sym, stats in fb.items()}

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

def run_screener(client: AlpacaClient, data_provider: DataProvider, watchlist_limit: int = 5, candidates: list[str] | None = None) -> list[str]:
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
        
    # Get decay-weighted per-symbol feedback for the SQLite Feedback Loop.
    symbol_feedback = get_symbol_feedback()

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

        # Check if the symbol is a cryptocurrency (e.g., SOL/USD)
        is_crypto = '/' in symbol or symbol.endswith('USD')

        # Apply volume filter only if it's not a cryptocurrency
        if not is_crypto and avg_dollar_vol <= 10_000_000:
            logger.debug(f"Skipping {symbol}: Avg daily dollar volume ${avg_dollar_vol:,.2f} is below $10M threshold.")
            continue # Skip this symbol
        if not is_crypto and avg_dollar_vol < 10000000.0:
            logger.debug(f"Filtering out {symbol}: Illiquid (Avg Dollar Vol: ${avg_dollar_vol:,.2f})")
            continue
            
        # 5. Stage 2: Technical Setup Scoring
        latest_row = group_df.iloc[-1]
        tech_score = calculate_technical_score(latest_row)
        
        # 6. Stage 3: Feedback Loop (decay-weighted expectancy / profit factor)
        # Replace the coarse win-rate multiplier with a capped additive adjustment
        # driven by expectancy and profit factor, and penalize whipsaw-heavy names
        # even if they are net winners.
        fb = symbol_feedback.get(symbol)
        adjustment = 0.0
        if fb and fb["n_trades"] > 0:
            pf = fb.get("profit_factor")
            exp = fb.get("expectancy", 0.0)
            # Profit-factor contribution: strong PF -> positive, weak PF -> negative.
            if pf == "inf" or (isinstance(pf, float) and pf >= 1.5):
                adjustment += 10.0
            elif isinstance(pf, float):
                adjustment += max(-12.0, min(8.0, (pf - 1.0) * 12.0))
            # Expectancy direction & magnitude (bounded).
            if exp <= 0:
                adjustment -= 8.0
            else:
                adjustment += min(6.0, exp / 25.0)
            # Whipsaw penalty: if >40% of (decayed) activity is <4h round-trips.
            if fb["whipsaw_ratio"] > 0.40:
                adjustment -= 10.0
                logger.info(f"Feedback whipsaw penalty applied to {symbol}: "
                            f"whipsaw_ratio={fb['whipsaw_ratio']*100:.0f}%")
            adjustment = max(-15.0, min(15.0, adjustment))
            logger.info(f"Feedback adjustment for {symbol}: {adjustment:+.1f} "
                        f"(PF={pf}, exp=${exp:+,.2f}, n={fb['n_trades']})")
                
        final_score = tech_score + adjustment
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
        fb = symbol_feedback.get(sym)
        fb_str = "N/A"
        if fb and fb["n_trades"] > 0:
            fb_str = f"WR={fb['win_rate']:.1f}% PF={fb['profit_factor'] if fb['profit_factor'] == 'inf' else round(fb['profit_factor'],2)}"
        logger.info(f" - {sym}: Score = {score:.2f} | Avg Daily Vol = ${vol:,.2f} | Feedback = {fb_str}")
        
    # 8. Log chosen watchlist to database
    try:
        log_watchlist(final_watchlist)
        logger.info("Watchlist successfully logged to watchlist_history.")
    except Exception as db_err:
        logger.error(f"Failed to log watchlist to DB: {db_err}")
        
    return final_watchlist
