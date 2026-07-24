import http.server
import socketserver
import json
import logging
import os
import sys
import sqlite3
import threading
import time
from datetime import datetime

# Add current folder to path to ensure local imports work
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import config
import database
from alpaca_client import AlpacaClient

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

PORT = 8080

# Thread-safe global cache and locking mechanism
LATEST_STATUS_CACHE = {}
CACHE_LOCK = threading.Lock()

def get_portfolio_history():
    """Retrieves portfolio history from the SQLite database."""
    try:
        with database.get_db_connection() as conn:
            cursor = conn.cursor()
            # Fetch the most recent 500 records (to keep chart performance snappy)
            cursor.execute("SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp DESC LIMIT 500")
            rows = cursor.fetchall()
            history = [dict(row) for row in rows]
            # Reverse so that chronological order is preserved for the Chart.js timeline (oldest to newest)
            history.reverse()
            
            # Prevent co-mingling: if we have any active paper trading metrics (not exactly 100000.0),
            # filter out the baseline fallback 100k data points.
            has_real_data = any(h['equity'] != 100000.0 for h in history)
            if has_real_data:
                history = [h for h in history if h['equity'] != 100000.0]
                
            return history
    except Exception as e:
        print(f"[Dashboard Server] Error fetching portfolio history: {e}", file=sys.stderr)
        return []

def get_ticker_history():
    """Extracts historical ticker-specific performance from decisions log."""
    import collections
    ticker_hist = collections.defaultdict(list)
    try:
        with database.get_db_connection() as conn:
            cursor = conn.cursor()
            # Fetch the most recent 500 records to reconstruct positions curves
            cursor.execute("SELECT timestamp, portfolio_state FROM decisions ORDER BY timestamp DESC LIMIT 500")
            rows = cursor.fetchall()
            # Reverse so that chronological order is preserved (oldest to newest)
            rows = list(rows)
            rows.reverse()
            
            for row in rows:
                ts = row['timestamp']
                p_state_str = row['portfolio_state']
                if p_state_str:
                    try:
                        state_data = json.loads(p_state_str)
                        positions = state_data.get('positions', {})
                        for symbol, pos in positions.items():
                            ticker_hist[symbol].append({
                                "timestamp": ts,
                                "equity": float(pos.get('market_value', 0.0)),
                                "unrealized_pnl": float(pos.get('unrealized_pnl', 0.0)),
                                "cash": 0.0
                            })
                    except Exception:
                        pass
    except Exception as e:
        print(f"[Dashboard Server] Error extracting ticker history: {e}", file=sys.stderr)
    return ticker_hist

def status_cache_worker():
    """Background thread that periodically fetches Alpaca status and SQLite data to update the global cache."""
    global LATEST_STATUS_CACHE
    print("[Dashboard Server] Background status cache worker started.", flush=True)
    while True:
        try:
            # Initialize default / fallback structures for Alpaca
            account = {}
            positions = {}
            is_mock = True

            # 1. Fetch Alpaca Account State and Positions with extreme resilience
            try:
                client = AlpacaClient()
                is_mock = client.is_mock
                
                # Fetch positions independently
                try:
                    positions = client.get_positions()
                except Exception as pos_err:
                    print(f"[Dashboard Server] Failed to fetch positions: {pos_err}", file=sys.stderr)
                    positions = {}

                # Fetch account state independently
                try:
                    account = client.get_account_state()
                except Exception as acc_err:
                    print(f"[Dashboard Server] Failed to fetch account state: {acc_err}", file=sys.stderr)
                    account = {}
                    # Force fallback of account metrics to DB history if account state fails
                    is_mock = True
            except Exception as alpaca_err:
                print(f"[Dashboard Server] Gracefully handled Alpaca Client initialization/query failure: {alpaca_err}", file=sys.stderr)
                is_mock = True  # Fallback to mock mode

            # 2. Retrieve DB Decisions, Trades, and History (Completely local, shouldn't fail unless DB is locked/corrupt)
            decisions = []
            trades = []
            history = []
            ticker_history = {}
            try:
                decisions = database.get_recent_decisions(limit=15)
                trades = database.get_recent_trades(limit=15)
                history = get_portfolio_history()
                ticker_history = get_ticker_history()
            except Exception as db_err:
                print(f"[Dashboard Server] Database retrieval failed: {db_err}", file=sys.stderr)

            # 3. Retrieve Latest Log Lines
            log_lines = []
            try:
                if os.path.exists(config.LOG_FILE):
                    with open(config.LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                        log_lines = f.readlines()[-60:]  # last 60 lines
            except Exception as log_err:
                print(f"[Dashboard Server] Log file retrieval failed: {log_err}", file=sys.stderr)

            # 3b. Retrieve DoD Balances from CSV
            dod_balances = []
            if os.path.exists("portfolio_dod_balances.csv"):
                try:
                    import csv
                    with open("portfolio_dod_balances.csv", "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for r in reader:
                            dod_balances.append({
                                "date": r["date"],
                                "equity": float(r["equity"]),
                                "cash": float(r["cash"]),
                                "holdings": float(r["holdings"]),
                                "dod_pnl_usd": float(r["dod_pnl_usd"]),
                                "dod_pnl_pct": float(r["dod_pnl_pct"])
                            })
                except Exception as csv_err:
                    print(f"[Dashboard Server] Error reading portfolio_dod_balances.csv: {csv_err}", file=sys.stderr)

            # If we are in mock mode or Alpaca failed, override the account state placeholders with the latest entry in database history
            if is_mock and history:
                latest_entry = history[-1]
                account["equity"] = latest_entry.get("equity", 100000.0)
                account["cash"] = latest_entry.get("cash", 100000.0)
                account["unrealized_pnl"] = latest_entry.get("unrealized_pnl", 0.0)

            # Assemble payload
            payload = {
                "account": account,
                "positions": positions,
                "decisions": decisions,
                "trades": trades,
                "history": history,
                "ticker_history": ticker_history,
                "dod_balances": dod_balances,
                "logs": log_lines,
                "trading_universe": config.TRADING_UNIVERSE,
                "interval": config.TRADING_INTERVAL_MINUTES,
                "is_mock": is_mock,
                "is_paper": config.ALPACA_PAPER,
                "error": None
            }

            with CACHE_LOCK:
                LATEST_STATUS_CACHE = payload

        except Exception as e:
            print(f"[Dashboard Server Error] Background cache update failed: {e}", file=sys.stderr, flush=True)
            with CACHE_LOCK:
                if not LATEST_STATUS_CACHE:
                    LATEST_STATUS_CACHE = {
                        "account": {},
                        "positions": {},
                        "decisions": [],
                        "trades": [],
                        "history": [],
                        "ticker_history": {},
                        "logs": [f"[SERVER ERROR] Background cache failed to initialize: {e}"],
                        "trading_universe": config.TRADING_UNIVERSE,
                        "interval": config.TRADING_INTERVAL_MINUTES,
                        "is_mock": True,
                        "is_paper": config.ALPACA_PAPER,
                        "error": str(e)
                    }
                else:
                    LATEST_STATUS_CACHE["error"] = str(e)

        time.sleep(10)


HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AGE Desk - Autonomous AI Trading Control Center</title>
    <!-- Premium Early Error Console -->
    <script>
        window.addEventListener('error', function(event) {
            const errDiv = document.createElement('div');
            errDiv.style.cssText = 'position:fixed; top:0; left:0; width:100%; background:#ff0844; color:#fff; padding:15px; z-index:999999; font-family:monospace; font-size:13px; line-height:1.5; box-shadow:0 4px 30px rgba(0,0,0,0.5); display:flex; justify-content:space-between; align-items:center;';
            errDiv.innerHTML = `
                <div>
                    <strong>[EARLY LIFECYCLE RUNTIME ERROR]</strong> ${event.message || 'An unhandled script error occurred.'}<br>
                    <span style="opacity:0.8; font-size:11px;">Source: ${event.filename || 'unknown'} | Line: ${event.lineno || 0}</span>
                </div>
                <button onclick="this.parentElement.remove()" style="background:rgba(255,255,255,0.25); border:none; color:#fff; padding:6px 12px; border-radius:4px; cursor:pointer; font-size:11px;">Dismiss</button>
            `;
            document.documentElement.appendChild(errDiv);
        });
    </script>
    <!-- Premium Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <!-- Lucide Icons -->
    <script src="https://unpkg.com/lucide@latest"></script>
    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-base: #0a0d16;
            --bg-surface: #101424;
            --bg-surface-elevated: #161c33;
            --border-subtle: rgba(255, 255, 255, 0.07);
            --border-glow: rgba(0, 242, 254, 0.15);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --text-muted: #6b7280;
            --color-teal: #00f2fe;
            --color-blue: #0070f3;
            --color-crimson: #ff0844;
            --color-gold: #f6d365;
            --color-green: #10b981;
            --glass-gradient: linear-gradient(135deg, rgba(16, 20, 36, 0.7) 0%, rgba(10, 13, 22, 0.9) 100%);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            overflow-x: hidden;
            background-image: 
                radial-gradient(at 0% 0%, rgba(0, 112, 243, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(0, 242, 254, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            min-height: 100vh;
        }

        /* Custom Scrollbars */
        ::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.2);
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.15);
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.5rem 2rem;
            border-bottom: 1px solid var(--border-subtle);
            background: rgba(16, 20, 36, 0.5);
            backdrop-filter: blur(12px);
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .brand-container {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .brand-logo {
            width: 2.25rem;
            height: 2.25rem;
            background: linear-gradient(135deg, var(--color-teal), var(--color-blue));
            border-radius: 0.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 0 15px rgba(0, 242, 254, 0.4);
            animation: pulse-glow 3s infinite alternate;
        }

        .brand-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.5rem;
            font-weight: 800;
            background: linear-gradient(to right, #ffffff, #9ca3af);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .system-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.825rem;
            font-weight: 600;
            background: rgba(16, 20, 36, 0.8);
            padding: 0.4rem 0.8rem;
            border-radius: 2rem;
            border: 1px solid var(--border-subtle);
        }

        .status-dot {
            width: 0.5rem;
            height: 0.5rem;
            border-radius: 50%;
            background-color: var(--color-green);
            box-shadow: 0 0 8px var(--color-green);
            animation: blink 1.5s infinite;
        }

        main {
            padding: 2rem;
            max-width: 1600px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 2rem;
        }

        @media (max-width: 1100px) {
            main {
                grid-template-columns: 1fr;
            }
        }

        /* Metric Cards Grid */
        .metrics-bar {
            grid-column: 1 / -1;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1.5rem;
        }

        @media (max-width: 768px) {
            .metrics-bar {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .metric-card {
            background: var(--glass-gradient);
            border: 1px solid var(--border-subtle);
            border-radius: 1rem;
            padding: 1.25rem 1.5rem;
            position: relative;
            overflow: hidden;
            backdrop-filter: blur(8px);
            transition: transform 0.2s ease, border-color 0.2s ease;
        }

        .metric-card:hover {
            transform: translateY(-2px);
            border-color: var(--color-blue);
        }

        .metric-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 2px;
            background: linear-gradient(to right, transparent, rgba(255, 255, 255, 0.05), transparent);
        }

        .metric-label {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }

        .metric-value {
            font-family: 'Outfit', sans-serif;
            font-size: 1.85rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .metric-sub {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 0.35rem;
        }

        .text-green { color: var(--color-green) !important; }
        .text-crimson { color: var(--color-crimson) !important; }
        .text-gold { color: var(--color-gold) !important; }

        /* General Card Section */
        .card-panel {
            background: var(--glass-gradient);
            border: 1px solid var(--border-subtle);
            border-radius: 1.25rem;
            padding: 1.5rem;
            backdrop-filter: blur(12px);
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            position: relative;
        }

        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 0.75rem;
        }

        .panel-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.15rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        /* AI Thought Stream */
        .thought-stream {
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            max-height: 700px;
            overflow-y: auto;
            padding-right: 0.25rem;
        }

        .thought-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 0.75rem;
            padding: 1.25rem;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
            position: relative;
        }

        .thought-card:hover {
            border-color: rgba(0, 242, 254, 0.25);
            box-shadow: 0 4px 20px rgba(0, 242, 254, 0.05);
        }

        .thought-card.approved {
            border-left: 3px solid var(--color-green);
        }

        .thought-card.rejected {
            border-left: 3px solid var(--color-crimson);
            opacity: 0.85;
        }

        .thought-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.75rem;
        }

        .thought-meta {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
        }

        .thought-ticker {
            font-family: 'Outfit', sans-serif;
            font-size: 1.1rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .badge {
            font-size: 0.65rem;
            font-weight: 700;
            padding: 0.15rem 0.4rem;
            border-radius: 0.25rem;
            text-transform: uppercase;
        }

        .badge-buy { background: rgba(16, 185, 129, 0.15); color: var(--color-green); border: 1px solid rgba(16, 185, 129, 0.3); }
        .badge-sell { background: rgba(239, 68, 68, 0.15); color: var(--color-crimson); border: 1px solid rgba(239, 68, 68, 0.3); }
        .badge-hold { background: rgba(245, 158, 11, 0.15); color: var(--color-gold); border: 1px solid rgba(245, 158, 11, 0.3); }

        .thought-time {
            font-size: 0.725rem;
            color: var(--text-muted);
        }

        .thought-text {
            font-size: 0.85rem;
            line-height: 1.5;
            color: var(--text-secondary);
            background: rgba(10, 13, 22, 0.3);
            padding: 0.75rem;
            border-radius: 0.5rem;
            border: 1px solid rgba(255, 255, 255, 0.02);
            white-space: pre-line;
        }

        .confluence-box {
            display: flex;
            gap: 1rem;
            margin-top: 0.75rem;
            font-size: 0.75rem;
        }

        .confluence-item {
            background: var(--bg-surface-elevated);
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
        }

        /* Order Audit Trail Table */
        .trades-table-container {
            overflow-x: auto;
        }

        .trades-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
            text-align: left;
        }

        .trades-table th {
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border-subtle);
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.725rem;
            letter-spacing: 0.05em;
        }

        .trades-table td {
            padding: 0.85rem 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.02);
        }

        .trades-table tr:hover {
            background: rgba(255, 255, 255, 0.01);
        }

        /* Live Terminal Logs */
        .terminal-pane {
            background: #05070f;
            border: 1px solid #1a1e36;
            border-radius: 0.75rem;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            line-height: 1.42;
            color: #d1d5db;
            height: 350px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
            display: flex;
            flex-direction: column-reverse; /* Shows latest log first */
        }

        .terminal-line {
            margin-bottom: 0.25rem;
            border-left: 2px solid transparent;
            padding-left: 0.4rem;
        }

        .terminal-info { border-color: var(--color-blue); }
        .terminal-warn { border-color: var(--color-gold); color: #fef08a; }
        .terminal-error { border-color: var(--color-crimson); color: #fca5a5; }

        /* Holdings Cards */
        .positions-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .position-row {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 0.75rem;
            padding: 0.85rem 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .position-symbol-side {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
        }

        .pos-sym {
            font-family: 'Outfit', sans-serif;
            font-weight: 700;
            font-size: 1rem;
        }

        .pos-qty {
            font-size: 0.75rem;
            color: var(--text-secondary);
        }

        .position-value-pnl {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 0.15rem;
        }

        .pos-val {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 500;
            font-size: 0.95rem;
        }

        .pos-pnl {
            font-size: 0.725rem;
            font-weight: 600;
        }

        /* Chart Section */
        .chart-box {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 0.75rem;
            padding: 1rem;
            height: 250px;
            width: 100%;
        }

        /* Animation utilities */
        @keyframes blink {
            0% { opacity: 0.4; }
            50% { opacity: 1; }
            100% { opacity: 0.4; }
        }

        @keyframes pulse-glow {
            0% { box-shadow: 0 0 10px rgba(0, 242, 254, 0.3); }
            100% { box-shadow: 0 0 20px rgba(0, 242, 254, 0.6); }
        }

        /* Maximized Strategy Q&A Focus Panel */
        #qa-analyst-panel.maximized {
            position: fixed;
            top: 4vh;
            left: 6vw;
            width: 88vw;
            height: 92vh;
            z-index: 99999;
            background: rgba(10, 13, 22, 0.96);
            backdrop-filter: blur(25px);
            border: 1.5px solid var(--color-teal);
            box-shadow: 0 10px 50px rgba(0, 242, 254, 0.25);
            border-radius: 1.25rem;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            animation: scaleInQAMax 0.25s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
        #qa-analyst-panel.maximized #chat-log {
            flex: 1 !important;
            height: auto !important; /* overrides fixed 250px height */
            background: rgba(5, 7, 15, 0.8) !important;
        }
        #equity-curve-panel.maximized {
            position: fixed;
            top: 4vh;
            left: 6vw;
            width: 88vw;
            height: 92vh;
            z-index: 99999;
            background: rgba(10, 13, 22, 0.96);
            backdrop-filter: blur(25px);
            border: 1.5px solid var(--color-blue);
            box-shadow: 0 10px 50px rgba(0, 112, 243, 0.25);
            border-radius: 1.25rem;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            animation: scaleInQAMax 0.25s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
        #chart-workarea-wrapper {
            display: block;
            width: 100%;
        }
        #equity-curve-panel.maximized #chart-workarea-wrapper {
            display: flex;
            gap: 1.75rem;
            flex: 1;
            min-height: 0;
        }
        #chart-left-pane {
            width: 100%;
            display: flex;
            flex-direction: column;
        }
        #equity-curve-panel.maximized #chart-left-pane {
            width: 70%;
            height: 100%;
        }
        #chart-right-pane {
            display: none;
        }
        #equity-curve-panel.maximized #chart-right-pane {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            width: 30%;
            height: 100%;
            overflow-y: auto;
            background: rgba(5, 7, 15, 0.4);
            padding: 1.25rem;
            border-radius: 0.75rem;
            border: 1px solid var(--border-subtle);
        }
        #chart-metric-selector {
            display: none;
        }
        #equity-curve-panel.maximized #chart-metric-selector {
            display: flex;
        }
        #equity-curve-panel.maximized .chart-box {
            flex: 1 !important;
            height: auto !important;
        }

        /* Maximized Executed Orders Panel */
        #executed-orders-panel.maximized {
            position: fixed;
            top: 4vh;
            left: 6vw;
            width: 88vw;
            height: 92vh;
            z-index: 99999;
            background: rgba(10, 13, 22, 0.96);
            backdrop-filter: blur(25px);
            border: 1.5px solid var(--color-green);
            box-shadow: 0 10px 50px rgba(16, 185, 129, 0.25);
            border-radius: 1.25rem;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            animation: scaleInQAMax 0.25s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
        #executed-orders-panel.maximized .trades-table-container {
            flex: 1 !important;
            overflow-y: auto !important;
            min-height: 0;
        }

        @keyframes scaleInQAMax {
            from { transform: scale(0.97); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }
        #orders-ticker-select option {
            background: #101424 !important;
            color: var(--text-primary) !important;
        }
    </style>
