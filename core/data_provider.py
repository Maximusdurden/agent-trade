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
        # Standardize crypto symbols to include /USD suffix if they are recognized crypto tickers
        symbol_upper = symbol.upper()
        crypto_tickers = {"SOL", "BTC", "ETH", "XRP", "ADA", "DOGE"}
        if symbol_upper in crypto_tickers:
            symbol = f"{symbol_upper}/USD"
            
        # For crypto assets, default to 5-minute bars as per the 5-minute trading interval setup
        if timeframe_str == "15min" and ("/" in symbol or "USD" in symbol):
            timeframe_str = "5min"

        try:
            # Fetch last 100 bars to calculate technical indicators
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
                    "vwap": float(latest["vwap"]) if not pd.isna(latest["vwap"]) else None,
                    "vwap_upper_1": float(latest["vwap_upper_1"]) if not pd.isna(latest["vwap_upper_1"]) else None,
                    "vwap_lower_1": float(latest["vwap_lower_1"]) if not pd.isna(latest["vwap_lower_1"]) else None,
                    "vwap_upper_2": float(latest["vwap_upper_2"]) if not pd.isna(latest["vwap_upper_2"]) else None,
                    "vwap_lower_2": float(latest["vwap_lower_2"]) if not pd.isna(latest["vwap_lower_2"]) else None,
                    "vwap_dist_pct": float(latest["vwap_dist_pct"]) if not pd.isna(latest["vwap_dist_pct"]) else None,
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
        if isinstance(df.index, pd.MultiIndex):
            return df.groupby(level=0, group_keys=False).apply(self._add_technical_indicators_single)
        else:
            return self._add_technical_indicators_single(df)

    def _add_technical_indicators_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Helper to calculate standard technical indicators for a single symbol."""
        df = df.copy()
        if df.empty:
            return df
        
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

        # 5. Dynamic Intraday VWAP and Bands (resetting daily)
        df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
        df["tp_vol"] = df["typical_price"] * df["volume"]
        
        dates = df.index.date if not isinstance(df.index, pd.MultiIndex) else df.index.get_level_values(1).date
        
        df["cum_tp_vol"] = df.groupby(dates)["tp_vol"].cumsum()
        df["cum_vol"] = df.groupby(dates)["volume"].cumsum()
        df["vwap"] = df["cum_tp_vol"] / np.where(df["cum_vol"] == 0, 0.00001, df["cum_vol"])
        
        df["tp_vwap_diff_sq_vol"] = ((df["typical_price"] - df["vwap"]) ** 2) * df["volume"]
        df["cum_diff_sq_vol"] = df.groupby(dates)["tp_vwap_diff_sq_vol"].cumsum()
        df["vwap_var"] = df["cum_diff_sq_vol"] / np.where(df["cum_vol"] == 0, 0.00001, df["cum_vol"])
        df["vwap_std"] = np.sqrt(np.maximum(df["vwap_var"], 0))
        
        df["vwap_upper_1"] = df["vwap"] + df["vwap_std"]
        df["vwap_lower_1"] = df["vwap"] - df["vwap_std"]
        df["vwap_upper_2"] = df["vwap"] + (df["vwap_std"] * 2)
        df["vwap_lower_2"] = df["vwap"] - (df["vwap_std"] * 2)
        df["vwap_dist_pct"] = ((df["close"] - df["vwap"]) / np.where(df["vwap"] == 0, 0.00001, df["vwap"])) * 100
        
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
        elif current_price > 10:
            increment = 1.0
        elif current_price > 1.0:
            increment = 0.10
        elif current_price > 0.10:
            increment = 0.01
        elif current_price > 0.01:
            increment = 0.001
        else:
            increment = 0.0001
            
        import math
        psy_lower = math.floor(round(current_price / increment, 9)) * increment
        psy_upper = psy_lower + increment
        result["psychological_levels"] = {
            "closest_support": float(psy_lower),
            "closest_resistance": float(psy_upper)
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


def get_earnings_dates(tickers=None, days_ahead=7) -> pd.DataFrame:
    """Fetches upcoming earnings dates for tickers within ``days_ahead`` days.

    Lightweight wrapper around yfinance (optional dependency). Returns an empty
    DataFrame if yfinance is unavailable or no earnings are found, so callers
    can fail-open safely (used by the options earnings/IV filter).
    """
    if not tickers:
        return pd.DataFrame(columns=["ticker", "earnings_date"])
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed. Earnings filter disabled (fail-open).")
        return pd.DataFrame(columns=["ticker", "earnings_date"])
    except Exception as e:
        logger.warning(f"Failed to import yfinance for earnings check: {e}")
        return pd.DataFrame(columns=["ticker", "earnings_date"])

    results = []
    from datetime import datetime, timedelta, date as _date
    now = datetime.now()
    horizon = now + timedelta(days=days_ahead)
    for ticker in (tickers if isinstance(tickers, (list, tuple)) else [tickers]):
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            if cal is None:
                continue
            earnings = cal.get("Earnings Date")
            if earnings is None:
                continue
            # yfinance 0.2.x returns a pandas Index or list of dates
            dates = list(earnings) if getattr(earnings, "__iter__", None) else [earnings]
            for d in dates:
                if d is None:
                    continue
                if isinstance(d, pd.Timestamp):
                    d = d.to_pydatetime()
                elif not isinstance(d, datetime):
                    # 'YYYY-MM-DD' string or python date
                    try:
                        d = datetime.combine(_date.fromisoformat(str(d)[:10]), datetime.min.time())
                    except ValueError:
                        continue
                # Compare on date only (ignore intraday tz complexity)
                if now.date() <= d.date() <= horizon.date():
                    results.append({"ticker": str(ticker).upper(), "earnings_date": d.date()})
        except Exception as e:
            logger.warning(f"Could not fetch earnings for {ticker}: {e}")
    return pd.DataFrame(results)
