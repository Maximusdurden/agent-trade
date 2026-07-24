import logging
import pandas as pd
import numpy as np
from core.alpaca_client import AlpacaClient

logger = logging.getLogger("DataProvider")

class DataProvider:
    """Class responsible for fetching market data and calculating technical indicators."""
    
    def __init__(self, client: AlpacaClient):
        self.client = client

    def get_market_state(self, symbol: str, timeframe_str: str = "15min") -> dict:
        """Fetches historical bars for a symbol and returns a dictionary of latest prices and indicators."""
        try:
            # Fetch last 100 bars (e.g. 15min bars) to calculate technical indicators
            df = self.client.get_historical_bars(symbol, limit=100, timeframe_str=timeframe_str)
            
            if df.empty or len(df) < 30:
                logger.warning(f"Not enough data to calculate indicators for {symbol}.")
                return {}

            # Calculate technical indicators
            df = self._add_technical_indicators(df)
            
            # Get the latest close and interval details
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            
            # Fetch daily return pct using daily bars to ensure accuracy of daily change metrics
            daily_return_pct = 0.0
            prev_close_val = float(prev["close"])
            daily_df = pd.DataFrame()
            try:
                daily_df = self.client.get_historical_bars(symbol, limit=35, timeframe_str="day")
                if not daily_df.empty and len(daily_df) >= 2:
                    d_latest = daily_df.iloc[-1]
                    d_prev = daily_df.iloc[-2]
                    daily_return_pct = float((d_latest["close"] - d_prev["close"]) / d_prev["close"] * 100)
                    prev_close_val = float(d_prev["close"])
                elif not daily_df.empty:
                    d_latest = daily_df.iloc[-1]
                    daily_return_pct = float((float(latest["close"]) - d_latest["open"]) / d_latest["open"] * 100)
                    prev_close_val = float(d_latest["open"])
                else:
                    daily_return_pct = float((latest["close"] - prev["close"]) / prev["close"] * 100)
            except Exception as daily_err:
                logger.warning(f"Failed to fetch daily return for {symbol}: {daily_err}. Using interval return instead.")
                daily_return_pct = float((latest["close"] - prev["close"]) / prev["close"] * 100)

            # Calculate Fibonacci, Psychological, and Support/Resistance Pivot levels
            pivots = self._calculate_advanced_pivots(daily_df, float(latest["close"]), symbol)

            # Fetch recent news/events
            news_data = []
            try:
                news_data = self.client.get_news(symbol, limit=3)
            except Exception as news_err:
                logger.warning(f"Could not retrieve news for {symbol}: {news_err}")

            market_state = {
                "symbol": symbol,
                "current_price": float(latest["close"]),
                "prev_close": prev_close_val,
                "daily_return_pct": daily_return_pct,
                "volume": int(latest["volume"]),
                "indicators": {
                    "rsi_14": float(latest["rsi_14"]) if not pd.isna(latest["rsi_14"]) else None,
                    "sma_20": float(latest["sma_20"]) if not pd.isna(latest["sma_20"]) else None,
                    "sma_50": float(latest["sma_50"]) if not pd.isna(latest["sma_50"]) else None,
                    "macd_line": float(latest["macd_line"]) if not pd.isna(latest["macd_line"]) else None,
                    "macd_signal": float(latest["macd_signal"]) if not pd.isna(latest["macd_signal"]) else None,
                    "macd_hist": float(latest["macd_hist"]) if not pd.isna(latest["macd_hist"]) else None,
                    "bollinger_upper": float(latest["bollinger_upper"]) if not pd.isna(latest["bollinger_upper"]) else None,
                    "bollinger_lower": float(latest["bollinger_lower"]) if not pd.isna(latest["bollinger_lower"]) else None,
                },
                "advanced_pivots": pivots,
                "news": news_data
            }
            return market_state
            
        except Exception as e:
            logger.error(f"Error compiling market state for {symbol}: {e}")
            return {}

    def _add_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Helper to calculate standard technical indicators using Pandas/Numpy."""
        df = df.copy()
        
        # 1. Simple Moving Averages (SMA)
        df["sma_20"] = df["close"].rolling(window=20).mean()
        df["sma_50"] = df["close"].rolling(window=50).mean()
        
        # 2. Relative Strength Index (RSI - 14 period)
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).copy()
        loss = (-delta.where(delta < 0, 0)).copy()
        
        # Smooth gain and loss using Exponential Moving Average
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        
        # Avoid division by zero
        rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
        df["rsi_14"] = 100 - (100 / (1 + rs))
        
        # 3. MACD (Moving Average Convergence Divergence)
        ema_12 = df["close"].ewm(span=12, adjust=False).mean()
        ema_26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd_line"] = ema_12 - ema_26
        df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd_line"] - df["macd_signal"]
        
        # 4. Bollinger Bands (20-day, 2 standard deviations)
        df["bollinger_mid"] = df["close"].rolling(window=20).mean()
        std_20 = df["close"].rolling(window=20).std()
        df["bollinger_upper"] = df["bollinger_mid"] + (std_20 * 2)
        df["bollinger_lower"] = df["bollinger_mid"] - (std_20 * 2)
        
        return df

    def _calculate_advanced_pivots(self, daily_df: pd.DataFrame, current_price: float, symbol: str) -> dict:
        """
        Calculates advanced price anchors:
        - Fibonacci Retracement levels (based on 30-day high/low range)
        - Round number psychological levels (nearest above/below)
        - Support/Resistance zones (using recent local swing highs and lows)
        """
        result = {
            "fib_levels": {},
            "psychological_levels": {},
            "pivot_zones": {}
        }
        
        if daily_df.empty or len(daily_df) < 5:
            return result
            
        # 1. Fibonacci Retracements (using last 30 daily bars)
        recent_bars = daily_df.tail(30)
        high_30 = float(recent_bars["high"].max())
        low_30 = float(recent_bars["low"].min())
        range_30 = high_30 - low_30
        
        if range_30 > 0:
            result["fib_levels"] = {
                "0.0% (Low)": low_30,
                "23.6%": low_30 + 0.236 * range_30,
                "38.2%": low_30 + 0.382 * range_30,
                "50.0%": low_30 + 0.500 * range_30,
                "61.8%": low_30 + 0.618 * range_30,
                "100.0% (High)": high_30
            }
            
        # 2. Psychological Levels
        if current_price > 250:
            increment = 10.0
        elif current_price > 50:
            increment = 5.0
        else:
            increment = 1.0
            
        psy_lower = (current_price // increment) * increment
        psy_upper = psy_lower + increment
        result["psychological_levels"] = {
            "closest_support": psy_lower,
            "closest_resistance": psy_upper
        }
        
        # 3. Supply & Demand (Support/Resistance Swing Levels)
        highs = daily_df["high"].values
        lows = daily_df["low"].values
        
        swing_highs = []
        swing_lows = []
        
        # Look for peaks and valleys with a window of 2 on each side (total size 5)
        for i in range(2, len(daily_df) - 2):
            if (highs[i] >= highs[i-1] and highs[i] >= highs[i-2] and 
                highs[i] >= highs[i+1] and highs[i] >= highs[i+2]):
                swing_highs.append(float(highs[i]))
            if (lows[i] <= lows[i-1] and lows[i] <= lows[i-2] and 
                lows[i] <= lows[i+1] and lows[i] <= lows[i+2]):
                swing_lows.append(float(lows[i]))
                
        support = None
        resistance = None
        
        # Filter swing highs that are above current price to find resistance
        potential_res = [h for h in swing_highs if h > current_price]
        if potential_res:
            resistance = min(potential_res)
        elif len(swing_highs) > 0:
            resistance = swing_highs[-1]
            
        # Filter swing lows that are below current price to find support
        potential_sup = [l for l in swing_lows if l < current_price]
        if potential_sup:
            support = max(potential_sup)
        elif len(swing_lows) > 0:
            support = swing_lows[-1]
            
        result["pivot_zones"] = {
            "recent_swing_support": support,
            "recent_swing_resistance": resistance
        }
        
        return result
