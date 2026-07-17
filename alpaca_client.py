import logging
import time
from datetime import datetime, timedelta
import pandas as pd

# Try importing alpaca-py clients. If not installed or fails, we provide a warning.
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    NEWS_AVAILABLE = True
except ImportError:
    NEWS_AVAILABLE = False

import config

logger = logging.getLogger("AlpacaClient")

class AlpacaClient:
    """Wrapper class for interfacing with the Alpaca API."""
    
    def __init__(self):
        self.api_key = config.ALPACA_API_KEY
        self.secret_key = config.ALPACA_SECRET_KEY
        self.paper = config.ALPACA_PAPER
        self.is_mock = False
        
        if not ALPACA_AVAILABLE:
            logger.warning("alpaca-py is not installed. Falling back to mock client.")
            self.is_mock = True
        elif not self.api_key or self.api_key == "your_alpaca_api_key_here":
            logger.warning("Alpaca API credentials missing. Falling back to mock client.")
            self.is_mock = True
        else:
            try:
                # Initialize real clients
                self.trading_client = TradingClient(
                    api_key=self.api_key,
                    secret_key=self.secret_key,
                    paper=self.paper
                )
                self.data_client = StockHistoricalDataClient(
                    api_key=self.api_key,
                    secret_key=self.secret_key
                )
                self.crypto_data_client = CryptoHistoricalDataClient(
                    api_key=self.api_key,
                    secret_key=self.secret_key
                )
                if NEWS_AVAILABLE:
                    self.news_client = NewsClient(
                        api_key=self.api_key,
                        secret_key=self.secret_key
                    )
                    logger.info("Alpaca NewsClient initialized.")
                else:
                    self.news_client = None
                logger.info("Successfully connected to Alpaca real/paper Trading API.")
            except Exception as e:
                logger.error(f"Failed to initialize Alpaca Client: {e}. Falling back to mock client.")
                self.is_mock = True
        
        if self.is_mock:
            # Simple mock data for offline/mock testing
            self.mock_cash = 100000.0
            self.mock_positions = {}  # symbol -> qty
            self.mock_equity = 100000.0
            logger.info("Initializing Mock Alpaca Trading Client (cash: $100,000).")

    def get_account_state(self) -> dict:
        """Retrieves portfolio state including cash, total equity, and buying power."""
        if self.is_mock:
            # Dynamically calculate equity based on a static price assumption for simplicity in mock
            position_value = sum(qty * 400.0 for qty in self.mock_positions.values()) # assume $400 share price
            self.mock_equity = self.mock_cash + position_value
            return {
                "cash": self.mock_cash,
                "equity": self.mock_equity,
                "buying_power": self.mock_cash * 2, # standard 2x paper buying power
                "unrealized_pnl": 0.0,
                "is_mock": True
            }
        
        try:
            account = self.trading_client.get_account()
            # Fetch active positions to calculate actual total unrealized profit/loss on all open holdings
            try:
                positions = self.trading_client.get_all_positions()
                unrealized_pnl = sum(float(pos.unrealized_pl) for pos in positions) if positions else 0.0
            except Exception as pos_err:
                logger.warning(f"Failed to fetch positions for account state unrealized PnL: {pos_err}")
                unrealized_pnl = 0.0
                
            return {
                "cash": float(account.cash),
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "unrealized_pnl": unrealized_pnl,
                "is_mock": False
            }
        except Exception as e:
            logger.error(f"Error fetching account info: {e}")
            raise

    def get_positions(self) -> dict:
        """Returns current open positions, mapped as symbol -> details dict."""
        if self.is_mock:
            positions_dict = {}
            for sym, qty in self.mock_positions.items():
                if qty > 0:
                    positions_dict[sym] = {
                        "qty": qty,
                        "qty_available": qty,
                        "market_value": qty * 400.0,
                        "avg_entry_price": 400.0,
                        "unrealized_pnl": 0.0
                    }
            return positions_dict
        
        try:
            positions = self.trading_client.get_all_positions()
            positions_dict = {}
            for pos in positions:
                positions_dict[pos.symbol] = {
                    "qty": float(pos.qty),
                    "qty_available": float(pos.qty_available) if hasattr(pos, "qty_available") else float(pos.qty),
                    "market_value": float(pos.market_value),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "unrealized_pnl": float(pos.unrealized_pl)
                }
            return positions_dict
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return {}

    def get_historical_bars(self, symbol: str, limit: int = 100, timeframe_str: str = "day") -> pd.DataFrame:
        """Fetches historical daily or intraday bar data for a ticker (automatically handles Stocks or Crypto)."""
        symbol = symbol.upper()
        timeframe_str = timeframe_str.lower()
        
        if self.is_mock:
            # Generate dummy pandas dataframe with close prices
            logger.info(f"Generating mock historical bars for {symbol} with timeframe {timeframe_str}.")
            end_date = datetime.now()
            
            if timeframe_str == "day":
                dates = [end_date - timedelta(days=i) for i in range(limit)][::-1]
            else:
                dates = [end_date - timedelta(minutes=15 * i) for i in range(limit)][::-1]
                
            import numpy as np
            
            # Base price: Stock is around $400, Solana is around $140
            base_price = 140.0 if "SOL" in symbol else 400.0
            
            closes = base_price + np.sin(np.linspace(0, 10, limit)) * (base_price * 0.05) + np.linspace(0, base_price * 0.05, limit)
            highs = closes * 1.01
            lows = closes * 0.99
            opens = closes - (closes * 0.002)
            volumes = np.random.randint(100000, 5000000, size=limit)
            
            df = pd.DataFrame({
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes
            }, index=dates)
            df.index.name = "timestamp"
            return df
            
        # Determine TimeFrame object from string
        if timeframe_str == "day":
            tf = TimeFrame.Day
            day_multiplier = 2
        elif timeframe_str in ("15min", "15m"):
            try:
                from alpaca.data.timeframe import TimeFrameUnit
                tf = TimeFrame(15, TimeFrameUnit.Minute)
            except Exception:
                tf = TimeFrame.Minute # Safe fallback if custom unit creation fails
            day_multiplier = 10  # Cover enough days back to satisfy limit of 15m intervals
        else:
            tf = TimeFrame.Day
            day_multiplier = 2
        
        # Check if symbol is cryptocurrency
        is_crypto = "/" in symbol or "USD" in symbol
        
        try:
            # Fetch last N days (making sure we cover weekends/holidays)
            # Fetch double the limit in calendar days to guarantee enough trading days
            start_time = datetime.now() - timedelta(days=limit * day_multiplier)
            
            if is_crypto:
                request_params = CryptoBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start_time,
                    end=datetime.now()
                )
                bars = self.crypto_data_client.get_crypto_bars(request_params)
            else:
                request_params = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start_time,
                    end=datetime.now()
                )
                bars = self.data_client.get_stock_bars(request_params)
            
            # Convert to pandas DataFrame
            df = bars.df
            # alpaca-py multi-index handles symbol. Reset index or select the symbol.
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol)
                
            # Take the tail up to requested limit
            return df.tail(limit)
        except Exception as e:
            logger.error(f"Error fetching historical bars for {symbol}: {e}")
            raise

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancels all open orders for a specific symbol."""
        symbol = symbol.upper()
        if self.is_mock:
            logger.info(f"[MOCK] Canceling all mock orders for symbol {symbol}.")
            return

        logger.info(f"Retrieving open orders to cancel for symbol {symbol}...")
        try:
            open_orders = self.trading_client.get_orders()
            if not open_orders:
                logger.info("No open orders found.")
                return

            for order in open_orders:
                if order.symbol.upper().replace('/', '') == symbol.replace('/', ''):
                    logger.info(f"Canceling order {order.id} for symbol {order.symbol}...")
                    try:
                        self.trading_client.cancel_order_by_id(order.id)
                        logger.info(f"Successfully requested cancellation for order {order.id}.")
                    except Exception as cancel_err:
                        err_msg = str(cancel_err).lower()
                        err_code = getattr(cancel_err, "code", None)
                        if "pending cancel" in err_msg or "42210000" in err_msg or err_code == 42210000:
                            # Suppress log level to warning/info for harmless, temporary API states to prevent noisy ticketing
                            logger.warning(f"Order {order.id} is already pending cancellation: {cancel_err}")
                        else:
                            logger.error(f"Failed to cancel order {order.id}: {cancel_err}")
        except Exception as e:
            logger.error(f"Error while canceling open orders for symbol {symbol}: {e}")

    def execute_market_order(self, symbol: str, qty: float, side: str, take_profit_price: float = None, stop_loss_price: float = None) -> dict:
        """Executes a market order, optionally adding bracket take-profit and stop-loss legs for BUY actions on non-crypto assets."""
        symbol = symbol.upper()
        side = side.lower()
        
        if side == "sell":
            logger.info(f"Initiating cancellation of open orders for {symbol} prior to selling.")
            self.cancel_open_orders(symbol)
            
            # Dynamic Wait Loop: Wait for Alpaca to asynchronously process cancellations and release locked shares
            if not self.is_mock:
                logger.info(f"Waiting for Alpaca to release locked shares for {symbol} before submitting market SELL order...")
                max_wait = 10
                for _ in range(max_wait):
                    try:
                        pos = self.trading_client.get_open_position(symbol.replace("/", ""))
                        pos_qty = float(getattr(pos, "qty", 0))
                        avail_qty = float(getattr(pos, "qty_available", 0))
                        if pos_qty > 0 and avail_qty == pos_qty:
                            logger.info(f"Verified locked shares are fully released. Qty: {pos_qty}, Available: {avail_qty}.")
                            break
                    except Exception as pos_err:
                        # Position might be temporarily unavailable or fully closed
                        pass
                    time.sleep(1)

                # Fetch the open position again to check the final qty_available
                qty_available = 0.0
                try:
                    pos = self.trading_client.get_open_position(symbol.replace("/", ""))
                    qty_available = float(pos.qty_available) if hasattr(pos, "qty_available") else float(pos.qty)
                except Exception as pos_err:
                    logger.warning(f"Could not fetch open position for {symbol} to verify qty_available: {pos_err}")
                    qty_available = 0.0

                if qty_available < qty:
                    logger.warning(f"Requested qty {qty} is greater than qty_available {qty_available} for {symbol}.")
                    if qty_available > 0:
                        logger.info(f"Scaling down order quantity for {symbol} from {qty} to {qty_available}.")
                        qty = qty_available
                    else:
                        logger.warning(f"Aborting order for {symbol} as qty_available is 0.")
                        return {
                            "id": f"aborted-{int(time.time())}",
                            "symbol": symbol,
                            "qty": qty,
                            "side": side,
                            "filled_avg_price": None,
                            "status": "aborted_no_available_shares"
                        }
        
        if self.is_mock:
            price = 140.0 if "SOL" in symbol else 400.0
            cost = price * qty
            if side == "buy":
                if cost > self.mock_cash:
                    raise ValueError(f"Insufficient mock funds. Cash: {self.mock_cash}, Order Cost: {cost}")
                self.mock_cash -= cost
                self.mock_positions[symbol] = self.mock_positions.get(symbol, 0.0) + qty
                if take_profit_price and stop_loss_price:
                    logger.info(f"[MOCK BRACKET] Attached TP: ${take_profit_price:.2f} | SL: ${stop_loss_price:.2f} to {symbol} BUY order.")
            elif side == "sell":
                current_qty = self.mock_positions.get(symbol, 0.0)
                if qty > current_qty:
                    raise ValueError(f"Insufficient mock positions. Available: {current_qty}, Order Qty: {qty}")
                self.mock_cash += cost
                self.mock_positions[symbol] = current_qty - qty
                if self.mock_positions[symbol] <= 0:
                    del self.mock_positions[symbol]
                    
            return {
                "id": f"mock-order-{int(datetime.now().timestamp())}",
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "filled_avg_price": price,
                "status": "filled"
            }
        
        try:
            order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
            is_crypto = "/" in symbol or "USD" in symbol or "SOL" in symbol
            
            if side == "buy" and not is_crypto and take_profit_price is not None and stop_loss_price is not None:
                market_order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=take_profit_price),
                    stop_loss=StopLossRequest(stop_price=stop_loss_price)
                )
                logger.info(f"Submitting Exchange-Side Bracket Order for {symbol}: Buy {qty} shares, Take-Profit at ${take_profit_price:.2f}, Stop-Loss at ${stop_loss_price:.2f}.")
            else:
                time_in_force_val = TimeInForce.GTC if is_crypto else TimeInForce.DAY
                market_order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=time_in_force_val
                )
            
            order = self.trading_client.submit_order(order_data=market_order_data)
            order_id = str(order.id)
            logger.info(f"Market order submitted successfully to Alpaca. Order ID: {order_id}. Polling for fill confirmation...")
            
            # Poll order details to guarantee we obtain a real filled_avg_price
            filled_price = None
            status_str = str(order.status.value)
            
            for attempt in range(10):
                try:
                    updated_order = self.trading_client.get_order_by_id(order_id)
                    status_str = str(updated_order.status.value)
                    if updated_order.filled_avg_price is not None:
                        filled_price = float(updated_order.filled_avg_price)
                    if status_str in ("filled", "partially_filled"):
                        logger.info(f"Order {order_id} confirmed filled. Status: {status_str} | Avg Price: ${filled_price}")
                        break
                except Exception as poll_err:
                    logger.warning(f"Error polling order {order_id}: {poll_err}")
                time.sleep(0.5)
                
            if filled_price is None:
                logger.warning(f"Order {order_id} polling complete but fill price is still None. Status is: {status_str}")
                
            return {
                "id": order_id,
                "symbol": symbol,
                "qty": float(order.qty),
                "side": side,
                "filled_avg_price": filled_price,
                "status": status_str
            }
        except Exception as e:
            logger.error(f"Error executing market order: {e}")
            raise

    def get_news(self, symbol: str, limit: int = 3) -> list[dict]:
        """Fetches latest news for a symbol."""
        query_sym = symbol.upper()
        if "/" in query_sym:
            query_sym = query_sym.split("/")[0]
        elif "USD" in query_sym and len(query_sym) > 3:
            query_sym = query_sym.replace("USD", "")
            
        if self.is_mock or not NEWS_AVAILABLE or not getattr(self, "news_client", None):
            logger.info(f"Generating mock news for {symbol}.")
            return [
                {
                    "headline": f"Positive market momentum observed for {symbol} near key technical thresholds.",
                    "source": "MockNews",
                    "summary": f"Market dynamics for {symbol} remain stable with strong institutional interest reported.",
                    "url": f"https://example.com/news/{symbol}/1"
                },
                {
                    "headline": f"Sector rotation creates entry opportunity for index leaders.",
                    "source": "MockFinance",
                    "summary": f"Recent price movement of {symbol} highlights key dynamic support zones.",
                    "url": f"https://example.com/news/{symbol}/2"
                }
            ]
        
        try:
            start_time = datetime.now() - timedelta(days=5)
            request_params = NewsRequest(
                symbols=query_sym,
                start=start_time,
                end=datetime.now(),
                limit=limit
            )
            news_response = self.news_client.get_news(request_params)
            
            news_list = []
            for item in getattr(news_response, "news", []):
                news_list.append({
                    "headline": getattr(item, "headline", ""),
                    "source": getattr(item, "source", ""),
                    "summary": getattr(item, "summary", "") or getattr(item, "content", "")[:200],
                    "url": str(getattr(item, "url", "")) if getattr(item, "url", None) else ""
                })
            return news_list
        except Exception as e:
            logger.warning(f"Failed to fetch news for {symbol}: {e}. Returning mock news.")
            return [
                {
                    "headline": f"Technical factors dominate trading range of {symbol}.",
                    "source": "MockNews",
                    "summary": f"No major macro news events registered today. Ticker {symbol} continues regular trading.",
                    "url": f"https://example.com/news/{symbol}/fallback"
                }
            ]
