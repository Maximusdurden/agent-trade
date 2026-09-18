import logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from core.alpaca_client import AlpacaClient

logger = logging.getLogger("DataProvider")

# Regime classification constants. These define a deterministic, volatility-
# aware market-regime classifier so the brain and strategist get ONE coherent
# instruction per ticker instead of contradictory momentum-vs-reversion signals.
# - REGIME_TREND_SLOPE_LOOKBACK: bars used to estimate the SMA-20 slope (trend
#   strength). A longer window distinguishes a SUSTAINED trend from a sharp blip.
# - REGIME_TREND_MIN_SLOPE_PCT: min |slope| (as % of price per bar) to call a trend.
# - REGIME_RANGE_BAND_SIGMA: Bollinger width (in ATR units) below which = ranging.
# - REGIME_BREAKOUT_MOVE_SIGMA: recent 3-bar move / ATR above which = breakout
#   (a SHARP move, distinct from a sustained trend).
REGIME_TREND_SLOPE_LOOKBACK = 10
REGIME_TREND_MIN_SLOPE_PCT = 0.10   # ~0.10% of price per bar sustained
REGIME_RANGE_BAND_SIGMA = 2.5       # Bollinger width in ATR units
REGIME_BREAKOUT_MOVE_SIGMA = 2.0    # recent 3-bar move / ATR
REGIME_BREAKOUT_MOVE_BARS = 3


def classify_regime(df: pd.DataFrame) -> str:
    """Classify a symbol's current market regime from its indicator frame.

    Returns one of: ``TRENDING_UP``, ``TRENDING_DOWN``, ``RANGING``, ``BREAKOUT``.

    Logic (deterministic, volatility-normalized):
      1. BREAKOUT: a SHARP recent move — the close moved >= REGIME_BREAKOUT_MOVE_SIGMA
         ATRs over the last REGIME_BREAKOUT_MOVE_BARS bars. This is a strong,
         overextended move (mean-reversion setup), distinct from a sustained trend.
      2. Else TRENDING_UP / TRENDING_DOWN: a SUSTAINED SMA-20 slope over a longer
         window (>= REGIME_TREND_MIN_SLOPE_PCT per bar).
      3. Else RANGING: no strong signal (or a narrow Bollinger band relative to ATR).
      4. Fallback: RANGING.

    ``df`` must already have SMA-20, ATR-14, VWAP and Bollinger columns (i.e. it
    has been through ``_add_technical_indicators``). Returns "RANGING" on any
    missing/insufficient data so callers fail safe.
    """
    if df is None or len(df) < 20:
        return "RANGING"
    try:
        close = df["close"]
        sma20 = df["sma_20"]
        atr = df["atr_14"]
        b_upper = df["bollinger_upper"]
        b_lower = df["bollinger_lower"]
    except KeyError:
        return "RANGING"

    latest = df.iloc[-1]
    price = float(latest["close"])
    atr_v = float(latest["atr_14"]) if not pd.isna(latest["atr_14"]) else 0.0

    # 1. Breakout: a SHARP recent move relative to volatility.
    if atr_v > 0 and len(close) > REGIME_BREAKOUT_MOVE_BARS:
        recent_move = abs(float(close.iloc[-1]) - float(close.iloc[-1 - REGIME_BREAKOUT_MOVE_BARS]))
        if (recent_move / atr_v) >= REGIME_BREAKOUT_MOVE_SIGMA:
            return "BREAKOUT"

    # 2. Trend: SUSTAINED SMA-20 slope over a longer window.
    lookback = min(REGIME_TREND_SLOPE_LOOKBACK, len(sma20.dropna()))
    if lookback >= 3:
        recent = sma20.dropna().tail(lookback)
        if len(recent) >= 3 and price > 0:
            slope_per_bar = (float(recent.iloc[-1]) - float(recent.iloc[0])) / (len(recent) - 1)
            slope_pct = slope_per_bar / price * 100.0
            if slope_pct >= REGIME_TREND_MIN_SLOPE_PCT:
                return "TRENDING_UP"
            if slope_pct <= -REGIME_TREND_MIN_SLOPE_PCT:
                return "TRENDING_DOWN"

    # 3. Range: narrow Bollinger band relative to ATR.
    if atr_v > 0 and not pd.isna(latest["bollinger_upper"]) and not pd.isna(latest["bollinger_lower"]):
        band_width = float(latest["bollinger_upper"]) - float(latest["bollinger_lower"])
        if band_width > 0 and (band_width / atr_v) < REGIME_RANGE_BAND_SIGMA:
            return "RANGING"

    return "RANGING"


