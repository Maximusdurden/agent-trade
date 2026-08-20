import sqlite3
import json
from datetime import datetime, timezone
from collections import defaultdict
from core.config import DATABASE_PATH

def get_db_connection():
    """Returns a connection to the SQLite database with row factory enabled."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_last_insert_id(cursor: sqlite3.Cursor) -> int:
    """Return the inserted row ID, raising if SQLite did not provide one."""
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("SQLite did not return an ID for the inserted row")
    return lastrowid

def init_db():
    """Initializes the database schema if tables don't exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Decisions Table (LLM outputs & indicators)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker_indicators TEXT, -- JSON representation of indicators (RSI, MA, etc.)
                portfolio_state TEXT,    -- JSON representation of current positions, cash, etc.
                thought_process TEXT,    -- LLM's raw thought process
                proposed_action TEXT,    -- BUY, SELL, HOLD
                proposed_symbol TEXT,    -- Symbol to trade
                proposed_qty REAL,       -- Quantity to trade
                is_approved INTEGER,     -- 1 if passed guardrails, 0 if rejected
                rejection_reason TEXT,   -- Why did it fail guardrails, if any
                direction TEXT,          -- bullish | bearish | neutral (conviction model)
                conviction REAL,         -- 0.0-1.0 conviction score
                instrument TEXT          -- stock | crypto | option (resolved by guardrails)
            )
        """)
        
        # 2. Trades Table (Executed orders)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER,
                alpaca_order_id TEXT UNIQUE,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,       -- buy or sell
                qty REAL NOT NULL,
                filled_avg_price REAL,
                status TEXT NOT NULL,     -- filled, failed, open, canceled
                option_type TEXT,         -- call | put (options only)
                option_dte INTEGER,       -- days-to-expiry (options only)
                strike REAL,              -- strike price (options only)
                contract_symbol TEXT,     -- underlying root (options only)
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            )
        """)
        
        # 2b. Executions Table (Per-attempt order execution log)
        # Each decision can have MULTIPLE execution attempts (e.g. a failed
        # bracket attempt followed by a plain-market fallback). This table
        # records every attempt so the dashboard can reconcile what was
        # decided vs what actually reached the broker.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,          -- buy or sell
                qty REAL NOT NULL,
                order_type TEXT,             -- bracket, market
                status TEXT NOT NULL,        -- submitted, failed, filled, canceled
                error TEXT,                  -- error message if failed
                alpaca_order_id TEXT,
                filled_avg_price REAL,
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            )
        """)

        # 3. Portfolio History Table (Performance tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_history (
                timestamp TEXT PRIMARY KEY,
                equity REAL NOT NULL,
                cash REAL NOT NULL,
                unrealized_pnl REAL NOT NULL
            )
        """)
        
        # 4. Strategy History Table (Meta-brain dynamic rules)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                yesterdays_rules TEXT,
                todays_rules TEXT,
                meta_reasoning TEXT
            )
        """)
        
        # 5. Watchlist History Table (Autonomous Screener logs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                watchlist TEXT NOT NULL -- JSON representation of selected tickers
            )
        """)
        
        # 6. System State Table (Global persistent parameters)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # --- Migrations for pre-existing databases ---
        def add_column_if_missing(table: str, column: str, decl: str) -> None:
            cols = [r[1] for r in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        add_column_if_missing("decisions", "direction", "TEXT")
        add_column_if_missing("decisions", "conviction", "REAL")
        add_column_if_missing("decisions", "instrument", "TEXT")
        add_column_if_missing("trades", "option_type", "TEXT")
        add_column_if_missing("trades", "option_dte", "INTEGER")
        add_column_if_missing("trades", "strike", "REAL")
        add_column_if_missing("trades", "contract_symbol", "TEXT")

        conn.commit()

def log_decision(ticker_indicators: dict, portfolio_state: dict, thought_process: str,
                 proposed_action: str, proposed_symbol: str, proposed_qty: float,
                 is_approved: bool, rejection_reason: str | None = None,
                 direction: str | None = None, conviction: float | None = None,
                 instrument: str | None = None) -> int:
    """Logs the LLM decision to the SQLite database and returns the decision ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO decisions (
                timestamp, ticker_indicators, portfolio_state, thought_process,
                proposed_action, proposed_symbol, proposed_qty, is_approved, rejection_reason,
                direction, conviction, instrument
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            json.dumps(ticker_indicators),
            json.dumps(portfolio_state),
            thought_process,
            proposed_action.upper(),
            proposed_symbol.upper() if proposed_symbol else None,
            proposed_qty,
            1 if is_approved else 0,
            rejection_reason,
            direction,
            conviction,
            instrument
        ))
        conn.commit()
        return get_last_insert_id(cursor)

def log_trade(decision_id: int | None, alpaca_order_id: str, symbol: str,
              side: str, qty: float, filled_avg_price: float | None, status: str,
              option_type: str | None = None, option_dte: int | None = None,
              strike: float | None = None, contract_symbol: str | None = None) -> int:
    """Logs an executed trade to the SQLite database."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO trades (
                decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status,
                option_type, option_dte, strike, contract_symbol
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            decision_id,
            alpaca_order_id,
            datetime.utcnow().isoformat(),
            symbol.upper(),
            side.lower(),
            qty,
            filled_avg_price,
            status,
            option_type,
            option_dte,
            strike,
            contract_symbol
        ))
        conn.commit()
        return get_last_insert_id(cursor)

