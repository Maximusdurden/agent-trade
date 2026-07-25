import logging
from datetime import datetime
from collections import defaultdict
import pandas as pd
import numpy as np

from core import config
from core.database import get_db_connection
from core.discord_notifier import send_discord_webhook

logger = logging.getLogger("PerformanceAuditor")

def parse_eastern_date(iso_ts_str: str) -> str:
    """Parses UTC ISO string and returns date string in New York timezone."""
    try:
        dt = datetime.fromisoformat(iso_ts_str)
        from zoneinfo import ZoneInfo
        dt_utc = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_ny = dt_utc.astimezone(ZoneInfo("America/New_York"))
        return dt_ny.strftime("%Y-%m-%d")
    except Exception:
        try:
            import pytz
            dt = datetime.fromisoformat(iso_ts_str)
            dt_utc = dt.replace(tzinfo=pytz.utc)
            dt_ny = dt_utc.astimezone(pytz.timezone("pytz.timezone('America/New_York')"))
            return dt_ny.strftime("%Y-%m-%d")
        except Exception:
            return iso_ts_str.split("T")[0]

def format_ts_et(iso_ts_str: str) -> str:
    """Parses UTC ISO string and returns time string in HH:MM:SS Eastern Time."""
    try:
        dt = datetime.fromisoformat(iso_ts_str)
        from zoneinfo import ZoneInfo
        dt_utc = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_ny = dt_utc.astimezone(ZoneInfo("America/New_York"))
        return dt_ny.strftime("%H:%M:%S")
    except Exception:
        return iso_ts_str.split("T")[-1][:8]