def normalized_edge_sigma(df: pd.DataFrame) -> float | None:
    """Return the volatility-normalized distance from VWAP: |vwap_dist| / ATR.

    This is the "how much edge exists relative to noise" metric. A value >= 1.0
    means the price is a full ATR away from VWAP (a real move); a value < 0.5 is
    inside the noise band (the KO failure mode). Returns None when VWAP/ATR are
    not yet valid (early session) so callers can treat it as "no signal".
    """
    if df is None or len(df) < 20:
        return None
    try:
        latest = df.iloc[-1]
        price = float(latest["close"])
        atr = float(latest["atr_14"]) if not pd.isna(latest["atr_14"]) else 0.0
        vwap = float(latest["vwap"]) if not pd.isna(latest["vwap"]) else 0.0
    except (KeyError, IndexError):
        return None
    if atr <= 0 or vwap <= 0:
        return None
    return abs(price - vwap) / atr


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
            df = self._add_technical_indicators(df, symbol=symbol)
            
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

            # Gate VWAP-derived fields on intraday bar count. VWAP is cumulative
            # within the current day; with too few bars it is meaningless (a single
            # bar makes vwap == typical_price and vwap_dist_pct ~0%). Expose None
            # until the current day has at least MIN_VWAP_BARS bars so the brain
            # cannot cite VWAP as confluence early in the session.
            from core import config
            today_bars = self._count_today_bars(df, symbol=symbol)
            vwap_valid = today_bars >= config.MIN_VWAP_BARS
            if not vwap_valid:
                logger.info(
                    f"VWAP gated for {symbol}: only {today_bars} intraday bar(s) today "
                    f"(need >= {config.MIN_VWAP_BARS}). Exposing VWAP fields as None."
                )

            # Fetch recent news/events
            news_data = []
            try:
                news_data = self.client.get_news(symbol, limit=3)
            except Exception as news_err:
                logger.warning(f"Could not retrieve news for {symbol}: {news_err}")

            # Regime classification + volatility-normalized edge (Phase 1/2).
            # These give the brain/strategist ONE coherent instruction per ticker
            # and a "how much edge vs noise" metric, instead of contradictory
            # momentum-vs-reversion signals.
            regime = classify_regime(df)
            edge_sigma = normalized_edge_sigma(df)
            # Only expose the normalized edge when VWAP is valid (same gating as
            # the VWAP fields) so early-session "no signal" isn't misread as 0.
            edge_sigma = edge_sigma if (vwap_valid and edge_sigma is not None) else None

            # ------------------------------------------------------------------
            # LIVE-PRICE ALIGNMENT (2026-09-18)
            # Alpaca's intraday bar feed can LAG the real-time quote by minutes
            # to hours (observed: 5-min bars showed $105.75 while the live quote
            # was $111.82). The brain's indicators (RSI/VWAP/regime) are computed
            # from bars, but `current_price` should reflect the LIVE price so the
            # brain sizes/decides against reality, not a stale bar close. We fetch
            # the real-time quote and override `current_price` with it, and expose
            # a `data_freshness` block so the brain can see how stale the bar
            # indicators are (and discount them if needed).
            # ------------------------------------------------------------------
            bar_close = float(latest["close"])
            live_price = bar_close
            bar_lag_minutes = 0.0
            try:
                live_price = float(self.client.get_latest_price(symbol))
            except Exception as live_err:
                logger.warning(f"Could not fetch live price for {symbol}: {live_err}. Using bar close.")
                live_price = bar_close
            # How stale is the latest bar relative to now? (bar feed lag)
            try:
                latest_ts = df.index[-1]
                if isinstance(df.index, pd.MultiIndex):
                    latest_ts = latest_ts[-1]
                if latest_ts.tzinfo is None:
                    latest_ts = latest_ts.tz_localize("UTC")
                bar_lag_minutes = max(0.0, (datetime.now(timezone.utc) - latest_ts).total_seconds() / 60.0)
            except Exception as lag_err:
                logger.debug(f"Could not compute bar lag for {symbol}: {lag_err}")

            market_state = {
                "symbol": symbol,
                "current_price": live_price,
                "bar_close": bar_close,
                "prev_close": prev_close_val,
                "daily_return_pct": daily_return_pct,
                "volume": int(latest["volume"]),
                "regime": regime,
                "edge_sigma": edge_sigma,
                "data_freshness": {
                    "bar_lag_minutes": round(bar_lag_minutes, 1),
                    "is_stale": bar_lag_minutes > float(getattr(config, "DATA_STALE_MINUTES", 15)),
                    "live_price": live_price,
                    "bar_close": bar_close,
                },
                "indicators": {
                    "rsi_14": float(latest["rsi_14"]) if not pd.isna(latest["rsi_14"]) else None,
                    "sma_20": float(latest["sma_20"]) if not pd.isna(latest["sma_20"]) else None,
                    "sma_50": float(latest["sma_50"]) if not pd.isna(latest["sma_50"]) else None,
                    "macd_line": float(latest["macd_line"]) if not pd.isna(latest["macd_line"]) else None,
                    "macd_signal": float(latest["macd_signal"]) if not pd.isna(latest["macd_signal"]) else None,
                    "macd_hist": float(latest["macd_hist"]) if not pd.isna(latest["macd_hist"]) else None,
                    "bollinger_upper": float(latest["bollinger_upper"]) if not pd.isna(latest["bollinger_upper"]) else None,
                    "bollinger_lower": float(latest["bollinger_lower"]) if not pd.isna(latest["bollinger_lower"]) else None,
                    "vwap": float(latest["vwap"]) if (vwap_valid and not pd.isna(latest["vwap"])) else None,
                    "vwap_upper_1": float(latest["vwap_upper_1"]) if (vwap_valid and not pd.isna(latest["vwap_upper_1"])) else None,
                    "vwap_lower_1": float(latest["vwap_lower_1"]) if (vwap_valid and not pd.isna(latest["vwap_lower_1"])) else None,
                    "vwap_upper_2": float(latest["vwap_upper_2"]) if (vwap_valid and not pd.isna(latest["vwap_upper_2"])) else None,
                    "vwap_lower_2": float(latest["vwap_lower_2"]) if (vwap_valid and not pd.isna(latest["vwap_lower_2"])) else None,
                    "vwap_dist_pct": float(latest["vwap_dist_pct"]) if (vwap_valid and not pd.isna(latest["vwap_dist_pct"])) else None,
                    "atr_14": float(latest["atr_14"]) if not pd.isna(latest["atr_14"]) else None,
                    "atr_pct": float(latest["atr_pct"]) if not pd.isna(latest["atr_pct"]) else None,
                    "regime": regime,
                    "edge_sigma": edge_sigma,
                },
                "advanced_pivots": pivots,
                "news": news_data
            }
            return market_state
            
        except Exception as e:
            logger.error(f"Error compiling market state for {symbol}: {e}")
            return {}

    def _add_technical_indicators(self, df: pd.DataFrame, symbol: str | None = None) -> pd.DataFrame:
        """Helper to calculate standard technical indicators using Pandas/Numpy.

        ``symbol`` is used to decide the VWAP day boundary: 24/7 crypto uses a
        rolling window (so VWAP never goes stale at midnight UTC), while equities
        keep session-based (per-UTC-day) VWAP.
        """
        if isinstance(df.index, pd.MultiIndex):
            return df.groupby(level=0, group_keys=False).apply(
                lambda g: self._add_technical_indicators_single(g, symbol)
            )
        else:
            return self._add_technical_indicators_single(df, symbol)

    def _add_technical_indicators_single(self, df: pd.DataFrame, symbol: str | None = None) -> pd.DataFrame:
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

        # 4b. Average True Range (ATR-14) for volatility-based position sizing.
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|).
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["atr_14"] = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        # ATR as a % of price (volatility ratio) for cross-asset comparison.
        df["atr_pct"] = df["atr_14"] / np.where(df["close"] == 0, 0.00001, df["close"]) * 100.0

        # 5. Dynamic Intraday VWAP and Bands.
        # Equities: VWAP resets daily (session-based) — the standard definition.
        # 24/7 crypto: VWAP is computed over a ROLLING window (VWAP_ROLLING_HOURS)
        # so it never goes stale at midnight UTC. A per-UTC-day reset makes crypto
        # VWAP degenerate right after midnight (the new day has < MIN_VWAP_BARS
        # bars), which previously gated VWAP to None for ~20 min every day.
        df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
        df["tp_vol"] = df["typical_price"] * df["volume"]

        is_crypto = bool(symbol) and ("/" in symbol or "USD" in symbol)
        if is_crypto:
            from core import config
            window_hours = float(getattr(config, "VWAP_ROLLING_HOURS", 24.0))
            # Rolling window in bars (5-min bars -> 12/hour). Use the index
            # timestamps to build a boolean mask of bars within the window.
            idx = df.index
            if isinstance(idx, pd.MultiIndex):
                idx = idx.get_level_values(1)
            latest_ts = idx[-1]
            window_start = latest_ts - pd.Timedelta(hours=window_hours)
            in_window = idx >= window_start
            # Cumulative sums over the rolling window (reset at window start).
            df["cum_tp_vol"] = df["tp_vol"].where(in_window, 0.0).cumsum()
            df["cum_vol"] = df["volume"].where(in_window, 0.0).cumsum()
            df["vwap"] = df["cum_tp_vol"] / np.where(df["cum_vol"] == 0, 0.00001, df["cum_vol"])
            df["tp_vwap_diff_sq_vol"] = ((df["typical_price"] - df["vwap"]) ** 2) * df["volume"]
            df["cum_diff_sq_vol"] = df["tp_vwap_diff_sq_vol"].where(in_window, 0.0).cumsum()
            df["vwap_var"] = df["cum_diff_sq_vol"] / np.where(df["cum_vol"] == 0, 0.00001, df["cum_vol"])
            df["vwap_std"] = np.sqrt(np.maximum(df["vwap_var"], 0))
        else:
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

    def _count_today_bars(self, df: pd.DataFrame, symbol: str | None = None) -> int:
        """Count how many intraday bars fall in the VWAP window.

        VWAP is cumulative within a single day, so its value (and bands /
        dist_pct) is only meaningful once enough bars have accumulated. With a
        single bar, vwap == typical_price and vwap_dist_pct collapses to ~0%,
        which the brain can mistake for "price hugging VWAP" confluence. This
        helper returns the number of bars in the VWAP window so callers can gate
        VWAP-derived fields.

        For equities the window is the latest UTC trading day (session-based).
        For 24/7 crypto the window is a rolling VWAP_ROLLING_HOURS window, so the
        count never collapses to < MIN_VWAP_BARS right after midnight UTC.
        """
        if df is None or df.empty:
            return 0
        if isinstance(df.index, pd.MultiIndex):
            # Multi-symbol frame: use the last symbol's level-1 timestamps.
            last_sym = df.index.get_level_values(0)[-1]
            idx = df.index.get_level_values(1)[df.index.get_level_values(0) == last_sym]
        else:
            idx = df.index
        if not hasattr(idx, "date"):
            return 0
        is_crypto = bool(symbol) and ("/" in symbol or "USD" in symbol)
        if is_crypto:
            from core import config
            window_hours = float(getattr(config, "VWAP_ROLLING_HOURS", 24.0))
            latest_ts = idx[-1]
            window_start = latest_ts - pd.Timedelta(hours=window_hours)
            return int((idx >= window_start).sum())
        latest_date = idx[-1].date()
        return int((idx.date == latest_date).sum())

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
