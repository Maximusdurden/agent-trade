import logging
import time
from datetime import datetime, timedelta
import pandas as pd

# Try importing alpaca-py clients. If not installed or fails, we provide a warning.
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, GetOrdersRequest, LimitOrderRequest, GetPortfolioHistoryRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient, OptionHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, OptionChainRequest, OptionLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    try:
        from alpaca.data.enums import DataFeed
        DATA_FEED_AVAILABLE = True
    except ImportError:
        DATA_FEED_AVAILABLE = False
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    DATA_FEED_AVAILABLE = False

try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    NEWS_AVAILABLE = True
except ImportError:
    NEWS_AVAILABLE = False

from core import config

logger = logging.getLogger("AlpacaClient")

# Module-level singleton so guardrails/executors can read options buying power
# without re-instantiating the client.
_client_instance = None


def get_client_instance() -> "AlpacaClient":
    """Returns the shared AlpacaClient singleton (creates it if needed)."""
    global _client_instance
    if _client_instance is None:
        _client_instance = AlpacaClient()
    return _client_instance


class AlpacaClient:
    """Wrapper class for interfacing with the Alpaca API."""
    
    def __init__(self):
        global _client_instance
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
                try:
                    self.option_data_client = OptionHistoricalDataClient(
                        api_key=self.api_key,
                        secret_key=self.secret_key
                    )
                    logger.info("Alpaca OptionHistoricalDataClient initialized.")
                except Exception as opt_err:
                    logger.warning(f"Failed to initialize OptionHistoricalDataClient: {opt_err}. Options data unavailable.")
                    self.option_data_client = None
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
        _client_instance = self

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
            # Fetch active positions to calculate actual total unrealized profit/loss
            # on all open holdings. Use get_positions() so crypto cost basis is
            # corrected via our FIFO override (Alpaca's crypto avg_entry_price is
            # unreliable and would inflate/deflate unrealized PnL).
            try:
                positions = self.get_positions()
                unrealized_pnl = sum(float(p.get("unrealized_pnl", 0.0) or 0.0)
                                     for p in positions.values())
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

    def get_alpaca_portfolio_history(self, timeframe: str = "1D", period: str | None = None, extra_days: int | None = None) -> list[dict]:
        """Fetch the account's authoritative equity curve directly from Alpaca.

        This is the broker's own valuation history for THIS account (as opposed to
        the local SQLite ``portfolio_history`` table, which tracked the previous
        demo account). After the dexter cutover the dashboard must show the
        broker's real equity, not the stale DB-sourced curve.

        Returns a list of ``{"timestamp": iso-utc, "equity": float, "profit_loss": float, "profit_loss_pct": float}``
        ordered oldest -> newest. On any failure returns [] (dashboard falls back to DB).
        """
        if self.is_mock:
            return []
        try:
            kwargs = {"timeframe": timeframe}
            if period is not None:
                kwargs["period"] = period
            if extra_days is not None:
                kwargs["extra_days"] = extra_days
            hist = self.trading_client.get_portfolio_history(
                history_filter=GetPortfolioHistoryRequest(**kwargs)
            )
            if not hist or not hist.timestamp:
                return []
            out = []
            for i, ts in enumerate(hist.timestamp):
                eq = float(hist.equity[i]) if i < len(hist.equity) else 0.0
                pl = float(hist.profit_loss[i]) if i < len(hist.profit_loss) else 0.0
                pl_pct = hist.profit_loss_pct[i] if i < len(hist.profit_loss_pct) else None
                # Alpaca epoch seconds -> ISO-8601 UTC
                from datetime import datetime, timezone
                iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                out.append({
                    "timestamp": iso,
                    "equity": eq,
                    "profit_loss": pl,
                    "profit_loss_pct": (float(pl_pct) if pl_pct is not None else None),
                })
            return out
        except Exception as e:
            logger.error(f"Failed to fetch Alpaca portfolio history: {e}")
            return []

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

            def get_str(obj, attr, default=""):
                val = getattr(obj, attr, None)
                if val is None and isinstance(obj, dict):
                    val = obj.get(attr)
                return str(val).strip() if val is not None else default

            def get_float(obj, attr, default=None):
                val = getattr(obj, attr, None)
                if val is None and isinstance(obj, dict):
                    val = obj.get(attr)
                if val is None:
                    return default
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default

            for pos in positions:
                symbol = get_str(pos, "symbol").upper()
                if not symbol:
                    continue

                # If it's an option contract (OCC symbol), keep as-is under option handling
                if self.is_option_symbol(symbol):
                    positions_dict[symbol] = {
                        "qty": get_float(pos, "qty", 0.0),
                        "qty_available": get_float(pos, "qty_available", None) or get_float(pos, "qty", 0.0),
                        "market_value": get_float(pos, "market_value", 0.0),
                        "avg_entry_price": get_float(pos, "avg_entry_price", 0.0),
                        "unrealized_pnl": get_float(pos, "unrealized_pl", 0.0),
                        "is_option": True,
                    }
                    continue

                # Map Alpaca's slashless crypto symbol (e.g. "SOLUSD") back to standard universe representation (e.g. "SOL/USD")
                trading_universe = getattr(config, "TRADING_UNIVERSE", [])
                for u_symbol in trading_universe:
                    if "/" in u_symbol and u_symbol.replace("/", "").upper() == symbol:
                        symbol = u_symbol
                        break
                else:
                    # If not found in config.TRADING_UNIVERSE, but matches crypto pattern (e.g. ends with USD and >= 6 chars)
                    if symbol.endswith("USD") and len(symbol) >= 6 and "/" not in symbol:
                        symbol = f"{symbol[:-3]}/USD"

                qty = get_float(pos, "qty", 0.0)
                qty_available = get_float(pos, "qty_available", None)
                if qty_available is None:
                    qty_available = qty

                positions_dict[symbol] = {
                    "qty": qty,
                    "qty_available": qty_available,
                    "market_value": get_float(pos, "market_value", 0.0),
                    "avg_entry_price": get_float(pos, "avg_entry_price", 0.0),
                    "unrealized_pnl": get_float(pos, "unrealized_pl", 0.0)
                }

            # Override crypto cost basis with our own FIFO reconstruction.
            # Alpaca's avg_entry_price for crypto is unreliable (it can average
            # in realized sell prices / mishandle fractional-lot FIFO, e.g. SOL
            # showing $46 when the open lots are ~$109). Recompute from our
            # filled trades table so unrealized PnL reflects reality.
            self._apply_fifo_cost_basis_override(positions_dict)
            return positions_dict
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return {}

    def _apply_fifo_cost_basis_override(self, positions_dict: dict) -> None:
        """Overwrite crypto positions' avg_entry_price/unrealized_pnl in place.

        Alpaca's ``avg_entry_price`` for crypto is unreliable (it can average in
        realized sell prices / mishandle fractional-lot FIFO, e.g. SOL showing
        $46 when the true basis is ~$109). We recompute the correct cost basis
        from **Alpaca's own order history** — which has been verified to be
        accurate (FIFO comparison matches the live position) — rather than from
        our DB ``trades`` table, which can drift (missing/duplicated fills).

        Only symbols present in both the live positions and the broker-order
        FIFO basis are corrected; equity/option positions are left untouched.
        """
        try:
            basis = self._compute_cost_basis_from_orders()
        except Exception as e:
            logger.warning(f"FIFO cost-basis override unavailable: {e}")
            return
        for symbol, pos in positions_dict.items():
            if symbol not in basis:
                continue
            # NEVER override option positions: the FIFO cost-basis reconstruction
            # treats qty as shares (cost_basis = qty * price), but option cost
            # basis must include the 100x contract multiplier. Applying it here
            # corrupts option unrealized PnL (e.g. market_value - qty*price).
            # Alpaca's own unrealized_pl for options is authoritative.
            if self.is_option_symbol(symbol):
                continue
            b = basis[symbol]
            if b["qty"] <= 1e-9:
                continue
            market_value = float(pos.get("market_value", 0.0) or 0.0)
            live_qty = float(pos.get("qty", 0.0) or 0.0)
            broker_avg = float(pos.get("avg_entry_price", 0.0) or 0.0)

            # Live-qty guard: if the order-history FIFO open qty exceeds the
            # actual broker position (orders not fully captured), scale the cost
            # basis to the live qty so we don't attribute PnL to shares the
            # broker no longer holds.
            if live_qty > 1e-9 and b["qty"] > live_qty + 1e-9:
                cost_basis = b["cost_basis"] * (live_qty / b["qty"])
                logger.info(
                    f"FIFO cost-basis guard for {symbol}: order-history open "
                    f"{b['qty']:.4f} > live {live_qty:.4f}. Capping basis to live qty."
                )
            else:
                cost_basis = b["cost_basis"]

            avg_entry = b["avg_entry_price"]
            unrealized = market_value - cost_basis
            pos["avg_entry_price"] = avg_entry
            pos["unrealized_pnl"] = unrealized
            logger.info(
                f"FIFO cost-basis override for {symbol}: avg_entry "
                f"${avg_entry:.4f} (was ${broker_avg:.4f}), "
                f"unrealized ${unrealized:,.2f}"
            )

    def _compute_cost_basis_from_orders(self) -> dict:
        """Reconstruct open-position cost basis from Alpaca's order history.

        FIFO-matches every filled buy/sell order across symbols and returns
        ``{symbol: {"qty", "avg_entry_price", "cost_basis"}}`` for each symbol
        with a non-zero open position. This is the authoritative source because
        Alpaca's order history reproduces the live position (unlike the DB).
        """
        from collections import deque
        orders = self.get_executed_orders(limit=5000)  # paginated full history
        # Alpaca returns orders newest-first; FIFO matching REQUIRES chronological
        # (oldest-first) processing, so sort by timestamp ascending.
        orders = sorted(orders, key=lambda o: o.get("timestamp") or "")
        buy_queues = {}  # symbol -> deque[(qty, price)]
        for o in orders:
            symbol = o.get("symbol")
            if not symbol:
                continue
            side = (o.get("side") or "").lower()
            try:
                qty = float(o.get("qty") or 0.0)
                price = float(o.get("filled_avg_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            q = buy_queues.setdefault(symbol, deque())
            if side == "buy":
                q.append((qty, price))
            elif side == "sell":
                rem = qty
                while rem > 1e-9 and q:
                    lq, lp = q[0]
                    if lq <= rem:
                        rem -= lq
                        q.popleft()
                    else:
                        q[0] = (lq - rem, lp)
                        rem = 0
        result = {}
        for symbol, q in buy_queues.items():
            open_qty = sum(item_qty for item_qty, _ in q)
            if open_qty <= 1e-9:
                continue
            cost = sum(item_qty * item_price for item_qty, item_price in q)
            result[symbol] = {
                "qty": open_qty,
                "avg_entry_price": cost / open_qty,
                "cost_basis": cost,
            }
        return result

    def get_executed_orders(self, limit: int = 200, page_size: int = 2000) -> list[dict]:
        """Fetches filled/closed orders directly from Alpaca, paginating for full history.

        This catches TP/SL bracket fills and broker-side sells that the runner
        never logged to the local trades table. It pages through ALL closed
        orders (using the ``after`` cursor) so even old sells that closed prior
        positions are captured — otherwise the FIFO cost basis would attribute
        phantom open positions to the current holdings.

        Duplicate detection happens at the call site (dashboard cache worker /
        runner reconciliation) by comparing ``alpaca_order_id`` values.
        """
        if self.is_mock:
            return []

        try:
            executed = []
            until = None  # ISO timestamp cursor for pagination (go older)
            # Page backwards through time: fetch the most recent `page_size`
            # orders, then keep requesting older pages via `until` until we have
            # `limit` distinct filled orders or exhaust history.
            while len(executed) < limit:
                req_kwargs = {
                    "status": "closed",
                    "limit": page_size,
                }
                if until is not None:
                    req_kwargs["until"] = until
                page = self.trading_client.get_orders(
                    filter=GetOrdersRequest(**req_kwargs)
                )
                if not page:
                    break
                for order in page:
                    if order.filled_at is None:
                        continue
                    # Map Alpaca's slashless crypto symbol back
                    sym = (order.symbol or "").upper()
                    trading_universe = getattr(config, "TRADING_UNIVERSE", [])
                    for u_symbol in trading_universe:
                        if "/" in u_symbol and u_symbol.replace("/", "").upper() == sym:
                            sym = u_symbol
                            break
                    else:
                        if sym.endswith("USD") and len(sym) >= 6 and "/" not in sym:
                            sym = f"{sym[:-3]}/USD"
                    executed.append({
                        "alpaca_order_id": str(order.id),
                        "timestamp": str(order.filled_at.isoformat()) if hasattr(order.filled_at, "isoformat") else str(order.filled_at),
                        # Order CREATION time (when the order was submitted). For
                        # GTC limit orders this can be days before the fill, which
                        # is why the dashboard's fill-time and Alpaca's UI
                        # (created-time) can look mismatched. Expose both so the
                        # difference is visible.
                        "created_at": str(order.created_at.isoformat()) if hasattr(order.created_at, "isoformat") else str(order.created_at),
                        "symbol": sym,
                        # Use the enum's .value ("buy"/"sell") — str(OrderSide.BUY)
                        # is "OrderSide.BUY", which mislabels every order as "sell".
                        "side": "buy" if (order.side and (getattr(order.side, "value", None) == "buy" or str(order.side).lower() == "buy")) else "sell",
                        "qty": float(order.filled_qty or order.qty or 0),
                        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                        "status": str(order.status.value) if hasattr(order.status, "value") else str(order.status),
                    })
                if len(page) < page_size:
                    break  # got the oldest page; no more history
                # Advance cursor to the oldest filled_at on this page so the next
                # request returns older orders (via `until`).
                old_filled = [o.filled_at for o in page if o.filled_at is not None]
                if not old_filled:
                    break
                oldest_filled = min(old_filled)
                until = oldest_filled.isoformat() if hasattr(oldest_filled, "isoformat") else str(oldest_filled)
                if len(executed) >= limit:
                    break
            # Trim to requested limit, keeping most recent first (dedupe first).
            seen = set()
            unique = []
            for o in executed:
                if o["alpaca_order_id"] in seen:
                    continue
                seen.add(o["alpaca_order_id"])
                unique.append(o)
            unique.sort(key=lambda o: o["timestamp"], reverse=True)
            return unique[:limit]
        except Exception as e:
            logger.error(f"Error fetching executed orders from Alpaca: {e}")
            return []

    def get_historical_bars(self, symbol, limit: int = 100, timeframe_str: str = "day", max_retries: int = 3) -> pd.DataFrame:
        """Fetches historical daily or intraday bar data for a ticker or list of tickers (automatically handles Stocks or Crypto).
        
        Args:
            symbol: Single ticker or list of tickers
            limit: Number of bars to fetch
            timeframe_str: Timeframe for bars (e.g. 'day', '15min')
            max_retries: Maximum number of retry attempts
        
        Returns:
            pd.DataFrame: DataFrame containing historical bars
        """
        timeframe_str = timeframe_str.lower()
        
        # Check if symbol is list-like
        is_list = isinstance(symbol, (list, tuple, set, pd.Index))
        symbols_list = [sym.upper() for sym in (list(symbol) if is_list else [symbol])]
        
        if not symbols_list:
            logger.warning("No symbols provided for historical bars request")
            return pd.DataFrame()
            
        if self.is_mock:
            logger.info(f"Generating mock historical bars for {symbols_list} with timeframe {timeframe_str}")
            return self._generate_mock_bars(symbols_list, limit, timeframe_str, is_list)
                
        # Determine TimeFrame object from string
        tf, day_multiplier = self._get_timeframe(timeframe_str)
        
        # Partition into stock, crypto, and option symbols
        stock_symbols, crypto_symbols, option_symbols = self._partition_symbols(symbols_list)
        
        # Fetch last N days (making sure we cover weekends/holidays)
        start_time = datetime.now() - timedelta(days=limit * day_multiplier)
        
        # Try fetching data with retries
        stock_dfs = self._fetch_stock_data(stock_symbols, tf, start_time, max_retries)
        crypto_dfs = self._fetch_crypto_data(crypto_symbols, tf, start_time, max_retries)
        option_dfs = self._fetch_option_data(option_symbols, tf, start_time, max_retries)
        
        # Combine results
        all_dfs = stock_dfs + crypto_dfs + option_dfs
        if not all_dfs:
            logger.error(f"Failed to fetch historical bars for symbols: {symbols_list}")
            return pd.DataFrame()
            
        combined_df = pd.concat(all_dfs)
        
        if not is_list:
            single_sym = symbols_list[0]
            if isinstance(combined_df.index, pd.MultiIndex):
                if single_sym in combined_df.index.levels[0]:
                    combined_df = combined_df.xs(single_sym)
                else:
                    return pd.DataFrame()
            return combined_df.tail(limit)
        else:
            # Ensure the combined_df is multi-indexed and apply tail limit per symbol
            if isinstance(combined_df.index, pd.MultiIndex):
                combined_df = combined_df.groupby(level=0, group_keys=False).apply(lambda x: x.tail(limit))
            return combined_df

    def _generate_mock_bars(self, symbols_list: list[str], limit: int, timeframe_str: str, is_list: bool) -> pd.DataFrame:
        """Generate mock historical bars for testing."""
        all_dfs = []
        for sym in symbols_list:
            logger.info(f"Generating mock historical bars for {sym} with timeframe {timeframe_str}.")
            end_date = datetime.now()
            
            if timeframe_str == "day":
                dates = [end_date - timedelta(days=i) for i in range(limit)][::-1]
            else:
                dates = [end_date - timedelta(minutes=15 * i) for i in range(limit)][::-1]
                
            import numpy as np
            
            # Base price: Stock is around $400, Solana is around $140
            base_price = 140.0 if "SOL" in sym else 400.0
            
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
            if is_list:
                df = df.reset_index()
                df["symbol"] = sym
                df = df.set_index(["symbol", "timestamp"])
            all_dfs.append(df)
            
        if is_list:
            combined = pd.concat(all_dfs).sort_index()
            return combined
        else:
            return all_dfs[0]
            
    def _get_timeframe(self, timeframe_str: str) -> tuple:
        """Convert timeframe string to TimeFrame object and day multiplier."""
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
        return tf, day_multiplier
        
    def _partition_symbols(self, symbols_list: list[str]) -> tuple[list, list, list]:
        """Partition symbols into stock, crypto, and option lists.

        OCC option contracts (e.g. ``NVDA261016C00230000``) must NOT be sent to
        the stock or crypto bars endpoints — Alpaca rejects them with
        ``invalid symbol``. They are routed to the option bars path instead.
        """
        stock_symbols = []
        crypto_symbols = []
        option_symbols = []
        for sym in symbols_list:
            if self.is_option_symbol(sym):
                option_symbols.append(sym)
            elif "/" in sym or "USD" in sym:
                crypto_symbols.append(sym)
            else:
                stock_symbols.append(sym)
        return stock_symbols, crypto_symbols, option_symbols

    def _fetch_option_data(self, option_symbols: list[str], tf, start_time, max_retries: int) -> list[pd.DataFrame]:
        """Fetch option historical bars via the OptionHistoricalDataClient.

        Returns an empty list when no option symbols are requested or when option
        bars are unavailable, so the caller degrades gracefully instead of erroring.

        **Paper account note:** Alpaca *paper* accounts do not provide OPRA option
        data and the agreement cannot be signed (it is a live-account PDF agreement).
        On paper we therefore skip option bars entirely (the underlying's stock bars
        are still used for analysis) and log a calm, one-time notice rather than an
        error every cycle.
        """
        option_dfs = []
        if not option_symbols:
            return option_dfs
        # Paper accounts cannot access OPRA option bars -> skip quietly (once).
        if getattr(self, "paper", False):
            if not getattr(self, "_warned_paper_option_bars", False):
                self._warned_paper_option_bars = True
                logger.warning(
                    "Paper account: OPRA option bars are unavailable (agreement can't "
                    "be signed on paper). Skipping option bars; underlying stock bars "
                    "are used for analysis."
                )
            return option_dfs
        if self.option_data_client is None:
            logger.warning(f"Option data client unavailable; skipping option bars for {option_symbols}.")
            return option_dfs
        try:
            from alpaca.data.requests import OptionBarsRequest
            request_params = OptionBarsRequest(
                symbol_or_symbols=option_symbols,
                timeframe=tf,
                start=start_time,
                end=datetime.now()
            )
            bars = self._fetch_with_retry(self.option_data_client, request_params, self.option_data_client.get_option_bars, max_retries)
            if bars and bars.df is not None and not bars.df.empty:
                option_dfs.append(bars.df)
        except Exception as e:
            logger.warning(f"Batch option fetch failed for {option_symbols}: {e}. Retrying symbols individually.")
            for sym in option_symbols:
                try:
                    from alpaca.data.requests import OptionBarsRequest
                    request_params = OptionBarsRequest(
                        symbol_or_symbols=sym,
                        timeframe=tf,
                        start=start_time,
                        end=datetime.now()
                    )
                    bars = self._fetch_with_retry(self.option_data_client, request_params, self.option_data_client.get_option_bars, max_retries)
                    if bars and bars.df is not None and not bars.df.empty:
                        option_dfs.append(bars.df)
                except Exception as sym_err:
                    logger.error(f"Failed to fetch option bars for {sym}: {sym_err}")
        return option_dfs
        
    def _fetch_stock_data(self, stock_symbols: list[str], tf, start_time, max_retries: int) -> list[pd.DataFrame]:
        """Fetch stock data with retries and fallbacks."""
        stock_dfs = []
        if stock_symbols:
            try:
                stock_bars_request_kwargs = {
                    "symbol_or_symbols": stock_symbols,
                    "timeframe": tf,
                    "start": start_time,
                    "end": datetime.now()
                }
                if DATA_FEED_AVAILABLE:
                    stock_bars_request_kwargs["feed"] = DataFeed.IEX
                request_params = StockBarsRequest(**stock_bars_request_kwargs)
                bars = self._fetch_with_retry(self.data_client, request_params, self.data_client.get_stock_bars, max_retries)
                if bars and bars.df is not None and not bars.df.empty:
                    stock_dfs.append(bars.df)
            except Exception as e:
                logger.warning(f"Batch stock fetch failed for {stock_symbols}: {e}. Retrying symbols individually.")
                for sym in stock_symbols:
                    try:
                        stock_bars_request_kwargs = {
                            "symbol_or_symbols": sym,
                            "timeframe": tf,
                            "start": start_time,
                            "end": datetime.now()
                        }
                        if DATA_FEED_AVAILABLE:
                            stock_bars_request_kwargs["feed"] = DataFeed.IEX
                        request_params = StockBarsRequest(**stock_bars_request_kwargs)
                        bars = self._fetch_with_retry(self.data_client, request_params, self.data_client.get_stock_bars, max_retries)
                        if bars and bars.df is not None and not bars.df.empty:
                            stock_dfs.append(bars.df)
                    except Exception as sym_err:
                        logger.error(f"Failed to fetch stock bars for {sym}: {sym_err}")
        return stock_dfs
        
    def _fetch_crypto_data(self, crypto_symbols: list[str], tf, start_time, max_retries: int) -> list[pd.DataFrame]:
        """Fetch crypto data with retries and fallbacks."""
        crypto_dfs = []
        if crypto_symbols:
            try:
                request_params = CryptoBarsRequest(
                    symbol_or_symbols=crypto_symbols,
                    timeframe=tf,
                    start=start_time,
                    end=datetime.now()
                )
                bars = self._fetch_with_retry(self.crypto_data_client, request_params, self.crypto_data_client.get_crypto_bars, max_retries)
                if bars and bars.df is not None and not bars.df.empty:
                    crypto_dfs.append(bars.df)
            except Exception as e:
                logger.warning(f"Batch crypto fetch failed for {crypto_symbols}: {e}. Retrying symbols individually.")
                for sym in crypto_symbols:
                    try:
                        request_params = CryptoBarsRequest(
                            symbol_or_symbols=sym,
                            timeframe=tf,
                            start=start_time,
                            end=datetime.now()
                        )
                        bars = self._fetch_with_retry(self.crypto_data_client, request_params, self.crypto_data_client.get_crypto_bars, max_retries)
                        if bars and bars.df is not None and not bars.df.empty:
                            crypto_dfs.append(bars.df)
                    except Exception as sym_err:
                        logger.error(f"Failed to fetch crypto bars for {sym}: {sym_err}")
        return crypto_dfs
        
    def _fetch_with_retry(self, client, request_params, fetch_func, max_retries: int = 3):
        """Helper to fetch data with retry logic."""
        for attempt in range(max_retries):
            try:
                return fetch_func(request_params)
            except Exception as e:
                logger.warning(f"Fetch attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(1 * (attempt + 1))

        stock_dfs = []
        if stock_symbols:
            try:
                stock_bars_request_kwargs = {
                    "symbol_or_symbols": stock_symbols,
                    "timeframe": tf,
                    "start": start_time,
                    "end": datetime.now()
                }
                if DATA_FEED_AVAILABLE:
                    stock_bars_request_kwargs["feed"] = DataFeed.IEX
                request_params = StockBarsRequest(**stock_bars_request_kwargs)
                bars = fetch_with_retry(self.data_client, request_params, self.data_client.get_stock_bars)
                if bars and bars.df is not None and not bars.df.empty:
                    stock_dfs.append(bars.df)
            except Exception as e:
                logger.warning(f"Batch stock fetch failed for {stock_symbols}: {e}. Retrying symbols individually.")
                for sym in stock_symbols:
                    try:
                        stock_bars_request_kwargs = {
                            "symbol_or_symbols": sym,
                            "timeframe": tf,
                            "start": start_time,
                            "end": datetime.now()
                        }
                        if DATA_FEED_AVAILABLE:
                            stock_bars_request_kwargs["feed"] = DataFeed.IEX
                        request_params = StockBarsRequest(**stock_bars_request_kwargs)
                        bars = fetch_with_retry(self.data_client, request_params, self.data_client.get_stock_bars)
                        if bars and bars.df is not None and not bars.df.empty:
                            stock_dfs.append(bars.df)
                    except Exception as sym_err:
                        logger.error(f"Failed to fetch stock bars for {sym}: {sym_err}")

        crypto_dfs = []
        if crypto_symbols:
            try:
                request_params = CryptoBarsRequest(
                    symbol_or_symbols=crypto_symbols,
                    timeframe=tf,
                    start=start_time,
                    end=datetime.now()
                )
                bars = fetch_with_retry(self.crypto_data_client, request_params, self.crypto_data_client.get_crypto_bars)
                if bars and bars.df is not None and not bars.df.empty:
                    crypto_dfs.append(bars.df)
            except Exception as e:
                logger.warning(f"Batch crypto fetch failed for {crypto_symbols}: {e}. Retrying symbols individually.")
                for sym in crypto_symbols:
                    try:
                        request_params = CryptoBarsRequest(
                            symbol_or_symbols=sym,
                            timeframe=tf,
                            start=start_time,
                            end=datetime.now()
                        )
                        bars = fetch_with_retry(self.crypto_data_client, request_params, self.crypto_data_client.get_crypto_bars)
                        if bars and bars.df is not None and not bars.df.empty:
                            crypto_dfs.append(bars.df)
                    except Exception as sym_err:
                        logger.error(f"Failed to fetch crypto bars for {sym}: {sym_err}")

        all_dfs = stock_dfs + crypto_dfs
        if not all_dfs:
            return pd.DataFrame()
            
        combined_df = pd.concat(all_dfs)
        
        if not is_list:
            single_sym = symbols_list[0]
            if isinstance(combined_df.index, pd.MultiIndex):
                if single_sym in combined_df.index.levels[0]:
                    combined_df = combined_df.xs(single_sym)
                else:
                    return pd.DataFrame()
            return combined_df.tail(limit)
        else:
            # Ensure the combined_df is multi-indexed and apply tail limit per symbol
            if isinstance(combined_df.index, pd.MultiIndex):
                combined_df = combined_df.groupby(level=0, group_keys=False).apply(lambda x: x.tail(limit))
            return combined_df

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

    def get_latest_price(self, symbol: str) -> float:
        """Fetches the real-time latest trade price for a symbol with standard closes as fallbacks."""
        symbol = symbol.upper()
        if self.is_mock:
            return 140.0 if "SOL" in symbol else (2.50 if self.is_option_symbol(symbol) else 400.0)
            
        is_crypto = "/" in symbol or "USD" in symbol or "SOL" in symbol
        try:
            if self.is_option_symbol(symbol):
                from alpaca.data.requests import OptionLatestQuoteRequest
                req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
                res = self.option_data_client.get_option_latest_quote(req)
                quote = res.get(symbol)
                return float(getattr(quote, "midpoint", None) or getattr(quote, "last", None) or 0.0)
            elif is_crypto:
                from alpaca.data.requests import CryptoLatestTradeRequest
                req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
                res = self.crypto_data_client.get_crypto_latest_trade(req)
                return float(res[symbol].price)
            else:
                from alpaca.data.requests import StockLatestTradeRequest
                req = StockLatestTradeRequest(symbol_or_symbols=symbol)
                res = self.data_client.get_stock_latest_trade(req)
                return float(res[symbol].price)
        except Exception as e:
            logger.warning(f"Failed to fetch real-time price for {symbol} via latest trade API: {e}. Falling back to 15m historical close.")
            try:
                df = self.get_historical_bars(symbol, limit=2, timeframe_str="15min")
                if not df.empty:
                    return float(df.iloc[-1]["close"])
            except Exception as bar_err:
                logger.error(f"Fallback to historical bars failed for {symbol}: {bar_err}")
            raise ValueError(f"Could not resolve real-time or historical price for {symbol}: {e}")

    # ------------------------------------------------------------------
    # OPTIONS TRADING SUPPORT
    # ------------------------------------------------------------------

    @staticmethod
    def is_option_symbol(symbol: str) -> bool:
        """Detect whether a symbol is an OCC option contract (has digits, not crypto)."""
        clean = (symbol or "").upper().replace("/", "")
        has_digits = any(c.isdigit() for c in clean)
        is_crypto = "/" in symbol or clean.endswith("USD")
        return has_digits and not is_crypto

    def get_option_chain_snapshot(self, underlying_symbol: str, expiration_date_gte=None,
                                  expiration_date_lte=None, strike_price_gte=None,
                                  strike_price_lte=None, contract_type=None):
        """Fetches an option chain snapshot for an underlying symbol.

        Args:
            underlying_symbol: The underlying ticker (e.g. 'NVDA').
            expiration_date_gte/lte: YYYY-MM-DD expiry window.
            strike_price_gte/lte: Strike price window.
            contract_type: 'call' or 'put'.
        Returns:
            dict of OCC symbol -> snapshot, or {} if unavailable/mock.
        """
        if self.is_mock or self.option_data_client is None:
            logger.info(f"[MOCK] Option chain snapshot requested for {underlying_symbol} (type={contract_type}).")
            return {}
        try:
            request = OptionChainRequest(
                underlying_symbol=underlying_symbol.upper(),
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                type=contract_type
            )
            return self.option_data_client.get_option_chain(request)
        except Exception as e:
            logger.error(f"Error fetching option chain for {underlying_symbol}: {e}")
            return {}

    def get_latest_option_data(self, symbols):
        """Fetches latest option quotes for a list of OCC symbols."""
        if self.is_mock or self.option_data_client is None:
            logger.info(f"[MOCK] Latest option data requested for {symbols}.")
            return {}
        try:
            request = OptionLatestQuoteRequest(symbol_or_symbols=symbols)
            return self.option_data_client.get_option_latest_quote(request)
        except Exception as e:
            logger.error(f"Error fetching latest option data for {symbols}: {e}")
            return {}

    def place_option_order(self, symbol: str, qty: int, side: str, limit_price: float | None = None,
                           client_order_id: str | None = None) -> dict:
        """Places an option order (BUY-to-open or SELL-to-close).

        Options require whole qty, TIF DAY/GTC, extended_hours=false. Uses a
        limit order when limit_price is provided, otherwise a market order.
        """
        symbol = symbol.upper().replace(" ", "")
        side = side.lower()
        if self.is_mock:
            price = limit_price if limit_price else 2.50
            cost = price * 100 * qty
            if side == "buy":
                if cost > self.mock_cash:
                    raise ValueError(f"Insufficient mock funds. Cash: {self.mock_cash}, Order Cost: {cost}")
                self.mock_cash -= cost
                self.mock_positions[symbol] = self.mock_positions.get(symbol, 0) + qty
            elif side == "sell":
                current_qty = self.mock_positions.get(symbol, 0)
                if qty > current_qty:
                    raise ValueError(f"Insufficient mock option positions. Available: {current_qty}, Order Qty: {qty}")
                self.mock_cash += cost
                self.mock_positions[symbol] = current_qty - qty
                if self.mock_positions[symbol] <= 0:
                    del self.mock_positions[symbol]
            return {
                "id": f"mock-option-{int(datetime.now().timestamp())}",
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "filled_avg_price": price,
                "status": "filled",
                "order_type": "limit" if limit_price else "market",
                "is_option": True,
            }

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        try:
            if limit_price is not None:
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=str(int(qty)),
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(float(limit_price), 2),
                    client_order_id=client_order_id,
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=str(int(qty)),
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id,
                )
            order = self.trading_client.submit_order(order_data=req)
            order_id = str(order.id)
            logger.info(f"Option order submitted to Alpaca. Order ID: {order_id} ({side} {qty} {symbol}).")

            # Poll for fill
            filled_price = None
            status_str = str(getattr(order.status, "value", order.status))
            for _ in range(10):
                try:
                    updated = self.trading_client.get_order_by_id(order_id)
                    status_str = str(getattr(updated.status, "value", updated.status))
                    if updated.filled_avg_price is not None:
                        filled_price = float(updated.filled_avg_price)
                    if status_str in ("filled", "partially_filled"):
                        break
                except Exception as poll_err:
                    logger.warning(f"Error polling option order {order_id}: {poll_err}")
                time.sleep(0.5)

            return {
                "id": order_id,
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "filled_avg_price": filled_price,
                "status": status_str,
                "order_type": "limit" if limit_price else "market",
                "is_option": True,
            }
        except Exception as e:
            logger.error(f"Failed to place option order for {symbol}: {e}")
            raise

    def close_option_position(self, symbol: str) -> dict:
        """Closes an option position via market liquidation (SELL-to-close)."""
        symbol = symbol.upper().replace(" ", "")
        if self.is_mock:
            current_qty = self.mock_positions.get(symbol, 0)
            if current_qty <= 0:
                return {"id": f"mock-close-{int(time.time())}", "symbol": symbol, "qty": 0,
                        "side": "sell", "filled_avg_price": None, "status": "no_position"}
            self.mock_cash += current_qty * 2.50 * 100
            del self.mock_positions[symbol]
            return {"id": f"mock-close-{int(time.time())}", "symbol": symbol, "qty": current_qty,
                    "side": "sell", "filled_avg_price": 2.50, "status": "filled", "is_option": True}
        try:
            self.cancel_open_orders(symbol)
            closed = self.trading_client.close_position(symbol)
            return {
                "id": str(getattr(closed, "id", "")),
                "symbol": symbol,
                "qty": float(getattr(closed, "qty", 0) or 0),
                "side": "sell",
                "filled_avg_price": float(getattr(closed, "filled_avg_price", 0) or 0) or None,
                "status": str(getattr(closed, "status", "closed")),
                "is_option": True,
            }
        except Exception as e:
            logger.error(f"Failed to close option position {symbol}: {e}")
            raise

    def get_option_positions(self) -> dict:
        """Returns open option positions keyed by OCC symbol -> details dict."""
        if self.is_mock:
            return {sym: {"qty": qty, "qty_available": qty, "market_value": qty * 250.0,
                          "avg_entry_price": 2.50, "unrealized_pnl": 0.0, "is_option": True}
                    for sym, qty in self.mock_positions.items() if self.is_option_symbol(sym)}
        try:
            positions = self.trading_client.get_all_positions()
            result = {}
            for pos in positions:
                symbol = str(getattr(pos, "symbol", "")).upper()
                if not self.is_option_symbol(symbol):
                    continue
                result[symbol] = {
                    "qty": float(getattr(pos, "qty", 0) or 0),
                    "qty_available": float(getattr(pos, "qty_available", 0) or 0),
                    "market_value": float(getattr(pos, "market_value", 0) or 0),
                    "avg_entry_price": float(getattr(pos, "avg_entry_price", 0) or 0),
                    "unrealized_pnl": float(getattr(pos, "unrealized_pl", 0) or 0),
                    "is_option": True,
                }
            return result
        except Exception as e:
            logger.error(f"Error fetching option positions: {e}")
            return {}

    def get_options_buying_power(self) -> float:
        """Returns the account's options buying power (separate from equity buying power)."""
        if self.is_mock:
            return self.mock_cash * 2.0
        try:
            account = self.trading_client.get_account()
            obp = getattr(account, "options_buying_power", None)
            if obp is None:
                obp = getattr(account, "buying_power", 0.0)
            return float(obp or 0.0)
        except Exception as e:
            logger.error(f"Error fetching options buying power: {e}")
            return 0.0

    def execute_market_order(self, symbol: str, qty: float, side: str, take_profit_price: float | None = None, stop_loss_price: float | None = None) -> dict:
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
                "status": "filled",
                "order_type": "market",
                "fallback": False
            }
        
        try:
            order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
            is_crypto = "/" in symbol or "USD" in symbol or "SOL" in symbol

            def _submit_and_poll(req, order_type="market", fallback=False):
                """Submit an order and poll for fill confirmation."""
                order = self.trading_client.submit_order(order_data=req)
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
                    "status": status_str,
                    "order_type": order_type,
                    "fallback": fallback
                }

            if side == "buy" and take_profit_price is not None and stop_loss_price is not None:
                # Bracket (OCO) order with take-profit + stop-loss legs.
                # - Equities: Alpaca does NOT support fractional qty for brackets,
                #   so round to whole shares.
                # - Crypto: fractional qty is preserved; Alpaca may reject brackets
                #   on some crypto pairs, in which case we fall back to a plain
                #   market order (no TP/SL) so trading is never blocked.
                if is_crypto:
                    bracket_qty = qty
                else:
                    bracket_qty = int(qty)
                if bracket_qty < 1:
                    logger.warning(f"Bracket order qty for {symbol} rounds to {bracket_qty} (<1). Falling back to plain market order.")
                    market_order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=order_side,
                        time_in_force=TimeInForce.DAY
                    )
                    return _submit_and_poll(market_order_data, order_type="market", fallback=True)

                try:
                    market_order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=bracket_qty,
                        side=order_side,
                        time_in_force=TimeInForce.GTC,
                        order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(limit_price=take_profit_price),
                        stop_loss=StopLossRequest(stop_price=stop_loss_price)
                    )
                    logger.info(f"Submitting Exchange-Side Bracket Order for {symbol}: Buy {bracket_qty} shares (rounded from {qty}), Take-Profit at ${take_profit_price:.2f}, Stop-Loss at ${stop_loss_price:.2f}.")
                    return _submit_and_poll(market_order_data, order_type="bracket")
                except Exception as bracket_err:
                    logger.warning(f"Bracket order failed for {symbol}: {bracket_err}. Falling back to plain market order (no TP/SL).")
                    market_order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=bracket_qty,
                        side=order_side,
                        time_in_force=TimeInForce.DAY
                    )
                    return _submit_and_poll(market_order_data, order_type="market", fallback=True)
            else:
                time_in_force_val = TimeInForce.GTC if is_crypto else TimeInForce.DAY
                market_order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=time_in_force_val
                )
                try:
                    return _submit_and_poll(market_order_data, order_type="market")
                except Exception as frac_err:
                    # Alpaca only supports fractional quantities for eligible equities.
                    # If a fractional plain-market equity order is rejected, fall back to
                    # whole shares so the trade can still execute (without TP/SL legs).
                    if not is_crypto and float(qty) != int(qty):
                        whole_qty = int(qty)
                        if whole_qty >= 1:
                            logger.warning(
                                f"Fractional market order for {symbol} (qty={qty}) rejected: {frac_err}. "
                                f"Retrying with whole shares (qty={whole_qty})."
                            )
                            market_order_data = MarketOrderRequest(
                                symbol=symbol,
                                qty=whole_qty,
                                side=order_side,
                                time_in_force=time_in_force_val
                            )
                            return _submit_and_poll(market_order_data, order_type="market", fallback=True)
                    raise
        except Exception as e:
            err_msg = str(e)
            # Check if this is an Alpaca validation error about stop_loss.stop_price or take_profit.limit_price
            if "stop_loss.stop_price" in err_msg or "take_profit.limit_price" in err_msg:
                import re
                import json
                
                actual_base_price = None
                try:
                    # Attempt to parse as JSON or extract via regex
                    match = re.search(r'\{.*\}', err_msg)
                    if match:
                        err_json = json.loads(match.group(0))
                        if "base_price" in err_json:
                            actual_base_price = float(err_json["base_price"])
                except Exception as parse_err:
                    logger.warning(f"Could not parse base_price from error JSON: {parse_err}")
                
                # If JSON parsing failed, try direct float regex matching
                if not actual_base_price:
                    try:
                        price_match = re.search(r'\"base_price\":\"([\d\.]+)\"', err_msg)
                        if price_match:
                            actual_base_price = float(price_match.group(1))
                    except Exception:
                        pass
                
                if actual_base_price:
                    logger.info(f"Alpaca validation rejected bracket order. Extracted actual base_price: ${actual_base_price:.2f}")
                    # Dynamically adjust take_profit and stop_loss to be valid relative to actual_base_price
                    # A robust safe fallback is:
                    # stop-loss should be at least max_allowed_stop relative to actual_base_price
                    max_allowed_stop = round(min(actual_base_price * 0.995, actual_base_price - 0.05), 2)
                    adjusted_sl = min(stop_loss_price, max_allowed_stop) if stop_loss_price else max_allowed_stop
                    
                    min_allowed_tp = round(actual_base_price + 0.05, 2)
                    adjusted_tp = max(take_profit_price, min_allowed_tp) if take_profit_price else min_allowed_tp
                    
                    logger.info(f"Retrying bracket order submission with corrected levels: TP: ${adjusted_tp:.2f}, SL: ${adjusted_sl:.2f}")
                    
                    try:
                        market_order_data = MarketOrderRequest(
                            symbol=symbol,
                            qty=qty,
                            side=order_side,
                            time_in_force=TimeInForce.GTC,
                            order_class=OrderClass.BRACKET,
                            take_profit=TakeProfitRequest(limit_price=adjusted_tp),
                            stop_loss=StopLossRequest(stop_price=adjusted_sl)
                        )
                        order = self.trading_client.submit_order(order_data=market_order_data)
                        order_id = str(order.id)
                        logger.info(f"Bracket order retry submitted successfully! Order ID: {order_id}.")
                        
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
                    except Exception as retry_err:
                        logger.error(f"Failed retry of corrected bracket order: {retry_err}")
                        raise retry_err
                
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