</head>
<body>

    <header>
        <div class="brand-container">
            <div class="brand-logo">
                <i data-lucide="trending-up" style="color: var(--bg-base); width: 1.25rem; height: 1.25rem;"></i>
            </div>
            <h1 class="brand-title">AGE DESK</h1>
        </div>
        <div class="system-status">
            <div class="status-dot" id="status-dot"></div>
            <span id="system-status-text">SIMULATION LIVE</span>
        </div>
    </header>

    <main>
        <!-- Top metrics bar -->
        <div class="metrics-bar">
            <div class="metric-card">
                <div class="metric-label">
                    <i data-lucide="wallet" style="width: 0.85rem; height: 0.85rem; color: var(--color-teal)"></i>
                    Total Equity
                </div>
                <div class="metric-value" id="val-equity">$100,000.00</div>
                <div class="metric-sub" id="sub-equity">Live Account Net Asset Value</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">
                    <i data-lucide="dollar-sign" style="width: 0.85rem; height: 0.85rem; color: var(--color-blue)"></i>
                    Cash Balance
                </div>
                <div class="metric-value" id="val-cash">$100,000.00</div>
                <div class="metric-sub" id="sub-cash">Ready Buying Power Allocation</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">
                    <i data-lucide="percent" style="width: 0.85rem; height: 0.85rem; color: var(--color-green)"></i>
                    Unrealized PnL
                </div>
                <div class="metric-value" id="val-pnl">$0.00</div>
                <div class="metric-sub" id="sub-pnl">Open Intraday Profit / Loss</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">
                    <i data-lucide="refresh-cw" style="width: 0.85rem; height: 0.85rem; color: var(--color-gold)"></i>
                    Refresh Interval
                </div>
                <div class="metric-value" id="val-interval">15 mins</div>
                <div class="metric-sub">Loop Cycle Run Rate</div>
            </div>
        </div>

        <!-- Left column: Thoughts and Orders -->
        <div style="display: flex; flex-direction: column; gap: 2rem;">
            <!-- AI Thought Stream -->
            <div class="card-panel">
                <div class="panel-header">
                    <h2 class="panel-title">
                        <i data-lucide="brain-circuit" style="color: var(--color-teal); width: 1.15rem; height: 1.15rem;"></i>
                        AI Strategy Decision Stream
                    </h2>
                </div>
                <div class="thought-stream" id="thought-stream">
                    <!-- Dynamic insert -->
                    <div style="text-align: center; color: var(--text-muted); padding: 2rem;">Querying AI strategy database...</div>
                </div>
            </div>

            <!-- Equity Growth Chart -->
            <div class="card-panel" id="equity-curve-panel">
                <div class="panel-header">
                    <h2 class="panel-title">
                        <i data-lucide="activity" style="color: var(--color-blue); width: 1.15rem; height: 1.15rem;"></i>
                        Equity Valuation Curve
                    </h2>
                    <div id="chart-timeframe-selector" style="display: flex; gap: 0.35rem; margin-left: 1.5rem;">
                        <button class="timeframe-btn" onclick="changeChartTimeframe('1D')" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.7rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">1D</button>
                        <button class="timeframe-btn" onclick="changeChartTimeframe('5D')" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.7rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">5D</button>
                        <button class="timeframe-btn" onclick="changeChartTimeframe('1M')" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.7rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">1M</button>
                        <button class="timeframe-btn active" onclick="changeChartTimeframe('ALL')" style="background: rgba(0, 242, 254, 0.1); border: 1px solid var(--color-teal); color: var(--color-teal); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.7rem; cursor: pointer; font-weight: 600; transition: all 0.2s; box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);">ALL</button>
                    </div>
                    <div id="chart-ticker-selector-container" style="display: flex; align-items: center; gap: 0.35rem; margin-left: 1.5rem;">
                        <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Ticker:</span>
                        <select id="chart-ticker-select" onchange="filterChartByTicker()" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-primary); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.7rem; font-weight: 600; outline: none; cursor: pointer; transition: border-color 0.2s;">
                            <option value="ALL">ALL PORTFOLIO</option>
                        </select>
                    </div>
                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-left: auto;">
                        <button id="chart-reset-btn" onclick="resetTelemetryHistory()" title="Reset Telemetry History" style="background: rgba(255, 8, 68, 0.05); border: 1px solid rgba(255, 8, 68, 0.2); color: var(--color-crimson); cursor: pointer; display: none; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="trash-2" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                        <button id="chart-maximize-btn" onclick="toggleChartMaximize()" title="Maximize Chart" style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="maximize-2" id="chart-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                    </div>
                </div>
                <div id="chart-workarea-wrapper">
                    <div id="chart-left-pane">
                        <div id="chart-metric-selector" style="display: none; gap: 0.5rem; margin-bottom: 0.75rem;">
                            <button class="selector-btn active" onclick="changeChartMetric('equity')" style="background: rgba(0, 242, 254, 0.1); border: 1px solid var(--color-teal); color: var(--color-teal); padding: 0.35rem 0.75rem; border-radius: 0.25rem; font-size: 0.75rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">📈 Equity ($)</button>
                            <button class="selector-btn" onclick="changeChartMetric('pnl')" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); padding: 0.35rem 0.75rem; border-radius: 0.25rem; font-size: 0.75rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">📊 Unrealized PnL ($)</button>
                            <button class="selector-btn" onclick="changeChartMetric('cash')" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); padding: 0.35rem 0.75rem; border-radius: 0.25rem; font-size: 0.75rem; cursor: pointer; font-weight: 600; transition: all 0.2s;">💵 Cash Reserves ($)</button>
                        </div>
                        <div class="chart-box">
                            <canvas id="equity-chart"></canvas>
                        </div>
                    </div>
                    <div id="chart-right-pane">
                        <div style="font-family: 'Outfit', sans-serif; font-size: 1rem; font-weight: 700; border-bottom: 1px solid var(--border-subtle); padding-bottom: 0.5rem; margin-bottom: 0.5rem; color: var(--text-primary);">
                            Performance Diagnostics
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr; gap: 0.75rem; flex: 1;">
                            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; justify-content: center;">
                                <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Total Net Return</span>
                                <span id="kpi-return" style="font-family: 'Outfit', sans-serif; font-size: 1.5rem; font-weight: 700; color: var(--color-teal); margin-top: 0.15rem;">0.00%</span>
                            </div>
                            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; justify-content: center;">
                                <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Peak Drawdown</span>
                                <span id="kpi-drawdown" style="font-family: 'Outfit', sans-serif; font-size: 1.5rem; font-weight: 700; color: var(--color-crimson); margin-top: 0.15rem;">0.00%</span>
                            </div>
                            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; justify-content: center;">
                                <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Cycle Win Rate</span>
                                <span id="kpi-winrate" style="font-family: 'Outfit', sans-serif; font-size: 1.5rem; font-weight: 700; color: var(--color-green); margin-top: 0.15rem;">0.0%</span>
                            </div>
                            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; justify-content: center;">
                                <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Sharpe Ratio (Est.)</span>
                                <span id="kpi-sharpe" style="font-family: 'Outfit', sans-serif; font-size: 1.5rem; font-weight: 700; color: var(--color-gold); margin-top: 0.15rem;">0.00</span>
                            </div>
                            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; justify-content: center;">
                                <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Profit Factor</span>
                                <span id="kpi-factor" style="font-family: 'Outfit', sans-serif; font-size: 1.5rem; font-weight: 700; color: #a855f7; margin-top: 0.15rem;">1.00</span>
                            </div>
                        </div>
                        
                        <!-- Day-over-Day Ledger section -->
                        <div style="font-family: 'Outfit', sans-serif; font-size: 1rem; font-weight: 700; border-bottom: 1px solid var(--border-subtle); padding-bottom: 0.5rem; margin-top: 1rem; margin-bottom: 0.5rem; color: var(--text-primary);">
                            Day-over-Day Ledger
                        </div>
                        <div style="overflow-y: auto; max-height: 250px; background: rgba(5, 7, 15, 0.4); border: 1px solid var(--border-subtle); border-radius: 0.5rem; padding: 0.5rem;">
                            <table style="width: 100%; font-size: 0.75rem; border-collapse: collapse;">
                                <thead>
                                    <tr style="border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); text-align: left;">
                                        <th style="padding: 0.4rem;">Date</th>
                                        <th style="padding: 0.4rem; text-align: right;">Equity</th>
                                        <th style="padding: 0.4rem; text-align: right;">Cash</th>
                                        <th style="padding: 0.4rem; text-align: right;">DoD PnL</th>
                                    </tr>
                                </thead>
                                <tbody id="dod-ledger-tbody">
                                    <tr style="border-bottom: 1px solid rgba(255,255,255,0.02);">
                                        <td colspan="4" style="padding: 0.8rem; text-align: center; color: var(--text-muted);">No ledger data loaded.</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Executed Trades Audit -->
            <div class="card-panel" id="executed-orders-panel">
                <div class="panel-header">
                    <h2 class="panel-title">
                        <i data-lucide="receipt" style="color: var(--color-green); width: 1.15rem; height: 1.15rem;"></i>
                        Broker-Side Executed Orders
                    </h2>
                    
                    <!-- Interactive Ticker Select (Visible inline) -->
                    <div id="orders-filter-container" style="display: flex; align-items: center; gap: 0.5rem; margin-left: 1.5rem;">
                        <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em;">Ticker:</span>
                        <select id="orders-ticker-select" onchange="filterExecutedOrders()" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); color: var(--text-primary); padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-size: 0.75rem; font-weight: 600; outline: none; cursor: pointer; transition: border-color 0.2s;">
                            <option value="ALL">ALL TICKERS</option>
                        </select>
                    </div>

                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-left: auto;">
                        <button id="orders-export-btn" onclick="exportOrdersToCSV()" title="Export CSV Ledger" style="background: rgba(16, 185, 129, 0.05); border: 1px solid rgba(16, 185, 129, 0.2); color: var(--color-green); cursor: pointer; display: none; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="download" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                        <button id="orders-maximize-btn" onclick="toggleOrdersMaximize()" title="Maximize Orders" style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="maximize-2" id="orders-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                    </div>
                </div>
                
                <!-- Dynamic Transactional Stats Sub-Header (Only visible when maximized) -->
                <div id="orders-stats-banner" style="display: none; grid-template-columns: repeat(4, 1fr); gap: 1rem; background: rgba(255,255,255,0.01); border: 1px solid var(--border-subtle); padding: 0.75rem 1rem; border-radius: 0.5rem; margin-bottom: 1rem;">
                    <div style="display: flex; flex-direction: column;">
                        <span style="font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600;">Matching Fills</span>
                        <span id="order-stat-count" style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 700; color: var(--text-primary);">0</span>
                    </div>
                    <div style="display: flex; flex-direction: column;">
                        <span style="font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600;">Total Buy Volume</span>
                        <span id="order-stat-buys" style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 700; color: var(--color-teal);">$0.00</span>
                    </div>
                    <div style="display: flex; flex-direction: column;">
                        <span style="font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600;">Total Sell Volume</span>
                        <span id="order-stat-sells" style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 700; color: #a855f7;">$0.00</span>
                    </div>
                    <div style="display: flex; flex-direction: column;">
                        <span style="font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600;">Net Flow</span>
                        <span id="order-stat-flow" style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 700; color: var(--color-green);">$0.00</span>
                    </div>
                </div>

                <div class="trades-table-container">
                    <table class="trades-table">
                        <thead>
                            <tr>
                                <th>Timestamp (UTC)</th>
                                <th>Symbol</th>
                                <th>Action</th>
                                <th>Qty</th>
                                <th>Avg Fill Price</th>
                                <th>Status</th>
                                <th>Alpaca Order ID</th>
                            </tr>
                        </thead>
                        <tbody id="trades-tbody">
                            <!-- Dynamic insert -->
                            <tr>
                                <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No orders registered in database.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Right column: Logs and Positions -->
        <div style="display: flex; flex-direction: column; gap: 2rem;">
            <!-- Current Positions -->
            <div class="card-panel">
                <div class="panel-header">
                    <h2 class="panel-title">
                        <i data-lucide="pie-chart" style="color: var(--color-gold); width: 1.15rem; height: 1.15rem;"></i>
                        Current Holdings
                    </h2>
                </div>
                <div class="positions-list" id="positions-list">
                    <div style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No open positions. Portfolio in full cash buffer.</div>
                </div>
            </div>

            <!-- Strategy Copilot Chat -->
            <div class="card-panel" id="qa-analyst-panel" style="gap: 0.75rem;">
                <div class="panel-header" style="padding-bottom: 0.5rem;">
                    <h2 class="panel-title">
                        <i data-lucide="sparkles" style="color: var(--color-teal); width: 1.15rem; height: 1.15rem;"></i>
                        Strategy Q&A Analyst
                    </h2>
                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-left: auto;">
                        <button id="qa-copy-btn" onclick="copyQAToClipboard()" title="Copy Conversation as Markdown" style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="copy" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                        <button id="qa-export-btn" onclick="exportQAToFile()" title="Export to Markdown File" style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="download" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                        <button id="qa-maximize-btn" onclick="toggleQAMaximize()" title="Maximize Workspace" style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; width: 1.85rem; height: 1.85rem; border-radius: 0.35rem; transition: background 0.2s, color 0.2s;">
                            <i data-lucide="maximize-2" id="qa-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>
                        </button>
                    </div>
                </div>
                <div style="font-size: 0.75rem; color: var(--text-secondary); line-height: 1.4;">
                    Ask the agent's cognitive assistant about its active rules, trade logic, mathematical indicators, or why specific orders were filled or rejected.
                </div>
                <!-- Chat Log area -->
                <div id="chat-log" style="height: 250px; overflow-y: auto; background: rgba(5, 7, 15, 0.6); border: 1px solid var(--border-subtle); border-radius: 0.5rem; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.75rem;">
                    <div style="display: flex; gap: 0.5rem; align-items: flex-start;">
                        <div style="width: 1.5rem; height: 1.5rem; border-radius: 50%; background: linear-gradient(135deg, var(--color-teal), var(--color-blue)); display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
                            <i data-lucide="bot" style="color: var(--bg-base); width: 0.85rem; height: 0.85rem;"></i>
                        </div>
                        <div style="background: rgba(255, 255, 255, 0.05); padding: 0.6rem 0.8rem; border-radius: 0 0.75rem 0.75rem 0.75rem; font-size: 0.8rem; line-height: 1.4; color: var(--text-primary); max-width: 85%;">
                            Hello! I am your portfolio analyst co-pilot. I have full context of my recent technical thoughts, current holdings, and broker orders. How can I help you learn today?
                        </div>
                    </div>
                </div>
                <!-- Chat Input area -->
                <div style="display: flex; gap: 0.5rem; margin-top: 0.25rem;">
                    <input type="text" id="chat-input" placeholder="Ask about trades, RSI, pivots, etc..." style="flex: 1; background: var(--bg-surface-elevated); border: 1px solid var(--border-subtle); border-radius: 0.5rem; color: var(--text-primary); font-size: 0.8rem; padding: 0.5rem 0.75rem; outline: none; transition: border-color 0.2s;" onkeydown="if(event.key == 'Enter') sendChatMessage()">
                    <button id="chat-send-btn" onclick="sendChatMessage()" style="background: linear-gradient(135deg, var(--color-teal), var(--color-blue)); border: none; border-radius: 0.5rem; width: 2.25rem; height: 2.25rem; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: transform 0.1s, opacity 0.2s;">
                        <i data-lucide="send" style="color: var(--bg-base); width: 0.95rem; height: 0.95rem;"></i>
                    </button>
                </div>
            </div>

            <!-- Live Tail Logs -->
            <div class="card-panel">
                <div class="panel-header">
                    <h2 class="panel-title">
                        <i data-lucide="terminal" style="color: var(--text-primary); width: 1.15rem; height: 1.15rem;"></i>
                        System Activity Logs (Live Tail)
                    </h2>
                </div>
                <div class="terminal-pane" id="terminal-pane">
                    <!-- Dynamic insert -->
                </div>
            </div>

            <!-- Universe Quick Links -->
            <div class="card-panel" style="font-size: 0.8rem; line-height: 1.5;">
                <div class="panel-header" style="border-bottom: none; padding-bottom: 0;">
                    <h3 class="panel-title" style="font-size: 0.95rem;">
                        <i data-lucide="info" style="color: var(--color-blue); width: 1rem; height: 1rem;"></i>
                        Platform Parameters
                    </h3>
                </div>
                <div style="background: rgba(10, 13, 22, 0.4); padding: 1rem; border-radius: 0.75rem; border: 1px solid var(--border-subtle);">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.4rem;">
                        <span style="color: var(--text-secondary);">Trading Universe:</span>
                        <span style="font-family: 'JetBrains Mono', monospace; font-weight: bold;" id="universe-text">SPY, QQQ, SOL/USD</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.4rem;">
                        <span style="color: var(--text-secondary);">Drawdown Bound:</span>
                        <span style="color: var(--color-crimson); font-weight: 600;">2.0% Daily Limit</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.4rem;">
                        <span style="color: var(--text-secondary);">Allocation Cap:</span>
                        <span>10.0% Max/Trade</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span style="color: var(--text-secondary);">DB Engine:</span>
                        <span style="font-family: 'JetBrains Mono', monospace;">SQLite 3</span>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <script>
        // Timezone Alignment (TMCL-404) Date utilities
        function parseUtcTimestamp(ts) {
            if (!ts) return null;
            let str = ts.trim();
            const hasOffset = /([+-]\d{2}:?\d{2})$/.test(str) || /[Zz]$/.test(str);
            if (!hasOffset) {
                if (str.includes(' ') && !str.includes('T')) {
                    str = str.replace(' ', 'T');
                }
                str += 'Z';
            }
            return new Date(str);
        }

        function formatToEastern(ts) {
            const d = parseUtcTimestamp(ts);
            if (!d || isNaN(d.getTime())) return 'N/A';
            return d.toLocaleString('en-US', {
                timeZone: 'America/New_York',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: true
            });
        }

        // Global state for chart visualization and KPI workspace (TMCL-405)
        let activeChartMetric = 'equity';
        let activeChartTimeframe = 'ALL';
        let activeChartTicker = 'ALL';
        let latestHistoryCached = [];
        let latestTickerHistoryCached = {};

        function filterChartByTicker() {
            const select = document.getElementById('chart-ticker-select');
            if (select) {
                activeChartTicker = select.value;
            }
            updateChart(latestHistoryCached);
        }

        function populateTickerDropdown(tickerHistory) {
            const select = document.getElementById('chart-ticker-select');
            if (!select) return;
            const currentSelection = select.value;
            select.innerHTML = '<option value="ALL">ALL PORTFOLIO</option>';
            const tickers = Object.keys(tickerHistory).sort();
            tickers.forEach(t => {
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = t;
                select.appendChild(opt);
            });
            if (tickers.includes(currentSelection)) {
                select.value = currentSelection;
            } else {
                select.value = 'ALL';
                activeChartTicker = 'ALL';
            }
        }

        // Global error banner to capture and display any frontend/runtime issues on-screen
        window.onerror = function(message, source, lineno, colno, error) {
            const errDiv = document.createElement('div');
            errDiv.style.position = 'fixed';
            errDiv.style.top = '0';
            errDiv.style.left = '0';
            errDiv.style.width = '100%';
            errDiv.style.background = '#ff0844';
            errDiv.style.color = '#ffffff';
            errDiv.style.padding = '12px 20px';
            errDiv.style.zIndex = '999999';
            errDiv.style.fontFamily = "'JetBrains Mono', monospace";
            errDiv.style.fontSize = '13px';
            errDiv.style.lineHeight = '1.4';
            errDiv.style.boxShadow = '0 4px 30px rgba(0,0,0,0.5)';
            errDiv.style.display = 'flex';
            errDiv.style.justifyContent = 'space-between';
            errDiv.style.alignItems = 'center';
            errDiv.innerHTML = `
                <div style="text-align: left;">
                    <strong>[DASHBOARD RUNTIME ERROR]</strong> ${message} <br>
                    <span style="opacity: 0.8; font-size: 11px;">Source: ${source} | Line: ${lineno} | Col: ${colno}</span>
                </div>
                <button onclick="this.parentElement.remove()" style="background: rgba(255,255,255,0.2); border: none; color: #fff; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; flex-shrink: 0; margin-left: 15px;">Dismiss</button>
            `;
            document.body.appendChild(errDiv);
            return false;
        };

        // Safe wrapper for Lucide icons
        function safeCreateIcons() {
            if (typeof lucide !== 'undefined') {
                try {
                    lucide.createIcons();
                } catch (e) {
                    console.error('Failed to render Lucide icons:', e);
                }
            } else {
                console.warn('Lucide icons library is not loaded.');
            }
        }

        // Init Lucide Icons on load
        safeCreateIcons();

        let chartInstance = null;

        function updateChart(history) {
            latestHistoryCached = history || [];
            if (typeof Chart === 'undefined') {
                console.warn('Chart.js is not loaded. Skipping chart render.');
                return;
            }
            const canvasEl = document.getElementById('equity-chart');
            if (!canvasEl) return;
            const ctx = canvasEl.getContext('2d');
            
            let chartHistory = latestHistoryCached || [];
            if (activeChartTicker !== 'ALL') {
                chartHistory = latestTickerHistoryCached[activeChartTicker] || [];
            }
            
            // Gracefully hide/show Cash metric for ticker position curves
            const cashMetricBtn = document.querySelector('[onclick*="cash"]');
            if (activeChartTicker !== 'ALL') {
                if (cashMetricBtn) cashMetricBtn.style.display = 'none';
                if (activeChartMetric === 'cash') {
                    activeChartMetric = 'equity';
                    const metricButtons = document.querySelectorAll('#chart-metric-selector .selector-btn');
                    metricButtons.forEach(btn => {
                        btn.classList.remove('active');
                        btn.style.background = 'rgba(255, 255, 255, 0.03)';
                        btn.style.borderColor = 'var(--border-subtle)';
                        btn.style.color = 'var(--text-secondary)';
                        if (btn.getAttribute('onclick').includes('equity')) {
                            btn.classList.add('active');
                            btn.style.background = 'rgba(0, 242, 254, 0.1)';
                            btn.style.borderColor = 'var(--color-teal)';
                            btn.style.color = 'var(--color-teal)';
                        }
                    });
                }
            } else {
                if (cashMetricBtn) cashMetricBtn.style.display = 'flex';
            }
            
            // Filter history based on activeChartTimeframe
            if (activeChartTimeframe !== 'ALL') {
                const now = new Date();
                let cutoffTime = 0;
                if (activeChartTimeframe === '1D') {
                    cutoffTime = now.getTime() - 24 * 60 * 60 * 1000;
                } else if (activeChartTimeframe === '5D') {
                    cutoffTime = now.getTime() - 5 * 24 * 60 * 60 * 1000;
                } else if (activeChartTimeframe === '1M') {
                    cutoffTime = now.getTime() - 30 * 24 * 60 * 60 * 1000;
                }
                chartHistory = chartHistory.filter(item => {
                    const itemDate = parseUtcTimestamp(item.timestamp);
                    return itemDate && itemDate.getTime() >= cutoffTime;
                });
            }

            if (chartHistory.length === 0) {
                // Generate dummy flat historical chart if empty for aesthetics
                chartHistory = [
                    {timestamp: new Date(Date.now() - 3600000 * 3).toISOString(), equity: 100000, unrealized_pnl: 0, cash: 100000},
                    {timestamp: new Date(Date.now() - 3600000 * 2).toISOString(), equity: 100000, unrealized_pnl: 0, cash: 100000},
                    {timestamp: new Date(Date.now() - 3600000 * 1).toISOString(), equity: 100000, unrealized_pnl: 0, cash: 100000},
                    {timestamp: new Date().toISOString(), equity: 100000, unrealized_pnl: 0, cash: 100000}
                ];
            }

            // Dynamically plot based on activeChartMetric
            let label = 'Portfolio Value ($)';
            let borderColor = '#00f2fe';
            let backgroundColor = 'rgba(0, 242, 254, 0.05)';
            let data = [];

            if (activeChartMetric === 'equity') {
                label = activeChartTicker === 'ALL' ? 'Portfolio Value ($)' : `${activeChartTicker} Position Value ($)`;
                borderColor = '#00f2fe';
                backgroundColor = 'rgba(0, 242, 254, 0.05)';
                data = chartHistory.map(item => item.equity !== undefined ? item.equity : 100000);
            } else if (activeChartMetric === 'pnl') {
                label = activeChartTicker === 'ALL' ? 'Unrealized PnL ($)' : `${activeChartTicker} Unrealized PnL ($)`;
                borderColor = '#10b981';
                backgroundColor = 'rgba(16, 185, 129, 0.05)';
                data = chartHistory.map(item => item.unrealized_pnl !== undefined ? item.unrealized_pnl : 0);
            } else if (activeChartMetric === 'cash') {
                label = 'Cash Reserves ($)';
                borderColor = '#0070f3';
                backgroundColor = 'rgba(0, 112, 243, 0.05)';
                data = chartHistory.map(item => item.cash !== undefined ? item.cash : 100000);
            }

            const labels = chartHistory.map(item => {
                const d = parseUtcTimestamp(item.timestamp);
                if (!d) return '';
                if (activeChartTimeframe === '1D') {
                    return d.toLocaleTimeString('en-US', {timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit'});
                } else {
                    const datePart = d.toLocaleDateString('en-US', {timeZone: 'America/New_York', month: '2-digit', day: '2-digit'});
                    const timePart = d.toLocaleTimeString('en-US', {timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false});
                    return `${datePart} ${timePart}`;
                }
            });

            // Mathematical performance diagnostics engine (TMCL-405)
            let totalReturn = 0;
            let maxDrawdown = 0;
            let winRate = 0;
            let sharpeRatio = 0;
            let profitFactor = 1.00;

            if (chartHistory.length > 1) {
                // 1. Total Net Return
                const initialEquity = chartHistory[0].equity || 100000;
                const finalEquity = chartHistory[chartHistory.length - 1].equity || 100000;
                if (initialEquity > 0) {
                    totalReturn = ((finalEquity - initialEquity) / initialEquity) * 100;
                }

                // 2. Peak Drawdown
                let peak = -Infinity;
                chartHistory.forEach(item => {
                    const eq = item.equity !== undefined ? item.equity : 100000;
                    if (eq > peak) {
                        peak = eq;
                    }
                    if (peak > 0) {
                        const dd = ((peak - eq) / peak) * 100;
                        if (dd > maxDrawdown) {
                            maxDrawdown = dd;
                        }
                    }
                });

                // 3. Cycle Win Rate
                let wins = 0;
                let steps = chartHistory.length - 1;
                for (let i = 1; i < chartHistory.length; i++) {
                    const prev = chartHistory[i-1].equity !== undefined ? chartHistory[i-1].equity : 100000;
                    const curr = chartHistory[i].equity !== undefined ? chartHistory[i].equity : 100000;
                    if (curr > prev) {
                        wins++;
                    }
                }
                if (steps > 0) {
                    winRate = (wins / steps) * 100;
                }

                // 4. Sharpe Ratio (Annualized)
                const stepReturns = [];
                for (let i = 1; i < chartHistory.length; i++) {
                    const prev = chartHistory[i-1].equity !== undefined ? chartHistory[i-1].equity : 100000;
                    const curr = chartHistory[i].equity !== undefined ? chartHistory[i].equity : 100000;
                    if (prev > 0) {
                        stepReturns.push((curr - prev) / prev);
                    } else {
                        stepReturns.push(0);
                    }
                }
                if (stepReturns.length > 0) {
                    const sum = stepReturns.reduce((a, b) => a + b, 0);
                    const mean = sum / stepReturns.length;
                    const variance = stepReturns.reduce((acc, val) => acc + Math.pow(val - mean, 2), 0) / stepReturns.length;
                    const std = Math.sqrt(variance);
                    if (std > 0) {
                        sharpeRatio = (mean / std) * Math.sqrt(6552);
                    }
                }

                // 5. Profit Factor
                let grossGains = 0;
                let grossLosses = 0;
                for (let i = 1; i < chartHistory.length; i++) {
                    const prev = chartHistory[i-1].equity !== undefined ? chartHistory[i-1].equity : 100000;
                    const curr = chartHistory[i].equity !== undefined ? chartHistory[i].equity : 100000;
                    const diff = curr - prev;
                    if (diff > 0) {
                        grossGains += diff;
                    } else if (diff < 0) {
                        grossLosses += Math.abs(diff);
                    }
                }
                if (grossLosses > 0) {
                    profitFactor = grossGains / grossLosses;
                } else if (grossGains > 0) {
                    profitFactor = 99.99;
                } else {
                    profitFactor = 1.00;
                }
            }

            // Inject performance values into DOM with nice color indicators
            const elReturn = document.getElementById('kpi-return');
            if (elReturn) {
                elReturn.innerText = (totalReturn >= 0 ? '+' : '') + totalReturn.toFixed(2) + '%';
                elReturn.style.color = totalReturn >= 0 ? 'var(--color-teal)' : 'var(--color-crimson)';
            }

            const elDrawdown = document.getElementById('kpi-drawdown');
            if (elDrawdown) {
                elDrawdown.innerText = maxDrawdown.toFixed(2) + '%';
                elDrawdown.style.color = maxDrawdown > 0 ? 'var(--color-crimson)' : 'var(--text-secondary)';
            }

            const elWinrate = document.getElementById('kpi-winrate');
            if (elWinrate) {
                elWinrate.innerText = winRate.toFixed(1) + '%';
                elWinrate.style.color = winRate >= 50 ? 'var(--color-green)' : (winRate > 0 ? 'var(--color-gold)' : 'var(--text-secondary)');
            }

            const elSharpe = document.getElementById('kpi-sharpe');
            if (elSharpe) {
                elSharpe.innerText = sharpeRatio.toFixed(2);
                elSharpe.style.color = sharpeRatio >= 1.5 ? 'var(--color-gold)' : (sharpeRatio > 0 ? 'var(--color-teal)' : 'var(--text-secondary)');
            }

            const elFactor = document.getElementById('kpi-factor');
            if (elFactor) {
                elFactor.innerText = profitFactor === 99.99 ? 'MAX' : profitFactor.toFixed(2);
                elFactor.style.color = profitFactor >= 1.5 ? '#a855f7' : (profitFactor >= 1.0 ? 'var(--color-teal)' : 'var(--color-crimson)');
            }

            if (chartInstance) {
                chartInstance.destroy();
            }

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: label,
                        data: data,
                        borderColor: borderColor,
                        backgroundColor: backgroundColor,
                        borderWidth: 2,
                        tension: 0.3,
                        fill: true,
                        pointRadius: 0, // Hides the circular point-dots on the trend line for a premium smooth aesthetic
                        pointHoverRadius: 6 // Shows dot clearly on hover
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false }
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { 
                                color: '#6b7280', 
                                font: { size: 10 },
                                maxTicksLimit: 8, // Force auto-skipping to prevent cluttered/overlapping text labels
                                autoSkip: true
                            }
                        },
                        y: {
                            grid: { color: 'rgba(255, 255, 255, 0.03)' },
                            ticks: { color: '#6b7280', font: { size: 10 } }
                        }
                    }
                }
            });
        }

        async function fetchStatus() {
            try {
                const response = await fetch('/api/status');
                if (!response.ok) {
                    throw new Error(`Server returned HTTP ${response.status}`);
                }
                const data = await response.json();

                if (data && data.error) {
                    throw new Error(data.error);
                }

                // Update system status dynamically
                const statusDot = document.getElementById('status-dot');
                const statusText = document.getElementById('system-status-text');
                if (statusText && statusDot) {
                    if (data.is_paper) {
                        if (data.is_mock) {
                            statusText.innerText = "SIMULATION LIVE (PAPER - MOCK)";
                            statusText.style.color = "var(--color-gold)";
                            statusDot.style.backgroundColor = "var(--color-gold)";
                            statusDot.style.boxShadow = "0 0 8px var(--color-gold)";
                        } else {
                            statusText.innerText = "SIMULATION LIVE (PAPER)";
                            statusText.style.color = "var(--text-primary)";
                            statusDot.style.backgroundColor = "var(--color-green)";
                            statusDot.style.boxShadow = "0 0 8px var(--color-green)";
                        }
                    } else if (data.is_mock) {
                        statusText.innerText = "MOCK MODE (OFFLINE)";
                        statusText.style.color = "var(--color-gold)";
                        statusDot.style.backgroundColor = "var(--color-gold)";
                        statusDot.style.boxShadow = "0 0 8px var(--color-gold)";
                    } else {
                        statusText.innerText = "PRODUCTION LIVE (REAL CAPITAL)";
                        statusText.style.color = "var(--color-crimson)";
                        statusDot.style.backgroundColor = "var(--color-crimson)";
                        statusDot.style.boxShadow = "0 0 8px var(--color-crimson)";
                    }
                }

                // 1. Update Metrics Cards
                const account = (data && data.account) || {};
                const equity = account.equity !== undefined ? account.equity : 100000.0;
                const cash = account.cash !== undefined ? account.cash : 100000.0;
                const pnl = account.unrealized_pnl !== undefined ? account.unrealized_pnl : 0.0;
                
                const valEquityEl = document.getElementById('val-equity');
                if (valEquityEl) {
                    valEquityEl.innerText = '$' + equity.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                }
                const valCashEl = document.getElementById('val-cash');
                if (valCashEl) {
                    valCashEl.innerText = '$' + cash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                }
                
                const pnlEl = document.getElementById('val-pnl');
                if (pnlEl) {
                    pnlEl.innerText = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                    pnlEl.className = 'metric-value ' + (pnl >= 0 ? 'text-green' : 'text-crimson');
                }

                const valIntervalEl = document.getElementById('val-interval');
                if (valIntervalEl) {
                    valIntervalEl.innerText = ((data && data.interval) || 15) + ' mins';
                }
                const universeTextEl = document.getElementById('universe-text');
                if (universeTextEl) {
                    universeTextEl.innerText = ((data && data.trading_universe) || []).join(', ');
                }

                // 2. Positions List
                const posListEl = document.getElementById('positions-list');
                if (posListEl) {
                    posListEl.innerHTML = '';
                    const positionsData = (data && data.positions) || {};
                    const positions = Object.keys(positionsData);
                    if (positions.length === 0) {
                        posListEl.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No open positions. Portfolio in full cash buffer.</div>';
                    } else {
                        positions.forEach(sym => {
                            const pos = positionsData[sym] || {};
                            const posPnl = pos.unrealized_pnl !== undefined ? pos.unrealized_pnl : 0.0;
                            const pnlClass = posPnl >= 0 ? 'text-green' : 'text-crimson';
                            const pnlSign = posPnl >= 0 ? '+' : '';
                            const avgEntry = pos.avg_entry_price !== undefined ? pos.avg_entry_price : 0.0;
                            const marketVal = pos.market_value !== undefined ? pos.market_value : 0.0;
                            const qty = pos.qty !== undefined ? pos.qty : 0;
                            
                            posListEl.innerHTML += `
                                <div class="position-row">
                                    <div class="position-symbol-side">
                                        <span class="pos-sym">${sym}</span>
                                        <span class="pos-qty">${qty} shares @ $${avgEntry.toFixed(2)}</span>
                                    </div>
                                    <div class="position-value-pnl">
                                        <span class="pos-val">$${marketVal.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                                        <span class="pos-pnl ${pnlClass}">${pnlSign}$${posPnl.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                                    </div>
                                </div>
                            `;
                        });
                    }
                }

                // 3. AI Decisions Stream
                const streamEl = document.getElementById('thought-stream');
                if (streamEl) {
                    streamEl.innerHTML = '';
                    const decisions = (data && data.decisions) || [];
                    if (decisions.length === 0) {
                        streamEl.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 2rem;">No historical trading thoughts logged yet.</div>';
                    } else {
                        decisions.forEach(dec => {
                            const isApproved = dec.is_approved === 1;
                            const statusClass = isApproved ? 'approved' : 'rejected';
                            const dateStr = dec.timestamp ? formatToEastern(dec.timestamp) : 'N/A';
                            
                            const action = dec.proposed_action || 'HOLD';
                            let actionBadgeClass = 'badge-hold';
                            if (action === 'BUY') actionBadgeClass = 'badge-buy';
                            if (action === 'SELL') actionBadgeClass = 'badge-sell';

                            let alertIcon = '';
                            if (!isApproved) {
                                alertIcon = `<span class="badge" style="background: rgba(239, 68, 68, 0.15); color: var(--color-crimson); margin-left: 0.5rem; text-transform: none;">REJECTED BY RISK GUARDRAIL: ${dec.rejection_reason || 'Unknown'}</span>`;
                            } else if (action !== 'HOLD') {
                                alertIcon = `<span class="badge" style="background: rgba(16, 185, 129, 0.15); color: var(--color-green); margin-left: 0.5rem; text-transform: none;">PASSED DE-RISK BOUNDARIES</span>`;
                            }

                            const symText = dec.proposed_symbol ? dec.proposed_symbol : 'PORTFOLIO HOLD';
                            const qtyText = dec.proposed_qty > 0 ? dec.proposed_qty + ' shares' : '';
                            const thoughtText = dec.thought_process || 'No rationale logged.';

                            streamEl.innerHTML += `
                                <div class="thought-card ${statusClass}">
                                    <div class="thought-header">
                                        <div class="thought-meta">
                                            <div class="thought-ticker">
                                                ${symText} 
                                                <span class="badge ${actionBadgeClass}">${action} ${qtyText}</span>
                                                ${alertIcon}
                                            </div>
                                            <span class="thought-time">${dateStr}</span>
                                        </div>
                                    </div>
                                    <div class="thought-text">${thoughtText}</div>
                                </div>
                            `;
                        });
                    }
                }

                // 4. Trades Database List
                const tradesTbody = document.getElementById('trades-tbody');
                if (tradesTbody) {
                    tradesTbody.innerHTML = '';
                    const trades = (data && data.trades) || [];
                    if (trades.length === 0) {
                        tradesTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No orders registered in database.</td></tr>';
                    } else {
                        trades.forEach(t => {
                            const side = t.side || 'buy';
                            const sideClass = side.toLowerCase() === 'buy' ? 'text-green' : 'text-crimson';
                            const dateStr = t.timestamp ? formatToEastern(t.timestamp) : 'N/A';
                            const fillPriceStr = t.filled_avg_price ? '$' + t.filled_avg_price.toFixed(2) : '<span style="color: var(--text-muted)">Unfilled</span>';
                            const symbol = t.symbol || 'N/A';
                            const qty = t.qty !== undefined ? t.qty : 0;
                            const status = t.status || 'unknown';
                            const orderId = t.alpaca_order_id || 'N/A';
                            
                            tradesTbody.innerHTML += `
                                <tr>
                                    <td style="font-family: 'JetBrains Mono', monospace; font-size: 0.8rem;">${dateStr}</td>
                                    <td style="font-weight: 600;">${symbol}</td>
                                    <td class="${sideClass}" style="text-transform: uppercase; font-weight: bold;">${side}</td>
                                    <td>${qty}</td>
                                    <td style="font-family: 'JetBrains Mono', monospace;">${fillPriceStr}</td>
                                    <td><span class="badge" style="background: rgba(255, 255, 255, 0.05); color: #e5e7eb;">${status}</span></td>
                                    <td style="font-family: 'JetBrains Mono', monospace; color: var(--text-muted); font-size: 0.8rem;">${orderId}</td>
                                </tr>
                            `;
                        });
                    }
                    
                    // Populate unique tickers in the select dropdown dynamically
                    const selectEl = document.getElementById('orders-ticker-select');
                    if (selectEl) {
                        const previousValue = selectEl.value;
                        const uniqueSymbols = new Set();
                        trades.forEach(t => {
                            if (t.symbol) {
                                uniqueSymbols.add(t.symbol.toUpperCase());
                            }
                        });
                        const sortedSymbols = Array.from(uniqueSymbols).sort();
                        let optionsHtml = '<option value="ALL">ALL TICKERS</option>';
                        sortedSymbols.forEach(symbol => {
                            optionsHtml += `<option value="${symbol}">${symbol}</option>`;
                        });
                        selectEl.innerHTML = optionsHtml;
                        if (uniqueSymbols.has(previousValue)) {
                            selectEl.value = previousValue;
                        } else {
                            selectEl.value = 'ALL';
                        }
                    }

                    if (typeof filterExecutedOrders === 'function') {
                        filterExecutedOrders();
                    }
                }

                // 5. System Logs live tail
                const terminalPane = document.getElementById('terminal-pane');
                if (terminalPane) {
                    terminalPane.innerHTML = '';
                    const logs = (data && data.logs) || [];
                    logs.forEach(line => {
                        let levelClass = 'terminal-info';
                        if (line.includes('[WARNING]') || line.includes('[WARN]')) levelClass = 'terminal-warn';
                        if (line.includes('[CRITICAL]') || line.includes('[ERROR]') || line.includes('FATAL')) levelClass = 'terminal-error';
                        
                        // Simple escape HTML
                        const escapedLine = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                        terminalPane.innerHTML += `<div class="terminal-line ${levelClass}">${escapedLine}</div>`;
                    });
                }

                // Update Chart and cache
                latestHistoryCached = (data && data.history) || [];
                latestTickerHistoryCached = (data && data.ticker_history) || {};
                
                if (typeof populateTickerDropdown === 'function') {
                    populateTickerDropdown(latestTickerHistoryCached);
                }
                
                updateChart(latestHistoryCached);
                
                // Update DoD Ledger table
                const dodLedgerTbody = document.getElementById('dod-ledger-tbody');
                if (dodLedgerTbody && data.dod_balances) {
                    let html = '';
                    data.dod_balances.forEach(row => {
                        const pnlColor = row.dod_pnl_usd >= 0 ? 'var(--color-green)' : 'var(--color-crimson)';
                        const pnlSign = row.dod_pnl_usd >= 0 ? '+' : '';
                        html += `
                            <tr style="border-bottom: 1px solid rgba(255,255,255,0.02);">
                                <td style="padding: 0.4rem; font-weight: 500;">${row.date}</td>
                                <td style="padding: 0.4rem; text-align: right; font-family: 'JetBrains Mono', monospace;">$${row.equity.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0})}</td>
                                <td style="padding: 0.4rem; text-align: right; font-family: 'JetBrains Mono', monospace; color: var(--text-secondary);">$${row.cash.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0})}</td>
                                <td style="padding: 0.4rem; text-align: right; font-family: 'JetBrains Mono', monospace; color: ${pnlColor}; font-weight: 600;">${pnlSign}${row.dod_pnl_pct.toFixed(1)}%</td>
                            </tr>
                        `;
                    });
                    dodLedgerTbody.innerHTML = html;
                }
                
                // Refresh Lucide icons dynamically to bind new HTML elements
                safeCreateIcons();

            } catch (err) {
                console.error('Failed to query status api:', err);
                // Graceful UI error reporting for the user
                const statusDot = document.getElementById('status-dot');
                const statusText = document.getElementById('system-status-text');
                if (statusText && statusDot) {
                    statusText.innerText = "OFFLINE / API ERROR (" + err.message + ")";
                    statusText.style.color = "var(--color-crimson)";
                    statusDot.style.backgroundColor = "var(--color-crimson)";
                    statusDot.style.boxShadow = "0 0 8px var(--color-crimson)";
                }
            }
        }

        // Fetch on startup and trigger polling every 10s
        fetchStatus();
        setInterval(fetchStatus, 10000);

        async function sendChatMessage() {
            const inputEl = document.getElementById('chat-input');
            const logEl = document.getElementById('chat-log');
            const sendBtn = document.getElementById('chat-send-btn');
            if (!inputEl || !logEl || !sendBtn) return;
            
            const message = inputEl.value.trim();
            if (!message) return;
            
            // Clear input and disable elements while loading
            inputEl.value = '';
            inputEl.disabled = true;
            sendBtn.style.opacity = '0.5';
            sendBtn.style.pointerEvents = 'none';
            
            // 1. Append User Message
            logEl.innerHTML += `
                <div style="display: flex; gap: 0.5rem; align-items: flex-start; justify-content: flex-end;">
                    <div style="background: rgba(0, 112, 243, 0.25); border: 1px solid rgba(0, 112, 243, 0.4); padding: 0.6rem 0.8rem; border-radius: 0.75rem 0 0.75rem 0.75rem; font-size: 0.8rem; line-height: 1.4; color: var(--text-primary); max-width: 85%;">
                        ${escapeHtml(message)}
                    </div>
                    <div style="width: 1.5rem; height: 1.5rem; border-radius: 50%; background: var(--color-blue); display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
                        <i data-lucide="user" style="color: var(--text-primary); width: 0.85rem; height: 0.85rem;"></i>
                    </div>
                </div>
            `;
            safeCreateIcons();
            logEl.scrollTop = logEl.scrollHeight;
            
            // 2. Append Loading state
            const loadingId = 'loading-' + Date.now();
            logEl.innerHTML += `
                <div id="${loadingId}" style="display: flex; gap: 0.5rem; align-items: flex-start;">
                    <div style="width: 1.5rem; height: 1.5rem; border-radius: 50%; background: linear-gradient(135deg, var(--color-teal), var(--color-blue)); display: flex; align-items: center; justify-content: center; flex-shrink: 0; animation: pulse-glow 1.5s infinite alternate;">
                        <i data-lucide="bot" style="color: var(--bg-base); width: 0.85rem; height: 0.85rem;"></i>
                    </div>
                    <div style="background: rgba(255, 255, 255, 0.03); padding: 0.6rem 0.8rem; border-radius: 0 0.75rem 0.75rem 0.75rem; font-size: 0.8rem; color: var(--text-muted); display: flex; align-items: center; gap: 0.25rem;">
                        Analyzing database context...
                        <span style="display:inline-block; width:4px; height:4px; border-radius:50%; background:var(--text-muted); animation: blink 1.4s infinite;"></span>
                        <span style="display:inline-block; width:4px; height:4px; border-radius:50%; background:var(--text-muted); animation: blink 1.4s infinite 0.2s;"></span>
                        <span style="display:inline-block; width:4px; height:4px; border-radius:50%; background:var(--text-muted); animation: blink 1.4s infinite 0.4s;"></span>
                    </div>
                </div>
            `;
            safeCreateIcons();
            logEl.scrollTop = logEl.scrollHeight;
            
            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: message })
                });
                if (!response.ok) {
                    throw new Error(`Chat API responded with ${response.status}`);
                }
                const data = await response.json();
                
                // Remove loading indicator
                const loadingEl = document.getElementById(loadingId);
                if (loadingEl) loadingEl.remove();
                
                if (data && data.error) {
                    throw new Error(data.error);
                }
                
                let botResponse = data.response || "No response received.";
                
                // Convert Markdown images: ![alt](url) to beautiful premium responsive img tags with hover-zoom and scroll-to-bottom
                botResponse = botResponse.replace(/!\[(.*?)\]\((.*?)\)/g, function(match, alt, url) {
                    let processedUrl = url;
                    if (processedUrl.includes('quickchart.io')) {
                        processedUrl = processedUrl.replace(/%/g, '%25').replace(/#/g, '%23').replace(/ /g, '%20');
                    }
                    return '<div class="chat-image-container" style="margin: 12px 0; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.2); box-shadow: 0 4px 20px rgba(0,0,0,0.3); transition: transform 0.3s ease; max-width: 100%;"><img src="' + processedUrl + '" alt="' + alt + '" style="width: 100%; height: auto; display: block; border-radius: 12px; transition: transform 0.3s ease;" onmouseover="this.style.transform=\\\'scale(1.02)\\\'" onmouseout="this.style.transform=\\\'scale(1)\\\'" onload="const l = document.getElementById(\\\'chat-log\\\'); if(l) l.scrollTop = l.scrollHeight;" /></div>';
                });

                // Convert Markdown links: [text](url) to standard styled anchor tags
                botResponse = botResponse.replace(/\[(.*?)\]\((.*?)\)/g, '<a href="$2" target="_blank" style="color: var(--color-teal); text-decoration: underline;">$1</a>');

                // Format markdown highlights safely
                botResponse = botResponse
                    .replace(/\\n/g, '<br>')
                    .replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>')
                    .replace(/\\*(.*?)\\*/g, '<em>$1</em>')
                    .replace(/`(.*?)`/g, '<code style="font-family: monospace; background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px;">$1</code>');
                
                logEl.innerHTML += `
                    <div style="display: flex; gap: 0.5rem; align-items: flex-start;">
                        <div style="width: 1.5rem; height: 1.5rem; border-radius: 50%; background: linear-gradient(135deg, var(--color-teal), var(--color-blue)); display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
                            <i data-lucide="bot" style="color: var(--bg-base); width: 0.85rem; height: 0.85rem;"></i>
                        </div>
                        <div style="background: rgba(255, 255, 255, 0.05); padding: 0.6rem 0.8rem; border-radius: 0 0.75rem 0.75rem 0.75rem; font-size: 0.8rem; line-height: 1.45; color: var(--text-primary); max-width: 85%;">
                            ${botResponse}
                        </div>
                    </div>
                `;
            } catch (err) {
                console.error("Error in copilot chat:", err);
                const loadingEl = document.getElementById(loadingId);
                if (loadingEl) loadingEl.remove();
                
                logEl.innerHTML += `
                    <div style="display: flex; gap: 0.5rem; align-items: flex-start;">
                        <div style="width: 1.5rem; height: 1.5rem; border-radius: 50%; background: var(--color-crimson); display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
                            <i data-lucide="alert-circle" style="color: var(--text-primary); width: 0.85rem; height: 0.85rem;"></i>
                        </div>
                        <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); padding: 0.6rem 0.8rem; border-radius: 0 0.75rem 0.75rem 0.75rem; font-size: 0.8rem; line-height: 1.4; color: #fca5a5; max-width: 85%;">
                            Error: ${err.message || 'An error occurred while connecting to the copilot endpoint.'}
                        </div>
                    </div>
                `;
            }
            
            // Restore input state
            inputEl.disabled = false;
            sendBtn.style.opacity = '1';
            sendBtn.style.pointerEvents = 'auto';
            inputEl.focus();
            
            safeCreateIcons();
            logEl.scrollTop = logEl.scrollHeight;
        }
        
        function escapeHtml(text) {
            if (!text) return '';
            return String(text)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        // --- Strategy Q&A Focus Panel & Export Helpers ---

        function toggleQAMaximize() {
            const panel = document.getElementById('qa-analyst-panel');
            const btn = document.getElementById('qa-maximize-btn');
            if (!panel || !btn) return;
            
            const isMaximized = panel.classList.toggle('maximized');
            if (isMaximized) {
                btn.innerHTML = `<i data-lucide="minimize-2" id="qa-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Minimize Workspace');
            } else {
                btn.innerHTML = `<i data-lucide="maximize-2" id="qa-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Maximize Workspace');
            }
            safeCreateIcons();
        }

        function toggleChartMaximize() {
            const panel = document.getElementById('equity-curve-panel');
            const btn = document.getElementById('chart-maximize-btn');
            const resetBtn = document.getElementById('chart-reset-btn');
            if (!panel || !btn) return;
            
            const isMaximized = panel.classList.toggle('maximized');
            if (isMaximized) {
                btn.innerHTML = `<i data-lucide="minimize-2" id="chart-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Minimize Chart');
                if (resetBtn) resetBtn.style.display = 'flex';
            } else {
                btn.innerHTML = `<i data-lucide="maximize-2" id="chart-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Maximize Chart');
                if (resetBtn) resetBtn.style.display = 'none';
            }
            safeCreateIcons();
            updateChart(latestHistoryCached);
        }

        function changeChartTimeframe(timeframe) {
            activeChartTimeframe = timeframe;
            const container = document.getElementById('chart-timeframe-selector');
            if (container) {
                const buttons = container.querySelectorAll('.timeframe-btn');
                buttons.forEach(btn => {
                    btn.classList.remove('active');
                    btn.style.background = 'rgba(255, 255, 255, 0.03)';
                    btn.style.border = '1px solid var(--border-subtle)';
                    btn.style.color = 'var(--text-secondary)';
                    btn.style.boxShadow = 'none';
                });
                
                buttons.forEach(btn => {
                    if (btn.getAttribute('onclick').includes(`'${timeframe}'`)) {
                        btn.classList.add('active');
                        btn.style.background = 'rgba(0, 242, 254, 0.1)';
                        btn.style.border = '1px solid var(--color-teal)';
                        btn.style.color = 'var(--color-teal)';
                        btn.style.boxShadow = '0 0 10px rgba(0, 242, 254, 0.2)';
                    }
                });
            }
            updateChart(latestHistoryCached);
        }

        async function resetTelemetryHistory() {
            const confirmed = confirm("Are you sure you want to completely purge and reset the telemetry performance history? This will delete all logged portfolio equity records from the database and cannot be undone.");
            if (!confirmed) return;
            
            try {
                const response = await fetch('/api/reset_history', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    }
                });
                
                const result = await response.json();
                if (response.ok && result.success) {
                    showToast("Telemetry history reset successfully.");
                    // Instantly refresh the cache locally & trigger re-draw
                    latestHistoryCached = [];
                    updateChart([]);
                    fetchStatus();
                } else {
                    alert("Failed to reset telemetry history: " + (result.error || "Unknown error"));
                }
            } catch (err) {
                console.error("Error resetting history:", err);
                alert("Error connecting to server to reset history.");
            }
        }

        function changeChartMetric(metric) {
            activeChartMetric = metric;
            const container = document.getElementById('chart-metric-selector');
            if (container) {
                const buttons = container.querySelectorAll('.selector-btn');
                buttons.forEach(btn => {
                    btn.classList.remove('active');
                    btn.style.background = 'rgba(255, 255, 255, 0.03)';
                    btn.style.border = '1px solid var(--border-subtle)';
                    btn.style.color = 'var(--text-secondary)';
                });
                
                buttons.forEach(btn => {
                    if (btn.getAttribute('onclick').includes(`'${metric}'`)) {
                        btn.classList.add('active');
                        btn.style.background = 'rgba(0, 242, 254, 0.1)';
                        btn.style.border = '1px solid var(--color-teal)';
                        btn.style.color = 'var(--color-teal)';
                        btn.style.boxShadow = '0 0 10px rgba(0, 242, 254, 0.2)';
                    } else {
                        btn.style.boxShadow = 'none';
                    }
                });
            }
            updateChart(latestHistoryCached);
        }

        // Keyboard support: Escape exits focus mode
        window.addEventListener('keydown', function(event) {
            if (event.key === 'Escape') {
                const panel = document.getElementById('qa-analyst-panel');
                if (panel && panel.classList.contains('maximized')) {
                    toggleQAMaximize();
                }
                const chartPanel = document.getElementById('equity-curve-panel');
                if (chartPanel && chartPanel.classList.contains('maximized')) {
                    toggleChartMaximize();
                }
                const ordersPanel = document.getElementById('executed-orders-panel');
                if (ordersPanel && ordersPanel.classList.contains('maximized')) {
                    toggleOrdersMaximize();
                }
            }
        });

        function parseChatHistoryToMarkdown() {
            const logEl = document.getElementById('chat-log');
            if (!logEl) return "";
            
            const children = logEl.children;
            const nowEastern = new Date().toLocaleString('en-US', { timeZone: 'America/New_York' });
            let markdown = `# AGE Desk Copilot Chat History - ${nowEastern} (Eastern Time)\n---\n`;
            
            for (let i = 0; i < children.length; i++) {
                const child = children[i];
                if (child.nodeType !== 1) continue;
                if (child.id && child.id.startsWith('loading')) continue;
                
                // Classify sender
                let sender = "AGE Technical Assistant";
                const avatarEl = child.querySelector('[data-lucide]');
                if (avatarEl) {
                    const iconType = avatarEl.getAttribute('data-lucide');
                    if (iconType === 'user') {
                        sender = "User / Quant Investor";
                    } else if (iconType === 'alert-circle') {
                        sender = "AGE Technical Assistant (System Error)";
                    }
                }
                
                // Extract text html
                let textContentEl = null;
                const firstChild = child.children[0];
                const secondChild = child.children[1];
                if (!firstChild) continue;
                
                if (secondChild) {
                    // Check if first child has an icon
                    if (firstChild.querySelector('[data-lucide]') || firstChild.querySelector('svg') || firstChild.querySelector('i')) {
                        // First child is avatar, so second child is text
                        textContentEl = secondChild;
                    } else {
                        // First child is text, second child is avatar
                        textContentEl = firstChild;
                    }
                } else {
                    // Fallback if only one child exists
                    textContentEl = firstChild;
                }
                
                const htmlContent = textContentEl ? textContentEl.innerHTML : "";
                if (!htmlContent) continue;
                
                // Convert HTML to clean markdown
                let text = htmlContent
                    .replace(/<br\s*\/?>/gi, '\\n')
                    .replace(/<strong>(.*?)<\/strong>/gi, '**$1**')
                    .replace(/<b>(.*?)<\/b>/gi, '**$1**')
                    .replace(/<em>(.*?)<\/em>/gi, '*$1*')
                    .replace(/<i>(.*?)<\/i>/gi, '*$1*')
                    .replace(/<code[^>]*>(.*?)<\/code>/gi, '`$1`')
                    .replace(/<[^>]+>/g, ''); // strip any other tags
                
                // Decode basic HTML entities
                text = text
                    .replace(/&lt;/g, '<')
                    .replace(/&gt;/g, '>')
                    .replace(/&quot;/g, '"')
                    .replace(/&#039;/g, "'")
                    .replace(/&amp;/g, '&');
                    
                text = text.trim();
                
                // Approximate a nice timestamp if none exists in DOM
                const timestamp = new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit' });
                
                markdown += `**[${sender}]** (${timestamp})\n${text}\n\n`;
            }
            markdown += `---`;
            return markdown;
        }

        function copyQAToClipboard() {
            const markdown = parseChatHistoryToMarkdown();
            if (!markdown) return;
            
            navigator.clipboard.writeText(markdown).then(() => {
                showToast("Conversation Copied to Clipboard! (Formatted as Markdown)");
            }).catch(err => {
                console.error("Failed to copy conversation: ", err);
                showToast("Failed to copy conversation.", true);
            });
        }

        function exportQAToFile() {
            const markdown = parseChatHistoryToMarkdown();
            if (!markdown) return;
            
            const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            
            const now = new Date();
            const year = now.getFullYear();
            const month = String(now.getMonth() + 1).padStart(2, '0');
            const day = String(now.getDate()).padStart(2, '0');
            const hours = String(now.getHours()).padStart(2, '0');
            const minutes = String(now.getMinutes()).padStart(2, '0');
            const filename = `AGE_Copilot_Conversation_${year}${month}${day}_${hours}${minutes}.md`;
            
            const link = document.createElement('a');
            link.href = url;
            link.setAttribute('download', filename);
            link.style.display = 'none';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            
            showToast(`Conversation exported as ${filename}`);
        }

        function toggleOrdersMaximize() {
            const panel = document.getElementById('executed-orders-panel');
            const btn = document.getElementById('orders-maximize-btn');
            const statsBanner = document.getElementById('orders-stats-banner');
            const exportBtn = document.getElementById('orders-export-btn');
            if (!panel || !btn) return;
            
            const isMaximized = panel.classList.toggle('maximized');
            if (isMaximized) {
                btn.innerHTML = `<i data-lucide="minimize-2" id="orders-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Minimize Orders');
                if (statsBanner) statsBanner.style.display = 'grid';
                if (exportBtn) exportBtn.style.display = 'flex';
            } else {
                btn.innerHTML = `<i data-lucide="maximize-2" id="orders-max-icon" style="width: 0.9rem; height: 0.9rem;"></i>`;
                btn.setAttribute('title', 'Maximize Orders');
                if (statsBanner) statsBanner.style.display = 'none';
                if (exportBtn) exportBtn.style.display = 'none';
            }
            safeCreateIcons();
            // recalculate stats immediately upon toggle
            filterExecutedOrders();
        }

        function filterExecutedOrders() {
            const selectEl = document.getElementById('orders-ticker-select');
            if (!selectEl) return;
            const ticker = selectEl.value;
            const rows = document.querySelectorAll('#trades-tbody tr');
            
            let matchCount = 0;
            let buyVol = 0;
            let sellVol = 0;
            
            rows.forEach(row => {
                // If it's the "No orders registered" placeholder row, skip
                if (row.cells.length < 3) return;
                
                const symbol = row.cells[1].textContent.trim();
                const action = row.cells[2].textContent.trim().toUpperCase();
                const qty = parseFloat(row.cells[3].textContent.trim()) || 0;
                const priceText = row.cells[4].textContent.trim();
                const price = priceText.includes('Unfilled') ? 0 : parseFloat(priceText.replace('$', '').replace(/,/g, '')) || 0;
                
                const isMatch = (ticker === 'ALL' || symbol === ticker);
                if (isMatch) {
                    row.style.display = '';
                    matchCount++;
                    if (action === 'BUY') {
                        buyVol += qty * price;
                    } else if (action === 'SELL') {
                        sellVol += qty * price;
                    }
                } else {
                    row.style.display = 'none';
                }
            });
            
            const netFlow = sellVol - buyVol;
            
            // Update stats
            const countEl = document.getElementById('order-stat-count');
            if (countEl) countEl.textContent = matchCount;
            
            const buysEl = document.getElementById('order-stat-buys');
            if (buysEl) buysEl.textContent = '$' + buyVol.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            
            const sellsEl = document.getElementById('order-stat-sells');
            if (sellsEl) sellsEl.textContent = '$' + sellVol.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            
            const flowEl = document.getElementById('order-stat-flow');
            if (flowEl) {
                flowEl.textContent = (netFlow >= 0 ? '+' : '-') + '$' + Math.abs(netFlow).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                flowEl.style.color = netFlow >= 0 ? 'var(--color-green)' : 'var(--color-crimson)';
            }
        }

        function exportOrdersToCSV() {
            const rows = document.querySelectorAll('#trades-tbody tr');
            if (rows.length === 0 || (rows.length === 1 && rows[0].cells.length < 3)) {
                showToast('No trade history found to export!');
                return;
            }
            
            let csvContent = "Timestamp,Symbol,Action,Qty,Avg Fill Price,Status,Alpaca Order ID\\r\\n";
            let count = 0;
            
            rows.forEach(row => {
                if (row.style.display !== 'none' && row.cells.length >= 7) {
                    const timestamp = row.cells[0].textContent.trim().replace(/,/g, '');
                    const symbol = row.cells[1].textContent.trim();
                    const action = row.cells[2].textContent.trim();
                    const qty = row.cells[3].textContent.trim();
                    const price = row.cells[4].textContent.trim().replace('$', '').replace(/,/g, '');
                    const status = row.cells[5].textContent.trim();
                    const orderId = row.cells[6].textContent.trim();
                    
                    csvContent += `"${timestamp}","${symbol}","${action}",${qty},${price},"${status}","${orderId}"\\r\\n`;
                    count++;
                }
            });
            
            if (count === 0) {
                showToast('No matching records to export!');
                return;
            }
            
            const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            
            const now = new Date();
            const year = now.getFullYear();
            const month = String(now.getMonth() + 1).padStart(2, '0');
            const day = String(now.getDate()).padStart(2, '0');
            const hour = String(now.getHours()).padStart(2, '0');
            const minute = String(now.getMinutes()).padStart(2, '0');
            const filename = `AGE_Orders_Ledger_${year}${month}${day}_${hour}${minute}.csv`;
            
            link.setAttribute("href", url);
            link.setAttribute("download", filename);
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            
            showToast(`Exported ${count} orders to ${filename}`);
        }

        function showToast(message, isError = false) {
            const existingToast = document.getElementById('qa-toast');
            if (existingToast) {
                existingToast.remove();
            }
            
            const toast = document.createElement('div');
            toast.id = 'qa-toast';
            toast.style.position = 'fixed';
            toast.style.top = '2rem';
            toast.style.left = '50%';
            toast.style.transform = 'translateX(-50%) translateY(-20px)';
            toast.style.opacity = '0';
            toast.style.background = isError ? 'rgba(255, 8, 68, 0.95)' : 'rgba(16, 20, 36, 0.95)';
            toast.style.backdropFilter = 'blur(12px)';
            toast.style.color = '#ffffff';
            toast.style.padding = '0.75rem 1.5rem';
            toast.style.borderRadius = '0.5rem';
            toast.style.border = isError ? '1px solid var(--color-crimson)' : '1px solid var(--color-teal)';
            toast.style.boxShadow = isError ? '0 4px 20px rgba(255, 8, 68, 0.3)' : '0 4px 20px rgba(0, 242, 254, 0.3)';
            toast.style.zIndex = '999999';
            toast.style.fontFamily = "'Inter', sans-serif";
            toast.style.fontSize = '0.85rem';
            toast.style.fontWeight = '500';
            toast.style.transition = 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)';
            toast.innerText = message;
            
            document.body.appendChild(toast);
            
            // Force reflow
            toast.offsetHeight;
            
            // Animate in
            toast.style.transform = 'translateX(-50%) translateY(0)';
            toast.style.opacity = '1';
            
            // Animate out after 2.5s
            setTimeout(() => {
                toast.style.transform = 'translateX(-50%) translateY(-20px)';
                toast.style.opacity = '0';
                setTimeout(() => {
                    toast.remove();
                }, 300);
            }, 2500);
        }
    </script>
</body>
</html>
"""

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Prevent logging every static/API poll to stderr to keep the terminal clean
        return

    def do_GET(self):
        if self.path == '/':
            encoded_html = HTML_CONTENT.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded_html)))
            # Add strict anti-caching headers for the main page HTML
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(encoded_html)
        elif self.path == '/api/status':
            try:
                with CACHE_LOCK:
                    # Serve the thread-safe global status cache instantly (sub-millisecond)
                    payload = dict(LATEST_STATUS_CACHE) if LATEST_STATUS_CACHE else {
                        "account": {},
                        "positions": {},
                        "decisions": [],
                        "trades": [],
                        "history": [],
                        "ticker_history": {},
                        "logs": ["Dashboard server is initializing, please wait..."],
                        "trading_universe": config.TRADING_UNIVERSE,
                        "interval": config.TRADING_INTERVAL_MINUTES,
                        "is_mock": True,
                        "is_paper": config.ALPACA_PAPER,
                        "initializing": True
                    }
                
                encoded_payload = json.dumps(payload).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_payload)))
                self.send_header('Access-Control-Allow-Origin', '*')
                # Add strict anti-caching headers for status payload
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                self.end_headers()
                self.wfile.write(encoded_payload)
            except Exception as e:
                encoded_err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_err)))
                self.end_headers()
                self.wfile.write(encoded_err)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/api/chat':
            try:
                # Read content length and parse request body
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                request_payload = json.loads(post_data.decode('utf-8'))
                user_msg = request_payload.get("message", "")
                
                if not user_msg:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Empty message."}).encode('utf-8'))
                    return
                
                # Fetch recent decisions (SQLite)
                decisions = database.get_recent_decisions(limit=10)
                trades = database.get_recent_trades(limit=10)
                
                # Fetch live portfolio state
                client = AlpacaClient()
                account = client.get_account_state()
                positions = client.get_positions()
                
                positions_summary = ""
                if not positions:
                    positions_summary = "No open positions."
                for sym, pos in positions.items():
                    positions_summary += f"- {sym}: {pos['qty']} shares @ ${pos['avg_entry_price']} (Current Val: ${pos['market_value']}, PnL: ${pos['unrealized_pnl']})\n"
                
                dec_summary = ""
                if not decisions:
                    dec_summary = "No recent decisions logged."
                for d in decisions:
                    dec_summary += f"- [{d.get('timestamp')}] Ticker: {d.get('proposed_symbol') or 'PORTFOLIO'}, Action: {d.get('proposed_action')}, Qty: {d.get('proposed_qty')}, Approved: {d.get('is_approved')} (Reason: {d.get('rejection_reason')}), Rationale: {d.get('thought_process')}\n"
                
                trade_summary = ""
                if not trades:
                    trade_summary = "No recent trades logged."
                for t in trades:
                    trade_summary += f"- [{t.get('timestamp')}] Ticker: {t.get('symbol')}, Side: {t.get('side')}, Qty: {t.get('qty')}, Fill Price: {t.get('filled_avg_price')}, Status: {t.get('status')}\n"
                
                # Fetch database performance summary (successes and failures)
                perf_summary = database.get_performance_summary()
                perf_summary_str = perf_summary.get("text_summary", "No performance history available.")
                daily_performance_str = database.get_daily_performance_breakdown(limit=15)

                system_instruction = (
                    "You are the cognitive co-pilot, visual strategist, and expert portfolio analyst for the AGE Desk Autonomous Trading Agent.\n"
                    "The user is a highly particular quant investor who wants to analyze hypotheses, understand historical trends, and evaluate trading performance.\n\n"
                    
                    "=== CRITICAL REQUIREMENT 1: ABSOLUTELY NO CODE OR DATA BLOCKS ===\n"
                    "You MUST NOT output ANY Python code, Javascript, SQL, JSON, bash commands, or block text representations of code under any circumstances.\n"
                    "Do NOT write any code blocks (e.g., do NOT write ```python ... ``` or ```json ... ```). Provide clean, professional English explanations.\n\n"
                    
                    "=== CRITICAL REQUIREMENT 2: ALWAYS OUTPUT CHARTS AND VISUALS ===\n"
                    "Whenever the user asks for analysis, trends, performance, win/loss stats, asset ratios, or hypotheses, you MUST represent this data visually using high-quality QuickChart charts.\n"
                    "Never just output raw lists of numbers when you can format them into a stunning, responsive chart!\n"
                    "Generate charts using the QuickChart.io API: `https://quickchart.io/chart?c={...}`.\n"
                    "You must embed them using standard Markdown image syntax: `![Chart Title](https://quickchart.io/chart?c=...)`.\n\n"
                    
                    "=== QUICKCHART SPECIFICATIONS & STYLING ===\n"
                    "Keep the chart config clean, using single-quoted strings, without spaces, to ensure perfect URL loading. Apply our premium dark theme style:\n"
                    "- Palette Colors: Teal (`#00f2fe`), Blue (`#0070f3`), Purple (`#8b5cf6`), Pink (`#ff007f`), Green/Profit (`#10b981`), Red/Loss (`#ef4444`), Dark BG (`rgba(26,31,54,0.8)`).\n"
                    "- Fonts: Use clean modern default sans-serif.\n"
                    "- Available Chart Templates:\n"
                    "  1. Line Chart (e.g., Equity or Price trends):\n"
                    "     `https://quickchart.io/chart?c={type:'line',data:{labels:['Start','Peak','Current'],datasets:[{label:'Equity',data:[100000,112000,105300],borderColor:'#00f2fe',backgroundColor:'rgba(0,242,254,0.1)',fill:true}]}}`\n"
                    "  2. Bar Chart (e.g., Wins vs Losses or Trades count):\n"
                    "     `https://quickchart.io/chart?c={type:'bar',data:{labels:['Wins','Losses'],datasets:[{label:'TradesCount',data:[14,6],backgroundColor:['#10b981','#ef4444']}]}}`\n"
                    "  3. Doughnut/Pie Chart (e.g., Asset Allocations):\n"
                    "     `https://quickchart.io/chart?c={type:'doughnut',data:{labels:['SPY','QQQ','SOL','Cash'],datasets:[{data:[30,25,15,30],backgroundColor:['#00f2fe','#0070f3','#8b5cf6','#1a1f36']}]}}`\n\n"
                    
                    "=== CURRENT REAL-TIME CONTEXT ===\n"
                    "=== CURRENT PORTFOLIO STATE ===\n"
                    f"- Total Equity: ${account.get('equity', 100000.0):,.2f}\n"
                    f"- Cash: ${account.get('cash', 100000.0):,.2f}\n"
                    f"- Unrealized PnL: ${account.get('unrealized_pnl', 0.0):,.2f}\n\n"
                    "=== ACTIVE HOLDINGS ===\n"
                    f"{positions_summary}\n"
                    "=== RECENT STRATEGY DECISIONS & THOUGHT PROCESSES ===\n"
                    f"{dec_summary}\n"
                    "=== RECENT BROKER ORDER EXECUTIONS ===\n"
                    f"{trade_summary}\n"
                    "=== REAL-TIME DAILY PERFORMANCE TELEMETRY ===\n"
                    f"{daily_performance_str}\n\n"
                    "DIRECTIONS:\n"
                    "1. Always base your answers on the actual provided state, decisions, trades, and daily performance telemetry. Be extremely factual.\n"
                    "2. If the user asks why you bought, sold, or held a specific asset, look up your recent decision log's `Rationale` or check if the order failed due to guardrails. Explain the math, technical indicators, and pivot zones that led to that action.\n"
                    "3. Be educational. Explain concepts like RSI, Fibonacci retracements, Bollinger Bands, and support/resistance so the user can learn quantitative trading.\n"
                    "4. Keep your responses engaging, clear, and formatted in clean markdown. Keep paragraphs relatively concise so they look good in a small chat widget.\n"
                    "5. When the user asks for daily charts, performance summaries, or day-over-day trends, you MUST use the exact dates, ending equities, and trade metrics from the '=== REAL-TIME DAILY PERFORMANCE TELEMETRY ===' block above. NEVER make up or hallucinate any dates or data points under any circumstances. There is no trading on weekends, so do not include weekend dates (e.g. Saturdays, Sundays) in any daily charts or daily tables unless the database explicitly contains them.\n"
                )
                
                response_text = ""
                if GENAI_AVAILABLE and config.GEMINI_API_KEY and config.GEMINI_API_KEY != "your_gemini_api_key_here":
                    try:
                        g_client = genai.Client(api_key=config.GEMINI_API_KEY)
                        response = g_client.models.generate_content(
                            model=config.GEMINI_MODEL,
                            contents=user_msg,
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction
                            )
                        )
                        response_text = response.text
                    except Exception as llm_err:
                        response_text = f"Failed to query Gemini API: {llm_err}. Please ensure your GEMINI_API_KEY is correct in your .env file."
                else:
                    response_text = "Gemini API Client is offline or missing credentials. Please make sure google-genai is installed and GEMINI_API_KEY is configured."
                
                encoded_chat_res = json.dumps({"response": response_text}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_chat_res)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(encoded_chat_res)
                
            except Exception as e:
                encoded_chat_err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_chat_err)))
                self.end_headers()
                self.wfile.write(encoded_chat_err)
        elif self.path == '/api/reset_history':
            try:
                # Clear all records from portfolio_history table to purge polluted data
                with database.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM portfolio_history")
                    conn.commit()
                
                # Instantly clear cache so UI reacts immediately
                with CACHE_LOCK:
                    if LATEST_STATUS_CACHE:
                        LATEST_STATUS_CACHE["history"] = []
                
                encoded_reset_res = json.dumps({"success": True, "message": "Telemetry history has been reset successfully."}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_reset_res)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(encoded_reset_res)
            except Exception as e:
                encoded_reset_err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_reset_err)))
                self.end_headers()
                self.wfile.write(encoded_reset_err)
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    # Start the background status cache worker daemon thread
    worker_thread = threading.Thread(target=status_cache_worker, daemon=True)
    worker_thread.start()

    # Attempt to start server on specified port, retry on 8081 if occupied
    global PORT
    while PORT < 8090:
        try:
            handler = DashboardHandler
            with socketserver.ThreadingTCPServer(("", PORT), handler) as httpd:
                print("=" * 60)
                print(f"[AGE DESK] - AUTONOMOUS AI TRADING DASHBOARD")
                print(f"[*] Live Control Center is active and waiting for connections.")
                print(f"[*] Local Access URL: http://localhost:{PORT}")
                print(f"[*] Real-time Telemetry: Auto-polling database and files every 10s.")
                print("=" * 60)
                print("Press Ctrl+C to gracefully shut down the dashboard server.")
                httpd.serve_forever()
        except OSError:
            print(f"[Warning] Port {PORT} is occupied. Attempting next port...")
            PORT += 1

if __name__ == "__main__":
    try:
        run_server()
    except KeyboardInterrupt:
        print("\n[Dashboard Server] Shutdown signal received. Exiting.")
        sys.exit(0)