def compile_daily_report_embeds(date_str: str = None) -> tuple[dict, dict] | None:
    """
    Retrieves and aggregates today's trade events, computes FIFO realized PnL,
    starting/ending equity, and compiles summary and detailed rich embeds.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = None
        
    target_date = datetime.now(tz).strftime("%Y-%m-%d") if not date_str else date_str
    logger.info(f"Compiling Daily Performance Report for {target_date}...")
    
    # 1. Fetch all trades to run standard chronological FIFO matching
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status IN ('filled', 'partially_filled') ORDER BY id ASC")
            trades = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch trades from DB: {e}")
        return None

    buy_queues = defaultdict(list)
    today_buys_count = 0
    today_sells_count = 0
    today_cash_used_for_buys = 0.0
    today_realized_pnl = 0.0
    detailed_actions = []

    for t in trades:
        symbol = t["symbol"].upper()
        if symbol == "SOLUSD":
            symbol = "SOL/USD"
        side = t["side"].lower()
        qty = float(t["qty"])
        price = float(t["filled_avg_price"]) if t["filled_avg_price"] else 0.0
        ts = t["timestamp"]
        trade_date = parse_eastern_date(ts)
        
        is_today = (trade_date == target_date)
        
        if side == "buy":
            buy_queues[symbol].append({"qty": qty, "price": price})
            if is_today:
                today_buys_count += 1
                today_cash_used_for_buys += qty * price
                detailed_actions.append({
                    "time": format_ts_et(ts),
                    "symbol": symbol,
                    "side": "BUY",
                    "qty": qty,
                    "price": price,
                    "realized_pnl": 0.0
                })
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
                    
            if is_today:
                today_sells_count += 1
                if matched_any:
                    today_realized_pnl += trade_realized_pnl
                detailed_actions.append({
                    "time": format_ts_et(ts),
                    "symbol": symbol,
                    "side": "SELL",
                    "qty": qty,
                    "price": price,
                    "realized_pnl": trade_realized_pnl if matched_any else 0.0
                })

    # If no trades occurred and it's a weekend/holiday, let's report zero trade state gracefully.
    if today_buys_count == 0 and today_sells_count == 0:
        logger.info(f"No trades executed on {target_date}. Creating empty report.")
        
    # 2. Fetch starting and ending equity for target_date from portfolio_history
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, equity, cash FROM portfolio_history ORDER BY timestamp ASC")
            rows = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"Could not read portfolio history: {e}")
        rows = []

    history = [{"date": parse_eastern_date(r["timestamp"]), "equity": r["equity"]} for r in rows]
    today_rows = [h for h in history if h["date"] == target_date]
    prior_rows = [h for h in history if h["date"] < target_date]

    # Ending Equity
    if today_rows:
        ending_equity = today_rows[-1]["equity"]
    else:
        # Fallback to current Alpaca state
        try:
            from core.alpaca_client import AlpacaClient
            ac = AlpacaClient()
            ac_state = ac.get_account_state()
            ending_equity = ac_state["equity"]
        except Exception:
            ending_equity = 100000.0

    # Starting Equity
    if prior_rows:
        starting_equity = prior_rows[-1]["equity"]
    elif today_rows:
        starting_equity = today_rows[0]["equity"]
    else:
        starting_equity = 100000.0

    net_equity_change = ending_equity - starting_equity
    net_equity_change_pct = (net_equity_change / starting_equity * 100) if starting_equity > 0 else 0.0

    # Color code: Green (0x2ecc71) for positive net change, Red (0xe74c3c) for negative/neutral
    color = 0x2ecc71 if net_equity_change >= 0.0 else 0xe74c3c

    # Build Message A: Summary Embed
    summary_embed = {
        "title": f"📊 End-of-Day Portfolio Summary ({target_date})",
        "description": f"Daily Performance Audit Report for the automated `agent-trade` strategy.",
        "color": color,
        "fields": [
            {"name": "Total Trades Today", "value": f"🟢 Buys: **{today_buys_count}** | 🔴 Sells: **{today_sells_count}**", "inline": True},
            {"name": "Capital Allocated", "value": f"${today_cash_used_for_buys:,.2f}", "inline": True},
            {"name": "Realized PnL (FIFO)", "value": f"**${today_realized_pnl:+,.2f}**", "inline": True},
            {"name": "Starting Equity", "value": f"${starting_equity:,.2f}", "inline": True},
            {"name": "Ending Equity", "value": f"${ending_equity:,.2f}", "inline": True},
            {"name": "Net Equity Change", "value": f"**${net_equity_change:+,.2f} ({net_equity_change_pct:+.2f}%)**", "inline": True}
        ],
        "footer": {"text": f"Performance Auditor | Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
    }

    # Build Message B: Detailed Breakdown Embed
    if detailed_actions:
        lines = [
            f"{'Time (ET)':<10} | {'Ticker':<8} | {'Side':<5} | {'Qty':<6} | {'Price':<8} | {'Realized PnL':<12}",
            "-" * 60
        ]
        for a in detailed_actions:
            pnl_str = f"${a['realized_pnl']:+,.2f}" if a["side"] == "SELL" and a["realized_pnl"] != 0.0 else "-"
            lines.append(f"{a['time']:<10} | {a['symbol']:<8} | {a['side']:<5} | {a['qty']:<6.1f} | ${a['price']:<7.2f} | {pnl_str:<12}")
            
        table_str = "\n".join(lines)
        details_embed = {
            "title": f"📋 Detailed Trade Ledger ({target_date})",
            "description": f"```\n{table_str}\n```",
            "color": color
        }
    else:
        details_embed = {
            "title": f"📋 Detailed Trade Ledger ({target_date})",
            "description": "*No trades executed today.*",
            "color": color
        }

    return summary_embed, details_embed

def send_eod_report(date_str: str = None) -> bool:
    """Compiles and transmits the end-of-day performance report to Discord."""
    res = compile_daily_report_embeds(date_str)
    if not res:
        logger.error("Could not compile daily report embeds.")
        return False
        
    summary_embed, details_embed = res
    
    # Send both embeds to Discord
    payload = {
        "embeds": [summary_embed, details_embed]
    }
    return send_discord_webhook(payload)