def log_execution(decision_id: int | None, attempt: int, symbol: str, side: str,
                  qty: float, order_type: str | None, status: str,
                  error: str | None = None, alpaca_order_id: str | None = None,
                  filled_avg_price: float | None = None) -> int:
    """Logs a single order-execution attempt to the executions table.

    Records every attempt (including failures) so the dashboard can reconcile
    decisions against what actually reached the broker.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO executions (
                decision_id, attempt, timestamp, symbol, side, qty,
                order_type, status, error, alpaca_order_id, filled_avg_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            decision_id,
            attempt,
            datetime.utcnow().isoformat(),
            symbol.upper(),
            side.lower(),
            qty,
            order_type,
            status,
            error,
            alpaca_order_id,
            filled_avg_price
        ))
        conn.commit()
        return get_last_insert_id(cursor)

def get_executions(limit: int = 50) -> list[dict]:
    """Returns the most recent execution attempts as a list of dicts."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM executions ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_executions_for_decision(decision_id: int) -> list[dict]:
    """Returns all execution attempts for a given decision, oldest first."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM executions WHERE decision_id = ? ORDER BY id ASC
        """, (decision_id,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def log_portfolio_history(equity: float, cash: float, unrealized_pnl: float):
    """Saves portfolio metrics history."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO portfolio_history (timestamp, equity, cash, unrealized_pnl)
            VALUES (?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            equity,
            cash,
            unrealized_pnl
        ))
        conn.commit()

def get_recent_decisions(limit: int = 5) -> list[dict]:
    """Returns the most recent decisions as a list of dicts."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM decisions ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_recent_trades(limit: int = 10) -> list[dict]:
    """Returns the most recent executed trades as a list of dicts."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM trades ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def log_strategy_history(ticker: str, yesterdays_rules: str | None, todays_rules: str, meta_reasoning: str) -> int:
    """Logs a daily strategy shift for a ticker."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO strategy_history (timestamp, ticker, yesterdays_rules, todays_rules, meta_reasoning)
            VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            ticker.upper(),
            yesterdays_rules,
            todays_rules,
            meta_reasoning
        ))
        conn.commit()
        return get_last_insert_id(cursor)

def get_active_strategy(ticker: str) -> str:
    """Retrieve the latest persisted strategy, without synthesizing fallback rules."""
    ticker = ticker.upper()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT todays_rules FROM strategy_history WHERE ticker = ? ORDER BY id DESC LIMIT 1
        """, (ticker,))
        row = cursor.fetchone()
        if row:
            return row["todays_rules"]
        
        return f"No active strategy rules defined for {ticker}."

def get_performance_summary() -> dict:
    """
    Computes a comprehensive summary of historical successes and failures
    based on trades and portfolio history in the database.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Fetch all filled or partially filled trades chronologically
        try:
            cursor.execute("SELECT * FROM trades WHERE status IN ('filled', 'partially_filled') ORDER BY id ASC")
            trades = [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            trades = []

        buy_queues = defaultdict(list)
        total_realized_pnl = 0.0
        wins = 0
        losses = 0
        total_trades_count = 0
        closed_trades_pnl_list = []
        
        for t in trades:
            symbol = t["symbol"].upper()
            if symbol == "SOLUSD":
                symbol = "SOL/USD"
            side = t["side"].lower()
            qty = float(t["qty"])
            price = float(t["filled_avg_price"]) if t["filled_avg_price"] else 0.0
            
            if side == "buy":
                buy_queues[symbol].append({"qty": qty, "price": price})
            elif side == "sell":
                temp_qty = qty
                trade_realized_pnl = 0.0
                matched_any = False
                
                while temp_qty > 0 and buy_queues[symbol]:
                    oldest_buy = buy_queues[symbol][0]
                    buy_qty = oldest_buy["qty"]
                    buy_price = oldest_buy["price"]
                    
                    if buy_qty <= temp_qty:
                        realized = buy_qty * (price - buy_price)
                        trade_realized_pnl += realized
                        temp_qty -= buy_qty
                        buy_queues[symbol].pop(0)
                        matched_any = True
                    else:
                        realized = temp_qty * (price - buy_price)
                        trade_realized_pnl += realized
                        oldest_buy["qty"] -= temp_qty
                        temp_qty = 0
                        matched_any = True
                
                if matched_any:
                    total_trades_count += 1
                    total_realized_pnl += trade_realized_pnl
                    closed_trades_pnl_list.append(trade_realized_pnl)
                    if trade_realized_pnl > 0:
                        wins += 1
                    else:
                        losses += 1

        win_rate = (wins / total_trades_count * 100) if total_trades_count > 0 else 0.0
        avg_pnl = (total_realized_pnl / total_trades_count) if total_trades_count > 0 else 0.0
        max_win = max(closed_trades_pnl_list) if closed_trades_pnl_list else 0.0
        max_loss = min(closed_trades_pnl_list) if closed_trades_pnl_list else 0.0

        # 2. Get Portfolio History Max Drawdown and Peak Equity
        try:
            cursor.execute("SELECT equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp ASC")
            portfolio_rows = [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            portfolio_rows = []

        max_drawdown = 0.0
        peak_equity = 0.0
        current_equity = 100000.0
        current_cash = 100000.0
        current_unrealized = 0.0
        
        for row in portfolio_rows:
            eq = row["equity"]
            current_equity = eq
            current_cash = row["cash"]
            current_unrealized = row["unrealized_pnl"]
            
            if eq > peak_equity:
                peak_equity = eq
            if peak_equity > 0:
                dd = (peak_equity - eq) / peak_equity
                if dd > max_drawdown:
                    max_drawdown = dd

        max_drawdown_pct = max_drawdown * 100

        # Formulate rich text narrative
        text_summary = (
            f"=== HISTORICAL SUCCESSES & FAILURES (LEARNING ENGINE) ===\n"
            f"- Total Completed (Closed) Trades: {total_trades_count}\n"
            f"- Profitable Trades (Wins): {wins} | Unprofitable Trades (Losses): {losses}\n"
            f"- Current Historical Win Rate: {win_rate:.2f}%\n"
            f"- Total Realized Net Profit/Loss: ${total_realized_pnl:+,.2f}\n"
            f"- Average Return per Closed Trade: ${avg_pnl:+,.2f}\n"
            f"- Largest Successful Win: ${max_win:+,.2f} | Largest Failed Loss: ${max_loss:+,.2f}\n"
            f"- Peak Historical Portfolio Value: ${peak_equity:,.2f}\n"
            f"- Current Portfolio Value: ${current_equity:,.2f}\n"
            f"- Maximum Historical Peak-to-Trough Drawdown: {max_drawdown_pct:.2f}%\n"
            "COGNITIVE LESSONS FOR SYSTEM ADAPTATION:\n"
            "1. If win rate is below 50% or maximum drawdown exceeds 15%, tighten guardrails, reduce trade sizing to defensive (1-3% of equity), and prioritize capital preservation.\n"
            "2. Identify which assets are causing the largest failed losses, and avoid repeating high-volatility purchases without strong technical confirmations (e.g. oversold RSI with MACD bullish crossover).\n"
            "3. Leverage previous successes by identifying profitable entry zones (e.g., strong bounces off Fibonacci support) and stick to those regimes."
        )

        return {
            "total_trades": total_trades_count,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_realized_pnl": total_realized_pnl,
            "avg_pnl": avg_pnl,
            "max_win": max_win,
            "max_loss": max_loss,
            "peak_equity": peak_equity,
            "current_equity": current_equity,
            "max_drawdown_pct": max_drawdown_pct,
            "text_summary": text_summary
        }

def get_daily_performance_breakdown(limit: int = 15) -> str:
    """
    Computes a chronological daily performance breakdown showing daily ending equity,
    daily change (PnL), completed closed trades, win rate %, and realized PnL.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 1. Fetch trades
            try:
                cursor.execute("SELECT timestamp, symbol, side, qty, filled_avg_price FROM trades WHERE status IN ('filled', 'partially_filled') ORDER BY id ASC")
                trades = [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                trades = []

            # 2. Fetch portfolio history
            try:
                cursor.execute("SELECT date(timestamp) as day, equity, cash FROM portfolio_history WHERE timestamp IN (SELECT MAX(timestamp) FROM portfolio_history GROUP BY date(timestamp)) ORDER BY timestamp ASC")
                eq_rows = [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                eq_rows = []

        buy_queues = defaultdict(list)
        daily_trades = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'total': 0})

        for t in trades:
            symbol = t["symbol"].upper()
            side = t["side"].lower()
            qty = float(t["qty"])
            price = float(t["filled_avg_price"]) if t["filled_avg_price"] else 0.0
            date_str = t["timestamp"].split('T')[0]
            
            if side == 'buy':
                buy_queues[symbol].append({'qty': qty, 'price': price})
            elif side == 'sell':
                temp_qty = qty
                trade_pnl = 0.0
                matched = False
                while temp_qty > 0 and buy_queues[symbol]:
                    oldest = buy_queues[symbol][0]
                    if oldest['qty'] <= temp_qty:
                        trade_pnl += oldest['qty'] * (price - oldest['price'])
                        temp_qty -= oldest['qty']
                        buy_queues[symbol].pop(0)
                        matched = True
                    else:
                        trade_pnl += temp_qty * (price - oldest['price'])
                        oldest['qty'] -= temp_qty
                        temp_qty = 0
                        matched = True
                if matched:
                    daily_trades[date_str]['total'] += 1
                    daily_trades[date_str]['pnl'] += trade_pnl
                    if trade_pnl > 0:
                        daily_trades[date_str]['wins'] += 1
                    else:
                        daily_trades[date_str]['losses'] += 1

        lines = ["=== DAILY PORTFOLIO PERFORMANCE HISTORY ==="]
        prev_eq = None
        for row in eq_rows[-limit:]:
            day = row["day"]
            eq = row["equity"]
            t_stats = daily_trades.get(day, {'wins': 0, 'losses': 0, 'pnl': 0.0, 'total': 0})
            change = (eq - prev_eq) if prev_eq is not None else 0.0
            win_rate = (t_stats['wins'] / t_stats['total'] * 100) if t_stats['total'] > 0 else 0.0
            lines.append(f"- {day}: Ending Equity = ${eq:,.2f} (Daily Change = ${change:+,.2f}) | Closed Trades = {t_stats['total']} | Win Rate = {win_rate:.1f}% | Realized PnL = ${t_stats['pnl']:+,.2f}")
            prev_eq = eq
            
        return "\n".join(lines)
    except Exception as e:
        return f"Error computing daily performance: {e}"

def log_watchlist(symbols: list[str]) -> int:
    """Logs the chosen watchlist to the database."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO watchlist_history (timestamp, watchlist)
            VALUES (?, ?)
        """, (
            datetime.utcnow().isoformat(),
            json.dumps(symbols)
        ))
        conn.commit()
        return get_last_insert_id(cursor)

# Auto-initialize database on import so the tables exist right away
init_db()


from typing import Optional

def set_system_state(key: str, value: str) -> None:
    """Set a key-value pair in the system_state table."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO system_state (key, value)
            VALUES (?, ?)
            """,
            (key, value)
        )
        conn.commit()

def get_system_state(key: str) -> Optional[str]:
    """Get a value from the system_state table by key."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT value FROM system_state WHERE key = ?",
                (key,)
            )
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.OperationalError:
            return None


HEARTBEAT_KEYS = (
    "last_cycle_started_at",
    "last_cycle_completed_at",
    "last_cycle_status",
    "last_cycle_asset_scope",
    "last_cycle_message",
    "last_cycle_execution_id",
)


def record_cycle_heartbeat(status: str, asset_scope: str = "UNKNOWN",
                           message: str = "", execution_id: str = "") -> None:
    """Persist operational cycle state separately from trading decisions.

    These heartbeat keys let the dashboard determine runner freshness
    independently of whether the AI brain happened to generate a trade.
    Every call to ``run_trading_cycle`` records STARTED before the impl
    and FAILED or COMPLETED in a ``finally`` block.
    """
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if status == "STARTED":
        set_system_state("last_cycle_started_at", now_utc)
        set_system_state("last_cycle_completed_at", "")
    else:
        set_system_state("last_cycle_completed_at", now_utc)
    set_system_state("last_cycle_status", status)
    set_system_state("last_cycle_asset_scope", asset_scope)
    set_system_state("last_cycle_message", message)
    set_system_state("last_cycle_execution_id", execution_id)


def get_cycle_heartbeat() -> dict:
    """Return a normalized heartbeat object, including for pre-heartbeat databases."""
    return {
        key.removeprefix("last_cycle_"): get_system_state(key) or ""
        for key in HEARTBEAT_KEYS
    }


def get_latest_watchlist_raw() -> list[str]:
    """Retrieves the latest watchlist as a raw list of strings."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT watchlist FROM watchlist_history ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                return json.loads(row["watchlist"])
    except Exception:
        pass
    return []


class Database:
    def __init__(self, db_path: Optional[str] = None):
        from core import config
        self.db_path = db_path or str(config.DATABASE_PATH)
        
    def log_decision(self, **kwargs) -> int:
        return log_decision(**kwargs)
        
    def log_trade(self, **kwargs) -> int:
        return log_trade(**kwargs)

    def log_execution(self, **kwargs) -> int:
        return log_execution(**kwargs)

    def get_executions(self, limit: int = 50) -> list[dict]:
        return get_executions(limit)

    def get_executions_for_decision(self, decision_id: int) -> list[dict]:
        return get_executions_for_decision(decision_id)

    def get_recent_decisions(self, limit: int = 5) -> list[dict]:
        return get_recent_decisions(limit)
        
    def get_recent_trades(self, limit: int = 15) -> list[dict]:
        return get_recent_trades(limit)
        
    def log_portfolio_history(self, equity: float, cash: float, unrealized_pnl: float) -> Optional[int]:
        return log_portfolio_history(equity, cash, unrealized_pnl)

    def set_system_state(self, key: str, value: str) -> None:
        set_system_state(key, value)

    def get_system_state(self, key: str) -> Optional[str]:
        return get_system_state(key)

    def record_cycle_heartbeat(self, status: str, asset_scope: str = "UNKNOWN",
                               message: str = "", execution_id: str = "") -> None:
        record_cycle_heartbeat(status, asset_scope, message, execution_id)

    def get_cycle_heartbeat(self) -> dict:
        return get_cycle_heartbeat()

    def get_latest_watchlist_raw(self) -> list[str]:
        return get_latest_watchlist_raw()

    def get_active_strategy(self, ticker: str) -> str:
        return get_active_strategy(ticker)

