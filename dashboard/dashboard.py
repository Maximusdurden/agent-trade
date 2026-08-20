import http.server
import socketserver
import json
import logging
import os
import sys
import sqlite3
import threading
import time
from datetime import datetime, timezone

# Initialize google.cloud.storage first to prevent Python namespace conflicts with google-genai
try:
    from google.cloud import storage
    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False

# Add parent folder to path to ensure root and core package imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config
from core import database
from core.alpaca_client import AlpacaClient
from core.screener import load_screener_pool

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

def sync_database_from_gcs():
    """
    Downloads the latest trading_agent.db from Google Cloud Storage
    to the local writeable /tmp/ folder if running inside a GCP Cloud Run environment.
    """
    gcs_bucket = os.getenv("GCS_BUCKET_NAME")
    if not gcs_bucket:
        # Not configured for GCS, assume local execution
        return
        
    db_filename = os.getenv("DATABASE_FILENAME", "trading_agent.db")
    # Verify we need to sync and that we are in a serverless environment (e.g. database path points to /tmp)
    if "/tmp" not in db_filename:
        return

    # Check cache TTL (avoid downloading on every single HTTP asset request)
    local_temp_db = db_filename
    last_sync_key = "_gcs_last_sync_time"
    now = time.time()
    
    with CACHE_LOCK:
        last_sync = LATEST_STATUS_CACHE.get(last_sync_key, 0.0)
        # Check if downloaded in the last 120 seconds (2 minutes)
        if last_sync and (now - last_sync) < 120:
            return

    try:
        from google.cloud import storage
        print(f"[GCS Sync] Checking for file updates in gs://{gcs_bucket}...", flush=True)
        client = storage.Client()
        bucket = client.bucket(gcs_bucket)
        
        # 1. Download database
        blob = bucket.blob("trading_agent.db")
        os.makedirs(os.path.dirname(local_temp_db), exist_ok=True)
        # Download to a temp file first, then atomically replace the live DB.
        # This prevents readers (e.g. the chat handler) from opening a
        # partially-written or locked SQLite file mid-download, which caused
        # intermittent "database is locked" 500 errors on /api/chat.
        tmp_db = local_temp_db + ".tmp"
        blob.download_to_filename(tmp_db)
        os.replace(tmp_db, local_temp_db)
        print(f"[GCS Sync] Successfully synchronized {local_temp_db} from Cloud Storage.", flush=True)
        
        # Ensure database tables exist in the downloaded database (Auto-Migration)
        try:
            database.init_db()
            print("[GCS Sync] Database tables auto-initialized/migrated successfully.", flush=True)
        except Exception as init_err:
            print(f"[GCS Sync WARNING] Failed to auto-initialize database tables: {init_err}", file=sys.stderr)
        
        # 2. Download trading.log
        log_blob = bucket.blob("trading.log")
        try:
            if log_blob.exists():
                log_blob.download_to_filename("/tmp/trading.log")
                print("[GCS Sync] Successfully synchronized /tmp/trading.log from Cloud Storage.", flush=True)
        except Exception as log_sync_err:
            print(f"[GCS Sync WARNING] Failed to sync trading.log: {log_sync_err}", file=sys.stderr)
            
        # 3. Download portfolio_dod_balances.csv
        csv_blob = bucket.blob("portfolio_dod_balances.csv")
        try:
            if csv_blob.exists():
                csv_blob.download_to_filename("/tmp/portfolio_dod_balances.csv")
                print("[GCS Sync] Successfully synchronized /tmp/portfolio_dod_balances.csv from Cloud Storage.", flush=True)
        except Exception as csv_sync_err:
            print(f"[GCS Sync WARNING] Failed to sync portfolio_dod_balances.csv: {csv_sync_err}", file=sys.stderr)

        with CACHE_LOCK:
            LATEST_STATUS_CACHE[last_sync_key] = now
    except Exception as e:
        print(f"[GCS Sync WARNING] Failed to sync files from GCS: {e}", file=sys.stderr)

def get_portfolio_history():
    """Retrieves portfolio history from the SQLite database."""
    try:
        conn = database.get_db_connection()
        try:
            cursor = conn.cursor()
            # Fetch ALL portfolio history records (oldest to newest) so the full
            # equity valuation curve back to inception is always available and
            # new records are always appended. No LIMIT cap so longer timeframes
            # (5D/1M/ALL) are not truncated to the most recent few days.
            cursor.execute("SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp ASC")
            rows = cursor.fetchall()
            history = [dict(row) for row in rows]
        finally:
            conn.close() # Explicitly close SQLite connection securely
            
        # Rows are already in chronological order (oldest to newest), which the
        # Chart.js timeline renders oldest -> newest.
        
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
        conn = database.get_db_connection()
        try:
            cursor = conn.cursor()
            # Fetch ALL decision records (oldest to newest) so ticker position
            # curves reconstruct the full history back to inception. No LIMIT cap
            # so per-ticker dropdowns and longer timeframes are not truncated.
            cursor.execute("SELECT timestamp, portfolio_state FROM decisions ORDER BY timestamp ASC")
            rows = cursor.fetchall()
        finally:
            conn.close() # Explicitly close SQLite connection securely
            
        # Already in chronological order (oldest to newest)
        rows = list(rows)
        
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

def get_latest_watchlist():
    """Retrieves the latest screened watchlist from the SQLite database."""
    try:
        conn = database.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT watchlist FROM watchlist_history ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                raw_watchlist = json.loads(row["watchlist"])
                # Normalize watchlist symbols to uppercase and remove slashes
                # to guarantee alignment with front-end string-matching logic
                return [symbol.upper().replace('/', '') for symbol in raw_watchlist]
        finally:
            conn.close() # Explicitly close SQLite connection securely
    except Exception as e:
        print(f"[Dashboard Server] Error fetching latest watchlist: {e}", file=sys.stderr)
    return []


def get_data_freshness(heartbeat: dict, interval_minutes: int | None = None,
                       now_utc: datetime | None = None) -> dict:
    """Classify runner health independently from the latest trading decision.

    Uses the cycle heartbeat (started_at / completed_at / status) rather than
    the last trade timestamp so the dashboard can report STALE, RUNNING,
    FAILED, or CRYPTO_ONLY_MONITORING even when no trades occurred. Staleness
    is defined as 2× the configured interval plus a 5-minute grace period.
    """
    interval = interval_minutes or config.TRADING_INTERVAL_MINUTES
    current_time = now_utc or datetime.now(timezone.utc)
    completed_at = heartbeat.get("completed_at", "")
    started_at = heartbeat.get("started_at", "")
    status = heartbeat.get("status", "")
    reference = completed_at or started_at
    age_seconds = None
    if reference:
        try:
            parsed = datetime.fromisoformat(reference.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(0, (current_time - parsed).total_seconds())
        except ValueError:
            pass

    stale_after_seconds = (interval * 2 + 5) * 60
    if not reference or age_seconds is None or age_seconds > stale_after_seconds:
        state = "STALE"
    elif status == "STARTED":
        state = "RUNNING"
    elif status in {"FAILED", "ACCOUNT_STATE_FAILED", "NO_MARKET_DATA", "GCS_UPLOAD_FAILED"}:
        state = "FAILED"
    elif status == "SKIPPED_KILL_SWITCH":
        state = "KILL_SWITCH_HALTED"
    elif heartbeat.get("asset_scope") == "CRYPTO_ONLY":
        state = "CRYPTO_ONLY_MONITORING"
    else:
        state = "HEALTHY"

    return {
        "state": state,
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "server_time_utc": current_time.isoformat().replace("+00:00", "Z"),
    }

def status_cache_worker():
    """Background thread that periodically fetches Alpaca status and SQLite data to update the global cache."""
    global LATEST_STATUS_CACHE
    print("[Dashboard Server] Background status cache worker started.", flush=True)
    while True:
        try:
            # Sync SQLite database if configured for Google Cloud Storage
            sync_database_from_gcs()

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
            executions = []
            history = []
            ticker_history = {}
            broker_orders = []
            ticker_convictions = []
            try:
                decisions = database.get_recent_decisions(limit=15)
                trades = database.get_recent_trades(limit=15)
                executions = database.get_executions(limit=50)
                history = get_portfolio_history()
                ticker_history = get_ticker_history()
                try:
                    ticker_convictions = database.get_ticker_convictions(limit=200)
                except Exception as tc_err:
                    print(f"[Dashboard Server] Ticker convictions fetch failed (non-fatal): {tc_err}", file=sys.stderr)
                    ticker_convictions = []
                
                # 2b. Fetch executed orders directly from Alpaca.  This catches
                #     TP/SL bracket fills and broker-side sells that the runner
                #     never logged to the local trades table.  Broker-supplied
                #     orders are deduplicated by alpaca_order_id — the broker
                #     wins and the local DB fills any gaps.
                try:
                    if not client.is_mock:
                        alpaca_orders = client.get_executed_orders(limit=100)
                        # Deduplicate by alpaca_order_id — broker wins, DB fills gaps
                        db_ids = {t.get("alpaca_order_id") for t in trades if t.get("alpaca_order_id")}
                        for ao in alpaca_orders:
                            if ao.get("alpaca_order_id") not in db_ids:
                                broker_orders.append(ao)
                            # else: already in trades, skip duplicate
                        print(f"[Dashboard Server] Fetched {len(alpaca_orders)} broker orders, {len(broker_orders)} new (not in DB).", flush=True)
                except Exception as broker_err:
                    print(f"[Dashboard Server] Broker order fetch failed (non-fatal): {broker_err}", file=sys.stderr)
                    broker_orders = []
            except Exception as db_err:
                print(f"[Dashboard Server] Database retrieval failed: {db_err}", file=sys.stderr)

            heartbeat = database.get_cycle_heartbeat()
            freshness = get_data_freshness(heartbeat)
            latest_decision_at = decisions[0].get("timestamp", "") if decisions else ""
            latest_portfolio_at = history[-1].get("timestamp", "") if history else ""

            # 3. Retrieve Latest Log Lines
            log_lines = []
            try:
                log_file_path = "/tmp/trading.log" if os.getenv("GCS_BUCKET_NAME") else config.LOG_FILE
                if os.path.exists(log_file_path):
                    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        raw_lines = f.readlines()[-200:]  # pull more so filtering keeps 60 clean
                    # Filter out stale local-test artifacts / benign noise so the
                    # System Activity Logs tail isn't polluted by old mock-test runs
                    # (e.g. "RuntimeError: boom", "execution=local", unittest mocks)
                    # or repeated boilerplate. This keeps the live tail focused on
                    # real operational output.
                    _STALE_NOISE_PATTERNS = [
                        "RuntimeError: boom",
                        "unittest.mock",
                        "execution=local scope=UNKNOWN",
                        "Cycle heartbeat finalized: execution=local",
                    ]
                    filtered = [
                        line for line in raw_lines
                        if not any(p in line for p in _STALE_NOISE_PATTERNS)
                    ]
                    log_lines = filtered[-60:]
            except Exception as log_err:
                print(f"[Dashboard Server] Log file retrieval failed: {log_err}", file=sys.stderr)

            # 3b. Retrieve DoD Balances from CSV
            dod_balances = []
            csv_path = "/tmp/portfolio_dod_balances.csv" if os.getenv("GCS_BUCKET_NAME") else "portfolio_dod_balances.csv"
            if os.path.exists(csv_path):
                try:
                    import csv
                    with open(csv_path, "r", encoding="utf-8") as f:
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

            # GCS Kill Switch Check
            kill_switch_state = "ACTIVE"
            try:
                from core.gcs_sync import check_kill_switch
                ks_data = check_kill_switch()
                if ks_data:
                    kill_switch_state = ks_data.get("status", "ACTIVE")
            except Exception as ks_err:
                print(f"[Dashboard Server] Failed to read kill switch: {ks_err}", file=sys.stderr)

            # Assemble payload
            payload = {
                "account": account,
                "positions": positions,
                "decisions": decisions,
                "trades": trades,
                "executions": executions,
                "ticker_convictions": ticker_convictions,
                "broker_orders": broker_orders,
                "history": history,
                "ticker_history": ticker_history,
                "dod_balances": dod_balances,
                "logs": log_lines,
                "trading_universe": config.TRADING_UNIVERSE,
                "screener_pool": load_screener_pool(),
                "latest_watchlist": get_latest_watchlist(),
                "interval": config.TRADING_INTERVAL_MINUTES,
                "is_mock": is_mock,
                "is_paper": config.ALPACA_PAPER,
                "kill_switch": kill_switch_state,
                "heartbeat": heartbeat,
                "freshness": freshness,
                "latest_decision_at": latest_decision_at,
                "latest_portfolio_at": latest_portfolio_at,
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
                        "executions": [],
                        "broker_orders": [],
                        "history": [],
                        "ticker_history": {},
                        "logs": [f"[SERVER ERROR] Background cache failed to initialize: {e}"],
                        "trading_universe": config.TRADING_UNIVERSE,
                        "screener_pool": [],
                        "latest_watchlist": [],
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
            /* P-1 Green Phosphor (Default theme variables) */
            --crt-color: #33ff33;
            --crt-color-rgb: 51, 255, 51;
            --crt-color-dim: #008000;
            --crt-bg: #030703;
            --crt-surface: rgba(5, 12, 5, 0.85);
            --crt-surface-elevated: rgba(10, 24, 10, 0.95);
            --crt-border: rgba(51, 255, 51, 0.25);
            --crt-glow: rgba(51, 255, 51, 0.12);
            --crt-text-glow: 0 0 4px rgba(51, 255, 51, 0.55), 0 0 8px rgba(51, 255, 51, 0.25);
            
            --font-pixel: 'VT323', monospace;
            --font-data: 'Share Tech Mono', monospace;

            /* Tri-Phosphor Palette: Combining Green, Amber, Cyan inside Retro Console views */
            --color-green: #33ff33;      /* P1 Green Phosphor for Holdings, Gains, Passes */
            --color-crimson: #ffb000;    /* P3 Amber Phosphor for Warnings, Losses, Drawdowns */
            --color-gold: #ffb000;       /* P3 Amber Phosphor for Intervals, Warnings, Screener Pool */
            --color-teal: #00ffff;       /* P4 Cyan Phosphor for Focus, Primary, Headers */
            --color-blue: #00ffff;       /* P4 Cyan Phosphor for Watchlists, Connections */

            --text-primary: #33ff33;     /* Main text is Green by default */
            --text-secondary: #008000;   /* Secondary is Dim Green */
            --text-muted: rgba(51, 255, 51, 0.4);
            --border-subtle: rgba(51, 255, 51, 0.15);
            
            /* Original Glass theme variables for fallback storage */
            --modern-bg-base: #0a0d16;
            --modern-bg-surface: #101424;
            --modern-bg-surface-elevated: #161c33;
            --modern-border-subtle: rgba(255, 255, 255, 0.07);
            --modern-border-glow: rgba(0, 242, 254, 0.15);
            --modern-text-primary: #f3f4f6;
            --modern-text-secondary: #9ca3af;
            --modern-text-muted: #6b7280;
            --modern-color-teal: #00f2fe;
            --modern-color-blue: #0070f3;
            --modern-color-crimson: #ff0844;
            --modern-color-gold: #f6d365;
            --modern-color-green: #10b981;
            --modern-glass-gradient: linear-gradient(135deg, rgba(16, 20, 36, 0.7) 0%, rgba(10, 13, 22, 0.9) 100%);
        }

        /* P-3 AMBER PHOSPHOR THEME */
        body.theme-amber {
            --crt-color: #ffb000;
            --crt-color-rgb: 255, 176, 0;
            --crt-color-dim: #996600;
            --crt-bg: #060400;
            --crt-surface: rgba(16, 10, 0, 0.85);
            --crt-surface-elevated: rgba(30, 20, 0, 0.95);
            --crt-border: rgba(255, 176, 0, 0.25);
            --crt-glow: rgba(255, 176, 0, 0.12);
            --crt-text-glow: 0 0 4px rgba(255, 176, 0, 0.55), 0 0 8px rgba(255, 176, 0, 0.25);

            --text-primary: #ffb000;
            --text-secondary: #996600;
            --text-muted: rgba(255, 176, 0, 0.4);
            --border-subtle: rgba(255, 176, 0, 0.15);
        }

        /* P-4 CYAN PHOSPHOR THEME */
        body.theme-cyan {
            --crt-color: #00ffff;
            --crt-color-rgb: 0, 255, 255;
            --crt-color-dim: #008b8b;
            --crt-bg: #000606;
            --crt-surface: rgba(0, 16, 16, 0.85);
            --crt-surface-elevated: rgba(0, 30, 30, 0.95);
            --crt-border: rgba(0, 255, 255, 0.25);
            --crt-glow: rgba(0, 255, 255, 0.12);
            --crt-text-glow: 0 0 4px rgba(0, 255, 255, 0.55), 0 0 8px rgba(0, 255, 255, 0.25);

            --text-primary: #00ffff;
            --text-secondary: #008b8b;
            --text-muted: rgba(0, 255, 255, 0.4);
            --border-subtle: rgba(0, 255, 255, 0.15);
        }

        /* MODERN GLASS FALLBACK THEME */
        body.theme-modern {
            --crt-color: var(--modern-text-primary);
            --crt-color-dim: var(--modern-text-secondary);
            --crt-bg: var(--modern-bg-base);
            --crt-surface: var(--modern-bg-surface);
            --crt-surface-elevated: var(--modern-bg-surface-elevated);
            --crt-border: var(--modern-border-subtle);
            --crt-glow: var(--modern-border-glow);
            --crt-text-glow: none;
            --font-pixel: 'Outfit', sans-serif;
            --font-data: 'Inter', sans-serif;

            --color-teal: var(--modern-color-teal);
            --color-blue: var(--modern-color-blue);
            --color-crimson: var(--modern-color-crimson);
            --color-gold: var(--modern-color-gold);
            --color-green: var(--modern-color-green);
            
            --text-primary: var(--modern-text-primary);
            --text-secondary: var(--modern-text-secondary);
            --text-muted: var(--modern-text-muted);
            --border-subtle: var(--modern-border-subtle);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: var(--font-data);
            background-color: var(--crt-bg);
            color: var(--crt-color);
            overflow-x: hidden;
            min-height: 100vh;
            transition: background-color 0.3s ease, color 0.3s ease;
        }

        /* CRT Screen Layout Wrapper */
        #crt-frame {
            position: relative;
            width: 100%;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        /* CRT Effects Overlay Layers */
        #crt-screen-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            pointer-events: none;
            z-index: 999999;
        }

        #scanlines {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(
                rgba(18, 16, 16, 0) 50%, 
                rgba(0, 0, 0, 0.18) 50%
            );
            background-size: 100% 4px;
        }

        #noise {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            opacity: 0.035;
            background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
            animation: noise-move 0.2s steps(4) infinite;
        }

        #vignette {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            box-shadow: inset 0 0 80px rgba(0, 0, 0, 0.95);
            background: radial-gradient(circle, transparent 45%, rgba(0, 0, 0, 0.45) 100%);
        }

        @keyframes noise-move {
            0%, 100% { background-position: 0 0; }
            25% { background-position: 10px 15px; }
            50% { background-position: -5px 10px; }
            75% { background-position: -12px -5px; }
        }

        @keyframes crt-flicker {
            0% { opacity: 0.985; }
            50% { opacity: 1.0; }
            100% { opacity: 0.988; }
        }

        /* Screen Flicker active only in retro modes when CRT effects are on */
        body:not(.crt-off):not(.theme-modern) #crt-frame {
            animation: crt-flicker 0.15s infinite;
        }

        /* CRT Disable overrides */
        body.crt-off #scanlines,
        body.crt-off #noise,
        body.crt-off #vignette {
            display: none !important;
        }
        body.crt-off * {
            text-shadow: none !important;
            box-shadow: none !important;
            animation: none !important;
        }

        /* Scrollbars Customization */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.35);
            border-left: 1px dashed var(--crt-border);
        }
        ::-webkit-scrollbar-thumb {
            background: var(--crt-color-dim);
            border: 1px solid var(--crt-color);
        }
        body.theme-modern ::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.2);
            border-left: none;
        }
        body.theme-modern ::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.15);
            border: none;
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: var(--crt-color);
            box-shadow: var(--crt-text-glow);
        }

        /* Layout Elements */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.25rem 2rem;
            border-bottom: 2px solid var(--crt-color);
            background: var(--crt-surface);
            backdrop-filter: blur(12px);
            position: sticky;
            top: 0;
            z-index: 100;
            transition: all 0.3s ease;
        }

        body.theme-modern header {
            border-bottom: 1px solid var(--modern-border-subtle);
            background: rgba(16, 20, 36, 0.5);
        }

        .brand-container {
            display: flex;
            align-items: center;
            gap: 0.85rem;
        }

        .brand-title {
            font-family: var(--font-pixel);
            font-size: 1.65rem;
            font-weight: 800;
            color: var(--crt-color);
            text-shadow: var(--crt-text-glow);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .system-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-family: var(--font-pixel);
            font-size: 0.9rem;
            font-weight: 600;
            background: rgba(0, 0, 0, 0.4);
            padding: 0.35rem 0.75rem;
            border-radius: 2px;
            border: 1px solid var(--crt-border);
            box-shadow: var(--crt-text-glow);
        }

        body.theme-modern .system-status {
            border-radius: 2rem;
            background: rgba(16, 20, 36, 0.8);
            border: 1px solid var(--modern-border-subtle);
            box-shadow: none;
            font-family: var(--font-data);
        }

        .status-dot {
            width: 0.5rem;
            height: 0.5rem;
            border-radius: 50%;
            background-color: var(--crt-color);
            box-shadow: var(--crt-text-glow);
            animation: blink 1.5s infinite;
        }

        main {
            padding: 2rem;
            max-width: 1600px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 2rem;
            width: 100%;
        }

        @media (max-width: 1100px) {
            main {
                grid-template-columns: 1fr;
            }
        }

        /* Metrics Bar */
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
            background: var(--crt-surface);
            border: 2px double var(--crt-color);
            border-radius: 2px;
            padding: 1.25rem 1.5rem;
            position: relative;
            overflow: hidden;
            transition: all 0.3s ease;
            box-shadow: inset 0 0 10px rgba(var(--crt-color-rgb), 0.03);
        }

        body.theme-modern .metric-card {
            background: var(--modern-glass-gradient);
            border: 1px solid var(--modern-border-subtle);
            border-radius: 1rem;
            backdrop-filter: blur(8px);
            box-shadow: none;
        }

        .metric-card:hover {
            transform: translateY(-2px);
            border-color: var(--crt-color);
            box-shadow: 0 0 12px var(--crt-glow), inset 0 0 10px rgba(var(--crt-color-rgb), 0.05);
        }

        body.theme-modern .metric-card:hover {
            border-color: var(--modern-color-blue);
            box-shadow: none;
        }

        .metric-label {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--crt-color);
            opacity: 0.8;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }

        body.theme-modern .metric-label {
            color: var(--modern-text-secondary);
            opacity: 1;
        }

        .metric-value {
            font-family: var(--font-pixel);
            font-size: 2.15rem;
            font-weight: 700;
            letter-spacing: 0.02em;
            color: var(--crt-color);
            text-shadow: var(--crt-text-glow);
        }

        body.theme-modern .metric-value {
            font-family: 'Outfit', sans-serif;
            font-size: 1.85rem;
            letter-spacing: -0.02em;
            text-shadow: none;
            color: var(--modern-text-primary);
        }

        .metric-sub {
            font-size: 0.725rem;
            color: var(--crt-color-dim);
            margin-top: 0.35rem;
        }

        body.theme-modern .metric-sub {
            color: var(--modern-text-muted);
        }

        .text-green { color: var(--crt-color) !important; text-shadow: var(--crt-text-glow); }
        .text-crimson { color: #f23d3d !important; text-shadow: 0 0 4px rgba(242, 61, 61, 0.4); }
        .text-gold { color: #e6a100 !important; text-shadow: 0 0 4px rgba(230, 161, 0, 0.4); }

        body.theme-modern .text-green { color: var(--modern-color-green) !important; text-shadow: none; }
        body.theme-modern .text-crimson { color: var(--modern-color-crimson) !important; text-shadow: none; }
        body.theme-modern .text-gold { color: var(--modern-color-gold) !important; text-shadow: none; }

        /* Card Section Panels */
        .card-panel {
            background: var(--crt-surface);
            border: 2px double var(--crt-color);
            border-radius: 2px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            position: relative;
            box-shadow: inset 0 0 10px rgba(var(--crt-color-rgb), 0.03);
            transition: all 0.3s ease;
        }

        body.theme-modern .card-panel {
            background: var(--modern-glass-gradient);
            border: 1px solid var(--modern-border-subtle);
            border-radius: 1.25rem;
            backdrop-filter: blur(12px);
            box-shadow: none;
        }

        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px dashed var(--crt-border);
            padding-bottom: 0.75rem;
            font-family: var(--font-pixel);
            letter-spacing: 0.05em;
        }

        body.theme-modern .panel-header {
            border-bottom: 1px solid var(--modern-border-subtle);
            font-family: var(--font-data);
        }

        .panel-title {
            font-family: var(--font-pixel);
            font-size: 1.35rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: var(--crt-color);
            text-shadow: var(--crt-text-glow);
            text-transform: uppercase;
        }

        body.theme-modern .panel-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.15rem;
            text-shadow: none;
            text-transform: none;
            color: var(--modern-text-primary);
        }

        /* Thought Decision Stream */
        .thought-stream {
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            max-height: 700px;
            overflow-y: auto;
            padding-right: 0.25rem;
        }

        .thought-card {
            background: rgba(0, 0, 0, 0.3);
            border: 1px dashed var(--crt-border);
            border-radius: 2px;
            padding: 1.25rem;
            transition: all 0.25s ease;
            position: relative;
        }

        body.theme-modern .thought-card {
            background: var(--modern-bg-surface);
            border: 1px solid var(--modern-border-subtle);
            border-radius: 0.75rem;
        }

        .thought-card:hover {
            border-color: var(--crt-color);
            box-shadow: 0 0 10px var(--crt-glow);
        }

        body.theme-modern .thought-card:hover {
            border-color: rgba(0, 242, 254, 0.25);
            box-shadow: 0 4px 20px rgba(0, 242, 254, 0.05);
        }

        .thought-card.approved {
            border-left: 4px solid var(--crt-color);
        }

        body.theme-modern .thought-card.approved {
            border-left: 3px solid var(--modern-color-green);
        }

        .thought-card.rejected {
            border-left: 4px solid #f23d3d;
            opacity: 0.85;
        }

        body.theme-modern .thought-card.rejected {
            border-left: 3px solid var(--modern-color-crimson);
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
            font-family: var(--font-pixel);
            font-size: 1.3rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: var(--crt-color);
            text-shadow: var(--crt-text-glow);
        }

        body.theme-modern .thought-ticker {
            font-family: 'Outfit', sans-serif;
            font-size: 1.1rem;
            text-shadow: none;
            color: var(--modern-text-primary);
        }

        .badge {
            font-size: 0.65rem;
            font-weight: 700;
            padding: 0.15rem 0.4rem;
            border-radius: 2px;
            text-transform: uppercase;
        }

        body.theme-modern .badge {
            border-radius: 0.25rem;
        }

        .badge-buy { background: rgba(51, 255, 51, 0.1); color: var(--crt-color); border: 1px solid var(--crt-color); }
        .badge-sell { background: rgba(242, 61, 61, 0.1); color: #f23d3d; border: 1px solid #f23d3d; }
        .badge-hold { background: rgba(230, 161, 0, 0.1); color: #e6a100; border: 1px solid #e6a100; }

        body.theme-modern .badge-buy { background: rgba(16, 185, 129, 0.15); color: var(--modern-color-green); border: 1px solid rgba(16, 185, 129, 0.3); }
        body.theme-modern .badge-sell { background: rgba(239, 68, 68, 0.15); color: var(--modern-color-crimson); border: 1px solid rgba(239, 68, 68, 0.3); }
        body.theme-modern .badge-hold { background: rgba(245, 158, 11, 0.15); color: var(--modern-color-gold); border: 1px solid rgba(245, 158, 11, 0.3); }

        .thought-time {
            font-size: 0.725rem;
            color: var(--crt-color-dim);
        }

        body.theme-modern .thought-time {
            color: var(--modern-text-muted);
        }

        .thought-text {
            font-size: 0.85rem;
            line-height: 1.5;
            color: var(--crt-color);
            opacity: 0.95;
            background: rgba(0, 0, 0, 0.4);
            padding: 0.75rem;
            border-radius: 2px;
            border: 1px solid var(--crt-border);
            white-space: pre-line;
        }

        body.theme-modern .thought-text {
            color: var(--modern-text-secondary);
            opacity: 1;
            background: rgba(10, 13, 22, 0.3);
            border-radius: 0.5rem;
            border: 1px solid rgba(255, 255, 255, 0.02);
        }

        .confluence-box {
            display: flex;
            gap: 1rem;
            margin-top: 0.75rem;
            font-size: 0.75rem;
        }

        .confluence-item {
            background: rgba(0, 0, 0, 0.5);
            padding: 0.25rem 0.5rem;
            border-radius: 2px;
            border: 1px solid var(--crt-border);
            color: var(--crt-color);
        }

        body.theme-modern .confluence-item {
            background: var(--modern-bg-surface-elevated);
            border-radius: 0.25rem;
            border: 1px solid var(--modern-border-subtle);
            color: var(--modern-text-primary);
        }

        /* Tables Customization */
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
            border-bottom: 2px solid var(--crt-color);
            color: var(--crt-color-dim);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.725rem;
            letter-spacing: 0.05em;
        }

        body.theme-modern .trades-table th {
            border-bottom: 1px solid var(--modern-border-subtle);
            color: var(--modern-text-muted);
        }

        .trades-table td {
            padding: 0.85rem 1rem;
            border-bottom: 1px dashed var(--crt-border);
        }

        body.theme-modern .trades-table td {
            border-bottom: 1px solid rgba(255, 255, 255, 0.02);
        }

        .trades-table tr:hover {
            background: rgba(var(--crt-color-rgb), 0.04);
        }

        body.theme-modern .trades-table tr:hover {
            background: rgba(255, 255, 255, 0.01);
        }

        /* Activity Console live tails */
        .terminal-pane {
            background: rgba(0, 0, 0, 0.65);
            border: 2px solid var(--crt-color);
            border-radius: 2px;
            padding: 1rem;
            font-family: var(--font-data);
            font-size: 0.75rem;
            line-height: 1.42;
            color: var(--crt-color);
            height: 350px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
        }

        body.theme-modern .terminal-pane {
            background: #05070f;
            border: 1px solid #1a1e36;
            border-radius: 0.75rem;
            font-family: 'JetBrains Mono', monospace;
            color: #d1d5db;
        }

        .terminal-line {
            margin-bottom: 0.25rem;
            border-left: 2px solid transparent;
            padding-left: 0.4rem;
        }

        .terminal-info { border-color: var(--crt-color); }
        .terminal-warn { border-color: #e6a100; color: #fef08a; }
        .terminal-error { border-color: #f23d3d; color: #fca5a5; }

        body.theme-modern .terminal-info { border-color: var(--modern-color-blue); }
        body.theme-modern .terminal-warn { border-color: var(--modern-color-gold); color: #fef08a; }
        body.theme-modern .terminal-error { border-color: var(--modern-color-crimson); color: #fca5a5; }

        /* Holdings Cards list */
        .positions-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .position-row {
            background: rgba(0, 0, 0, 0.3);
            border: 1px dashed var(--crt-border);
            border-radius: 2px;
            padding: 0.85rem 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        body.theme-modern .position-row {
            background: var(--modern-bg-surface);
            border: 1px solid var(--modern-border-subtle);
            border-radius: 0.75rem;
        }

        .position-symbol-side {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
        }

        .pos-sym {
            font-family: var(--font-pixel);
            font-weight: 700;
            font-size: 1.25rem;
            color: var(--crt-color);
            text-shadow: var(--crt-text-glow);
        }

        body.theme-modern .pos-sym {
            font-family: 'Outfit', sans-serif;
            font-size: 1rem;
            text-shadow: none;
            color: var(--modern-text-primary);
        }

        .pos-qty {
            font-size: 0.75rem;
            color: var(--crt-color-dim);
        }

        body.theme-modern .pos-qty {
            color: var(--modern-text-secondary);
        }

        .position-value-pnl {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 0.15rem;
        }

        .pos-val {
            font-family: var(--font-data);
            font-weight: 500;
            font-size: 0.95rem;
        }

        body.theme-modern .pos-val {
            font-family: 'JetBrains Mono', monospace;
        }

        .pos-pnl {
            font-size: 0.725rem;
            font-weight: 600;
        }

        /* Chart Frame */
        .chart-box {
            background: rgba(0, 0, 0, 0.45);
            border: 2px dashed var(--crt-border);
            border-radius: 2px;
            padding: 1rem;
            height: 250px;
            width: 100%;
        }

        body.theme-modern .chart-box {
            background: var(--modern-bg-surface);
            border: 1px solid var(--modern-border-subtle);
            border-radius: 0.75rem;
        }

        /* Animations */
        @keyframes blink {
            0% { opacity: 0.35; }
            50% { opacity: 1; }
            100% { opacity: 0.35; }
        }

        @keyframes scaleInQAMax {
            from { transform: scale(0.97); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }

        /* Maximize Overlay Windows */
        #qa-analyst-panel.maximized,
        #equity-curve-panel.maximized,
        #executed-orders-panel.maximized {
            position: fixed;
            top: 4vh;
            left: 6vw;
            width: 88vw;
            height: 92vh;
            z-index: 99999;
            background: var(--crt-surface-elevated);
            border: 2px solid var(--crt-color);
            box-shadow: 0 0 30px var(--crt-glow);
            border-radius: 4px;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            animation: scaleInQAMax 0.2s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }

        body.theme-modern #qa-analyst-panel.maximized,
        body.theme-modern #equity-curve-panel.maximized,
        body.theme-modern #executed-orders-panel.maximized {
            background: rgba(10, 13, 22, 0.96);
            backdrop-filter: blur(25px);
            border: 1.5px solid var(--modern-color-teal);
            box-shadow: 0 10px 50px rgba(0, 242, 254, 0.25);
            border-radius: 1.25rem;
        }

        #qa-analyst-panel.maximized #chat-log {
            flex: 1 !important;
            height: auto !important;
            background: rgba(0, 0, 0, 0.6) !important;
        }

        #equity-curve-panel.maximized #chart-workarea-wrapper {
            display: flex;
            gap: 1.75rem;
            flex: 1;
            min-height: 0;
        }

        #chart-workarea-wrapper {
            display: block;
            width: 100%;
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
            background: rgba(0, 0, 0, 0.4);
            padding: 1.25rem;
            border-radius: 2px;
            border: 1px dashed var(--crt-border);
        }

        body.theme-modern #equity-curve-panel.maximized #chart-right-pane {
            background: rgba(5, 7, 15, 0.4);
            border-radius: 0.75rem;
            border: 1px solid var(--modern-border-subtle);
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

        #executed-orders-panel.maximized .trades-table-container {
            flex: 1 !important;
            overflow-y: auto !important;
            min-height: 0;
        }

        /* Buttons & Inputs */
        button, select, input {
            outline: none;
            transition: all 0.2s ease;
        }

        button:not(.theme-knob-btn) {
            cursor: pointer;
            font-family: var(--font-pixel);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        input[type="text"] {
            font-family: var(--font-data);
            background: rgba(0, 0, 0, 0.5) !important;
            border: 2px solid var(--crt-border) !important;
            color: var(--crt-color) !important;
            border-radius: 2px !important;
            padding: 0.5rem 0.75rem;
        }
        
        body.theme-modern input[type="text"] {
            font-family: var(--font-data);
            background: var(--modern-bg-surface-elevated) !important;
            border: 1px solid var(--modern-border-subtle) !important;
            color: var(--modern-text-primary) !important;
            border-radius: 0.5rem !important;
        }

        input[type="text"]:focus {
            border-color: var(--crt-color) !important;
            box-shadow: 0 0 10px var(--crt-glow) !important;
        }

        body.theme-modern input[type="text"]:focus {
            border-color: var(--modern-color-teal) !important;
            box-shadow: none !important;
        }

        select {
            background: rgba(0, 0, 0, 0.5) !important;
            border: 2px solid var(--crt-border) !important;
            color: var(--crt-color) !important;
            border-radius: 2px !important;
            font-family: var(--font-pixel) !important;
            letter-spacing: 0.05em;
        }

        body.theme-modern select {
            background: rgba(255, 255, 255, 0.03) !important;
            border: 1px solid var(--modern-border-subtle) !important;
            color: var(--modern-text-primary) !important;
            border-radius: 0.25rem !important;
            font-family: var(--font-data) !important;
        }

        select option {
            background: var(--crt-surface-elevated) !important;
            color: var(--crt-color) !important;
        }

        body.theme-modern select option {
            background: #101424 !important;
            color: var(--modern-text-primary) !important;
        }

        .timeframe-btn, .selector-btn {
            background: rgba(0, 0, 0, 0.3) !important;
            border: 2px solid var(--crt-border) !important;
            color: var(--crt-color-dim) !important;
            border-radius: 2px !important;
            font-family: var(--font-pixel) !important;
            font-size: 0.75rem !important;
            padding: 0.25rem 0.5rem !important;
        }

        .timeframe-btn.active, .selector-btn.active {
            background: rgba(var(--crt-color-rgb), 0.1) !important;
            border-color: var(--crt-color) !important;
            color: var(--crt-color) !important;
            box-shadow: var(--crt-text-glow) !important;
        }

        body.theme-modern .timeframe-btn, body.theme-modern .selector-btn {
            background: rgba(255, 255, 255, 0.03) !important;
            border: 1px solid var(--modern-border-subtle) !important;
            color: var(--modern-text-secondary) !important;
            border-radius: 0.25rem !important;
            font-family: var(--font-data) !important;
            font-size: 0.7rem !important;
        }

        body.theme-modern .timeframe-btn.active, body.theme-modern .selector-btn.active {
            background: rgba(0, 242, 254, 0.1) !important;
            border-color: var(--modern-color-teal) !important;
            color: var(--modern-color-teal) !important;
            box-shadow: 0 0 10px rgba(0, 242, 254, 0.2) !important;
        }

        /* ==========================================================================
           COMPREHENSIVE MOBILE & TABLET RESPONSIVENESS OVERRIDES (iOS & Android)
           ========================================================================== */
        
        /* 1. Global Spacing & Layout Scaling */
        @media (max-width: 1200px) {
            main {
                max-width: 100%;
                gap: 1.5rem;
                padding: 1.5rem;
            }
        }

        @media (max-width: 991px) {
            main {
                grid-template-columns: 1fr; /* Single column layout */
                gap: 1.25rem;
                padding: 1.25rem;
            }
            .metrics-bar {
                gap: 1rem;
            }
        }

        @media (max-width: 768px) {
            main {
                gap: 1rem;
                padding: 1rem;
            }
            .metrics-bar {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 0.75rem;
            }
        }

        @media (max-width: 480px) {
            main {
                gap: 0.75rem;
                padding: 0.5rem;
            }
            .metrics-bar {
                gap: 0.5rem;
            }
            .card-panel {
                padding: 1rem !important;
                gap: 1rem !important;
            }
        }

        /* 2. Responsive Premium Header */
        @media (max-width: 991px) {
            header {
                padding: 1rem 1.5rem !important;
            }
        }

        @media (max-width: 768px) {
            header {
                flex-direction: column !important;
                align-items: stretch !important;
                gap: 0.75rem !important;
                padding: 0.85rem 1rem !important;
            }
            .brand-container {
                justify-content: space-between;
                width: 100%;
            }
            .header-controls {
                width: 100%;
                justify-content: space-between !important;
                flex-wrap: wrap !important;
                gap: 0.5rem !important;
            }
        }

        @media (max-width: 576px) {
            .brand-title {
                font-size: 1.25rem !important;
            }
            .brand-badge {
                font-size: 0.75rem !important;
                padding: 0.15rem 0.4rem !important;
            }
            .theme-knob-container {
                width: 100% !important;
                justify-content: space-between !important;
                padding: 0.35rem 0.5rem !important;
            }
            .theme-knob-container span {
                display: none !important; /* Hide 'PHOSPHOR SELECTOR:' text to save space */
            }
            .theme-knob-btn {
                flex: 1;
                text-align: center;
                font-size: 0.7rem !important;
                padding: 0.25rem 0.35rem !important;
            }
            #crt-power-btn, .system-status {
                flex: 1;
                font-size: 0.75rem !important;
                padding: 0.35rem 0.5rem !important;
                justify-content: center !important;
                text-align: center !important;
            }
        }

        @media (max-width: 375px) {
            .header-controls {
                flex-direction: column !important;
                align-items: stretch !important;
            }
            #crt-power-btn, .system-status {
                width: 100% !important;
            }
        }

        /* 3. Metrics Cards Auto-Scaling */
        @media (max-width: 768px) {
            .metric-card {
                padding: 1rem !important;
            }
            .metric-value {
                font-size: 1.65rem !important;
            }
            .metric-label {
                font-size: 0.7rem !important;
            }
            .metric-sub {
                font-size: 0.65rem !important;
            }
        }

        @media (max-width: 480px) {
            .metric-card {
                padding: 0.75rem !important;
            }
            .metric-value {
                font-size: 1.35rem !important;
            }
            .metric-label {
                font-size: 0.65rem !important;
                gap: 0.25rem !important;
            }
            .metric-label i {
                width: 0.75rem !important;
                height: 0.75rem !important;
            }
        }

        /* 4. Responsive Panel Headers & Filters */
        @media (max-width: 768px) {
            .panel-header {
                flex-direction: column !important;
                align-items: stretch !important;
                gap: 0.65rem !important;
            }
            /* Remove hardcoded left/right margins from inline styles on controls */
            .panel-header > div,
            .panel-header #chart-timeframe-selector,
            .panel-header #chart-ticker-selector-container,
            .panel-header #orders-filter-container,
            .panel-header [style*="margin-left"] {
                margin-left: 0 !important;
                margin-right: 0 !important;
                width: 100% !important;
                justify-content: space-between !important;
                display: flex !important;
            }
            /* Styling selectors nicer on mobile */
            #chart-ticker-select, #orders-ticker-select {
                flex: 1;
                max-width: 200px;
                padding: 0.35rem 0.5rem !important;
            }
            .timeframe-btn {
                flex: 1;
                text-align: center;
                padding: 0.35rem 0.25rem !important;
            }
            .panel-header div[style*="margin-left: auto"] {
                justify-content: flex-end !important;
                gap: 0.5rem !important;
            }
        }

        /* 5. Immersive Full-Screen Maximized Overlays on Mobile */
        @media (max-width: 768px) {
            #qa-analyst-panel.maximized,
            #equity-curve-panel.maximized,
            #executed-orders-panel.maximized {
                top: 0 !important;
                left: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                border-radius: 0 !important;
                padding: 1rem !important;
                margin: 0 !important;
                z-index: 1000000 !important;
            }

            /* Adjust maximized chart panel split to vertical layout */
            #equity-curve-panel.maximized #chart-workarea-wrapper {
                flex-direction: column !important;
                overflow-y: auto !important;
                display: flex !important;
                gap: 1rem !important;
            }
            #equity-curve-panel.maximized #chart-left-pane,
            #equity-curve-panel.maximized #chart-right-pane {
                width: 100% !important;
                height: auto !important;
            }
            #equity-curve-panel.maximized .chart-box {
                height: 230px !important;
                flex: none !important;
            }
            #equity-curve-panel.maximized #chart-right-pane {
                padding: 0.75rem !important;
                max-height: none !important;
                overflow: visible !important;
            }
        }

        /* 6. High-Performance Mobile Tables */
        .trades-table-container {
            -webkit-overflow-scrolling: touch; /* Kinetic scrolling on iOS */
        }
        @media (max-width: 768px) {
            .trades-table th, .trades-table td {
                padding: 0.5rem 0.6rem !important;
                font-size: 0.75rem !important;
            }
            /* Hide non-critical columns on extremely small devices to reduce horizontal scroll scrollbars */
            @media (max-width: 576px) {
                .trades-table th:nth-child(6), .trades-table td:nth-child(6), /* Hide Order ID column */
                .trades-table th:nth-child(7), .trades-table td:nth-child(7) {
                    display: none !important;
                }
            }
        }

        /* 7. Copilot Q&A & Logs Optimization */
        @media (max-width: 768px) {
            .thought-card {
                padding: 0.85rem !important;
            }
            .thought-text {
                padding: 0.6rem !important;
                font-size: 0.8rem !important;
                line-height: 1.4 !important;
            }
            .confluence-box {
                flex-wrap: wrap !important;
                gap: 0.4rem !important;
            }
            .terminal-pane {
                height: 250px !important;
                font-size: 0.7rem !important;
            }
            #chat-log {
                height: 220px !important;
            }
            #chat-input {
                font-size: 0.8rem !important;
                padding: 0.4rem 0.6rem !important;
            }
            #chat-send-btn {
                width: 2rem !important;
                height: 2rem !important;
            }
            /* Active Watchlist Grid */
            #screener-pool-container {
                max-height: 100px !important;
                padding: 0.35rem !important;
            }
            #screener-pool-container span {
                font-size: 0.65rem !important;
                padding: 0.15rem 0.35rem !important;
            }
        }
    </style>
</head>
<body class="theme-green">
    <!-- Physical CRT monitor bezel casing wrappers -->
    <div id="crt-frame">
        <div id="crt-screen-overlay">
            <div id="scanlines"></div>
            <div id="noise"></div>
            <div id="vignette"></div>
        </div>

        <header>
            <div class="brand-container">
                <div class="brand-badge" style="border: 2px solid var(--crt-color); padding: 0.2rem 0.6rem; font-weight: bold; background: var(--crt-surface-elevated); box-shadow: var(--crt-text-glow); border-radius: 2px; font-family: var(--font-pixel);">
                    [AGNT-TRD]
                </div>
                <h1 class="brand-title" style="font-family: var(--font-pixel); font-size: 1.6rem; color: var(--crt-color); text-shadow: var(--crt-text-glow); text-transform: uppercase;">agenttrade.us</h1>
            </div>
            
            <div class="header-controls" style="display: flex; align-items: center; gap: 1rem;">
                <!-- Phosphor Theme Selector Segmented Control -->
                <div class="theme-knob-container" style="display: flex; align-items: center; gap: 0.5rem; background: rgba(0, 0, 0, 0.4); border: 1px solid var(--crt-border); padding: 0.25rem 0.5rem; border-radius: 4px; font-family: var(--font-pixel); font-size: 0.85rem;">
                    <span style="color: var(--crt-color); text-shadow: var(--crt-text-glow); text-transform: uppercase; margin-right: 0.25rem;">PHOSPHOR SELECTOR:</span>
                    <button class="theme-knob-btn active" data-theme="theme-green" onclick="applyTheme('theme-green')" style="background: transparent; border: 1px solid var(--crt-color); color: var(--crt-color); padding: 0.15rem 0.4rem; cursor: pointer; border-radius: 2px; font-family: var(--font-pixel);">P-1 GREEN</button>
                    <button class="theme-knob-btn" data-theme="theme-amber" onclick="applyTheme('theme-amber')" style="background: transparent; border: 1px solid var(--crt-color); color: var(--crt-color); padding: 0.15rem 0.4rem; cursor: pointer; border-radius: 2px; font-family: var(--font-pixel);">P-3 AMBER</button>
                    <button class="theme-knob-btn" data-theme="theme-cyan" onclick="applyTheme('theme-cyan')" style="background: transparent; border: 1px solid var(--crt-color); color: var(--crt-color); padding: 0.15rem 0.4rem; cursor: pointer; border-radius: 2px; font-family: var(--font-pixel);">P-4 CYAN</button>
                    <button class="theme-knob-btn" data-theme="theme-modern" onclick="applyTheme('theme-modern')" style="background: transparent; border: 1px solid var(--crt-color); color: var(--crt-color); padding: 0.15rem 0.4rem; cursor: pointer; border-radius: 2px; font-family: var(--font-pixel);">NEO-DARK</button>
                </div>
                
                <!-- CRT Screen Accessibility Power Toggle -->
                <button id="crt-power-btn" onclick="toggleCrtEffects()" style="background: var(--crt-surface-elevated); border: 2px solid var(--crt-color); color: var(--crt-color); font-family: var(--font-pixel); font-size: 0.85rem; padding: 0.25rem 0.75rem; border-radius: 4px; cursor: pointer; box-shadow: var(--crt-text-glow); font-weight: bold; transition: all 0.2s;">
                    CRT EFFECTS: ON
                </button>

                <!-- Enhanced System Status Badges -->
                <div class="system-status" style="display: flex; gap: 0.5rem;">
                    <div class="status-badge" style="background: rgba(51, 255, 51, 0.1); border: 1px solid var(--crt-color); border-radius: 4px; padding: 0.25rem 0.5rem; display: flex; align-items: center; gap: 0.25rem;">
                        <div class="status-dot" id="status-dot" style="background-color: var(--crt-color);"></div>
                        <span id="system-status-text">SIMULATION LIVE</span>
                    </div>
                    <div class="status-badge" id="kill-switch-badge" style="background: rgba(255, 8, 68, 0.1); border: 1px solid rgba(255, 8, 68, 0.3); border-radius: 4px; padding: 0.25rem 0.5rem; display: flex; align-items: center; gap: 0.25rem;">
                        <div class="status-dot" style="background-color: rgba(255, 8, 68, 0.8);"></div>
                        <span>KILL SWITCH: ACTIVE</span>
                    </div>
                    <div class="status-badge" id="weekend-skip-badge" style="background: rgba(230, 161, 0, 0.1); border: 1px solid rgba(230, 161, 0, 0.3); border-radius: 4px; padding: 0.25rem 0.5rem; display: flex; align-items: center; gap: 0.25rem;">
                        <div class="status-dot" style="background-color: rgba(230, 161, 0, 0.8);"></div>
                        <span>WEEKEND SKIP: OFF</span>
                    </div>
                    <div class="status-badge" id="db-sync-badge" style="background: rgba(0, 242, 254, 0.1); border: 1px solid rgba(0, 242, 254, 0.3); border-radius: 4px; padding: 0.25rem 0.5rem; display: flex; align-items: center; gap: 0.25rem;">
                        <div class="status-dot" style="background-color: rgba(0, 242, 254, 0.8);"></div>
                        <span>DB SYNC: ACTIVE</span>
                    </div>
                </div>
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
            <div class="metric-card" id="options-metric-card">
                <div class="metric-label">
                    <i data-lucide="activity" style="width: 0.85rem; height: 0.85rem; color: #c084fc"></i>
                    Options Exposure
                </div>
                <div class="metric-value" id="val-options">0 contracts</div>
                <div class="metric-sub" id="sub-options">Active Option Positions</div>
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
                        <button class="timeframe-btn" onclick="changeChartTimeframe('1D')">1D</button>
                        <button class="timeframe-btn" onclick="changeChartTimeframe('5D')">5D</button>
                        <button class="timeframe-btn" onclick="changeChartTimeframe('1M')">1M</button>
                        <button class="timeframe-btn active" onclick="changeChartTimeframe('ALL')">ALL</button>
                    </div>
                    <div id="chart-ticker-selector-container" style="display: flex; align-items: center; gap: 0.35rem; margin-left: 1.5rem;">
                        <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; letter-spacing: 0.05em; font-family: var(--font-pixel);">Ticker:</span>
                        <select id="chart-ticker-select" onchange="filterChartByTicker()" style="padding: 0.25rem 0.5rem; font-size: 0.7rem; font-weight: 600; outline: none; cursor: pointer;">
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
                            <button class="selector-btn active" onclick="changeChartMetric('equity')">Equity ($)</button>
                            <button class="selector-btn" onclick="changeChartMetric('pnl')">Unrealized PnL ($)</button>
                            <button class="selector-btn" onclick="changeChartMetric('cash')">Cash Reserves ($)</button>
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
                        Platform Parameters & Screener Pool
                    </h3>
                </div>
                <div style="background: rgba(10, 13, 22, 0.4); padding: 1rem; border-radius: 0.75rem; border: 1px solid var(--border-subtle);">
                    <div style="margin-bottom: 0.8rem;">
                        <div style="display: flex; align-items: center; margin-bottom: 0.5rem;">
                            <span style="color: var(--text-secondary); font-weight: 600;">Screener Universe</span>
                        </div>
                        
                        <!-- Color-coded Legend with counts -->
                        <div style="display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 0.5rem; font-size: 0.7rem;">
                            <span style="display: flex; align-items: center; gap: 0.25rem;">
                                <span style="width: 8px; height: 8px; border-radius: 50%; background: var(--color-green); display: inline-block; box-shadow: 0 0 5px var(--color-green);"></span>
                                <span style="color: var(--color-green); font-weight: bold;">Holding: <span id="stat-holdings">0</span></span>
                            </span>
                            <span style="display: flex; align-items: center; gap: 0.25rem;">
                                <span style="width: 8px; height: 8px; border-radius: 50%; background: var(--color-blue); display: inline-block; box-shadow: 0 0 5px var(--color-blue);"></span>
                                <span style="color: var(--color-blue); font-weight: bold;">Watchlist: <span id="stat-watchlist">0</span></span>
                            </span>
                            <span style="display: flex; align-items: center; gap: 0.25rem;">
                                <span style="width: 8px; height: 8px; border-radius: 50%; background: var(--color-gold); display: inline-block; box-shadow: 0 0 5px var(--color-gold);"></span>
                                <span style="color: var(--color-gold); font-weight: bold;">Screener Pool: <span id="stat-pool">0</span></span>
                            </span>
                        </div>
                        
                        <!-- Scrollable Grid -->
                        <div id="screener-pool-container" style="display: flex; flex-wrap: wrap; gap: 0.35rem; max-height: 120px; overflow-y: auto; padding: 0.5rem; background: rgba(0,0,0,0.25); border-radius: 0.5rem; border: 1px solid rgba(255,255,255,0.05); font-family: 'JetBrains Mono', monospace;">
                            <span style="color: var(--text-muted); font-size: 0.75rem;">Loading screener pool...</span>
                        </div>
                    </div>

                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.4rem; padding-top: 0.4rem; border-top: 1px solid rgba(255,255,255,0.05);">
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
            const hasOffset = /([+-]\\d{2}:?\\d{2})$/.test(str) || /[Zz]$/.test(str);
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

            // Dynamically plot based on activeChartMetric and active theme
            let label = 'Portfolio Value ($)';
            let borderColor = '#00f2fe';
            let backgroundColor = 'rgba(0, 242, 254, 0.05)';
            let data = [];

            // Read colors dynamically from CSS variables
            const isModern = document.body.classList.contains('theme-modern');
            const bodyStyle = getComputedStyle(document.body);
            
            let crtColor = bodyStyle.getPropertyValue('--crt-color').trim() || '#33FF33';
            let crtColorRgb = bodyStyle.getPropertyValue('--crt-color-rgb').trim() || '51, 255, 51';
            let crtGlow = bodyStyle.getPropertyValue('--crt-glow').trim() || 'rgba(51, 255, 51, 0.12)';

            if (activeChartMetric === 'equity') {
                label = activeChartTicker === 'ALL' ? 'Portfolio Value ($)' : `${activeChartTicker} Position Value ($)`;
                if (isModern) {
                    borderColor = '#00f2fe';
                    backgroundColor = 'rgba(0, 242, 254, 0.05)';
                } else {
                    borderColor = crtColor;
                    backgroundColor = crtGlow;
                }
                data = chartHistory.map(item => item.equity !== undefined ? item.equity : 100000);
            } else if (activeChartMetric === 'pnl') {
                label = activeChartTicker === 'ALL' ? 'Unrealized PnL ($)' : `${activeChartTicker} Unrealized PnL ($)`;
                if (isModern) {
                    borderColor = '#10b981';
                    backgroundColor = 'rgba(16, 185, 129, 0.05)';
                } else {
                    borderColor = crtColor;
                    backgroundColor = crtGlow;
                }
                data = chartHistory.map(item => item.unrealized_pnl !== undefined ? item.unrealized_pnl : 0);
            } else if (activeChartMetric === 'cash') {
                label = 'Cash Reserves ($)';
                if (isModern) {
                    borderColor = '#0070f3';
                    backgroundColor = 'rgba(0, 112, 243, 0.05)';
                } else {
                    borderColor = crtColor;
                    backgroundColor = crtGlow;
                }
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

            // Daily-resampled diagnostics: resample raw intraday history to one point per
            // trading day (last observed equity for each date) so Win Rate, Sharpe, and
            // Profit Factor are computed over equal-interval daily returns instead of
            // mixed-sampling intraday noise.
            function resampleToDaily(history) {
                const dayMap = new Map();
                for (const item of history) {
                    const d = parseUtcTimestamp(item.timestamp);
                    if (!d) continue;
                    const dayKey = d.toLocaleDateString('en-CA', {timeZone: 'America/New_York'}); // YYYY-MM-DD
                    // Always keep the latest record per day
                    dayMap.set(dayKey, item);
                }
                return Array.from(dayMap.values());
            }

            const dailyHistory = resampleToDaily(chartHistory);

            // Mathematical performance diagnostics engine (TMCL-405)
            let totalReturn = 0;
            let maxDrawdown = 0;
            let winRate = 0;
            let sharpeRatio = 0;
            let profitFactor = 1.00;

            if (chartHistory.length > 1) {
                const firstEq = chartHistory[0].equity || 100000;
                const lastEq = chartHistory[chartHistory.length - 1].equity || 100000;

                // 1. Total Net Return (full data is fine – start-to-end)
                if (firstEq > 0) {
                    totalReturn = ((lastEq - firstEq) / firstEq) * 100;
                }

                // 2. Peak Drawdown (full data is fine – peak/trough independent of rate)
                let peak = -Infinity;
                chartHistory.forEach(item => {
                    const eq = item.equity !== undefined ? item.equity : 100000;
                    if (eq > peak) peak = eq;
                    if (peak > 0) {
                        const dd = ((peak - eq) / peak) * 100;
                        if (dd > maxDrawdown) maxDrawdown = dd;
                    }
                });

                // ---- Daily-resampled stats below (Win Rate, Sharpe, Profit Factor) ----
                const useDaily = dailyHistory.length > 1;
                const perfData = useDaily ? dailyHistory : chartHistory;

                // 3. Daily Win Rate (% of up days)
                let wins = 0;
                const steps = perfData.length - 1;
                for (let i = 1; i < perfData.length; i++) {
                    const prev = perfData[i-1].equity !== undefined ? perfData[i-1].equity : 100000;
                    const curr = perfData[i].equity !== undefined ? perfData[i].equity : 100000;
                    if (curr > prev) wins++;
                }
                if (steps > 0) winRate = (wins / steps) * 100;

                // 4. Daily Sharpe Ratio (Annualized over ~252 trading days)
                const dailyReturns = [];
                for (let i = 1; i < perfData.length; i++) {
                    const prev = perfData[i-1].equity !== undefined ? perfData[i-1].equity : 100000;
                    const curr = perfData[i].equity !== undefined ? perfData[i].equity : 100000;
                    if (prev > 0) dailyReturns.push((curr - prev) / prev);
                }
                if (dailyReturns.length > 0) {
                    const sum = dailyReturns.reduce((a, b) => a + b, 0);
                    const mean = sum / dailyReturns.length;
                    const variance = dailyReturns.reduce((acc, val) => acc + Math.pow(val - mean, 2), 0) / dailyReturns.length;
                    const std = Math.sqrt(variance);
                    if (std > 0) sharpeRatio = (mean / std) * Math.sqrt(252);
                }

                // 5. Profit Factor (gross gains / gross losses on daily returns)
                let grossGains = 0;
                let grossLosses = 0;
                for (let i = 1; i < perfData.length; i++) {
                    const prev = perfData[i-1].equity !== undefined ? perfData[i-1].equity : 100000;
                    const curr = perfData[i].equity !== undefined ? perfData[i].equity : 100000;
                    const diff = curr - prev;
                    if (diff > 0) grossGains += diff;
                    else if (diff < 0) grossLosses += Math.abs(diff);
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
                            grid: { 
                                display: !isModern,
                                color: isModern ? 'rgba(255, 255, 255, 0.03)' : `rgba(${crtColorRgb}, 0.08)`,
                                borderDash: [2, 2]
                            },
                            ticks: { 
                                color: isModern ? '#6b7280' : crtColor, 
                                font: { 
                                    size: 10,
                                    family: isModern ? "'Inter', sans-serif" : "'Share Tech Mono', monospace"
                                },
                                maxTicksLimit: 8,
                                autoSkip: true
                            }
                        },
                        y: {
                            grid: { 
                                color: isModern ? 'rgba(255, 255, 255, 0.03)' : `rgba(${crtColorRgb}, 0.08)`,
                                borderDash: [2, 2]
                            },
                            ticks: { 
                                color: isModern ? '#6b7280' : crtColor, 
                                font: { 
                                    size: 10,
                                    family: isModern ? "'Inter', sans-serif" : "'Share Tech Mono', monospace"
                                } 
                            }
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
                    if (data.kill_switch === "HALTED") {
                        statusText.innerText = "SYSTEM HALTED (GCS KILL SWITCH)";
                        statusText.style.color = "var(--color-crimson)";
                        statusDot.style.backgroundColor = "var(--color-crimson)";
                        statusDot.style.boxShadow = "0 0 12px var(--color-crimson)";
                    } else if (data.freshness && data.freshness.state === "STALE") {
                        statusText.innerText = "RUNNER STALE — HEARTBEAT OVERDUE";
                        statusText.style.color = "var(--color-crimson)";
                        statusDot.style.backgroundColor = "var(--color-crimson)";
                        statusDot.style.boxShadow = "0 0 12px var(--color-crimson)";
                    } else if (data.freshness && data.freshness.state === "FAILED") {
                        statusText.innerText = "RUNNER FAILED — CHECK CLOUD LOGS";
                        statusText.style.color = "var(--color-crimson)";
                        statusDot.style.backgroundColor = "var(--color-crimson)";
                        statusDot.style.boxShadow = "0 0 12px var(--color-crimson)";
                    } else if (data.freshness && data.freshness.state === "RUNNING") {
                        statusText.innerText = "TRADING CYCLE RUNNING";
                        statusText.style.color = "var(--color-gold)";
                        statusDot.style.backgroundColor = "var(--color-gold)";
                        statusDot.style.boxShadow = "0 0 10px var(--color-gold)";
                    } else if (data.freshness && data.freshness.state === "CRYPTO_ONLY_MONITORING") {
                        const heartbeatTime = data.heartbeat && data.heartbeat.completed_at
                            ? formatToEastern(data.heartbeat.completed_at)
                            : 'unknown';
                        statusText.innerText = `CRYPTO-ONLY MONITORING — HEARTBEAT ${heartbeatTime}`;
                        statusText.style.color = "var(--color-gold)";
                        statusDot.style.backgroundColor = "var(--color-green)";
                        statusDot.style.boxShadow = "0 0 10px var(--color-green)";
                    } else if (data.is_paper) {
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

                // Options Exposure metric card
                const positionsDataForOpt = (data && data.positions) || {};
                let optContracts = 0;
                let optPositions = 0;
                Object.keys(positionsDataForOpt).forEach(sym => {
                    const p = positionsDataForOpt[sym] || {};
                    if (p.is_option) {
                        optContracts += (p.qty || 0);
                        optPositions += 1;
                    }
                });
                const valOptionsEl = document.getElementById('val-options');
                if (valOptionsEl) {
                    valOptionsEl.innerText = optContracts + ' contract' + (optContracts === 1 ? '' : 's');
                    valOptionsEl.className = 'metric-value ' + (optPositions > 0 ? 'text-gold' : '');
                }
                const subOptionsEl = document.getElementById('sub-options');
                if (subOptionsEl) {
                    subOptionsEl.innerText = optPositions > 0 ? (optPositions + ' active option position' + (optPositions === 1 ? '' : 's')) : 'No active option positions';
                }
                // Update Screener Pool Grid
                const screenerPoolContainer = document.getElementById('screener-pool-container');
                if (screenerPoolContainer) {
                    screenerPoolContainer.innerHTML = '';
                    const screenerPool = (data && data.screener_pool) || [];
                    const latestWatchlist = (data && data.latest_watchlist) || [];
                    const positionsData = (data && data.positions) || {};
                    const holdings = Object.keys(positionsData).map(sym => sym.toUpperCase());

                    // Update screener stats: pool, watchlist, holdings
                    const statPool = document.getElementById('stat-pool');
                    const statWatchlist = document.getElementById('stat-watchlist');
                    const statHoldings = document.getElementById('stat-holdings');
                    if (statPool) statPool.innerText = screenerPool.length;
                    if (statWatchlist) statWatchlist.innerText = latestWatchlist.length;
                    if (statHoldings) statHoldings.innerText = holdings.length;

                    if (screenerPool.length === 0) {
                        screenerPoolContainer.innerHTML = '<span style="color: var(--text-muted); font-size: 0.75rem;">No screener pool loaded.</span>';
                    } else {
                        // Sort pool so holdings and watchlisted show first, then alphabetical
                        const sortedPool = [...screenerPool].sort((a, b) => {
                            const aUpper = a.toUpperCase();
                            const bUpper = b.toUpperCase();
                            
                            const isAHolding = holdings.some(h => h.replace('/', '') === aUpper.replace('/', ''));
                            const isBHolding = holdings.some(h => h.replace('/', '') === bUpper.replace('/', ''));
                            
                            const isAWatching = latestWatchlist.some(w => w.toUpperCase().replace('/', '') === aUpper.replace('/', ''));
                            const isBWatching = latestWatchlist.some(w => w.toUpperCase().replace('/', '') === bUpper.replace('/', ''));

                            if (isAHolding && !isBHolding) return -1;
                            if (!isAHolding && isBHolding) return 1;
                            if (isAWatching && !isBWatching) return -1;
                            if (!isAWatching && isBWatching) return 1;
                            return a.localeCompare(b);
                        });

                        sortedPool.forEach(symbol => {
                            const symUpper = symbol.toUpperCase();
                            const isHolding = holdings.some(h => h.replace('/', '') === symUpper.replace('/', ''));
                            const isWatching = latestWatchlist.some(w => w.toUpperCase().replace('/', '') === symUpper.replace('/', ''));

                            let badgeStyle = "padding: 0.15rem 0.35rem; border-radius: 0.25rem; font-size: 0.7rem; font-weight: bold; border: 1px solid; transition: all 0.2s;";
                            if (isHolding) {
                                badgeStyle += " border-color: var(--color-green); background: rgba(51, 255, 51, 0.08); color: var(--color-green); box-shadow: 0 0 4px var(--crt-glow);";
                            } else if (isWatching) {
                                badgeStyle += " border-color: var(--color-blue); background: rgba(0, 255, 255, 0.08); color: var(--color-blue); box-shadow: 0 0 4px rgba(0, 255, 255, 0.08);";
                            } else {
                                badgeStyle += " border-color: var(--color-gold); background: rgba(255, 176, 0, 0.02); color: var(--color-gold); opacity: 0.65;";
                            }

                            screenerPoolContainer.innerHTML += `<span style="${badgeStyle}" title="${isHolding ? 'Holding' : (isWatching ? 'Watchlist' : 'Screener Pool')}">${symbol}</span>`;
                        });
                    }
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
                            const isOption = !!(pos.is_option);
                            const unitLabel = isOption ? 'contract(s)' : 'shares';
                            // Option-specific detail (strike / DTE) if present
                            let optionDetail = '';
                            if (isOption) {
                                const strike = pos.strike_price;
                                const dte = pos.option_dte;
                                const parts = [];
                                if (strike) parts.push('Strike $' + Number(strike).toFixed(2));
                                if (dte !== undefined && dte !== null) parts.push('DTE ' + dte + 'd');
                                optionDetail = parts.length ? ' <span class="pos-opt-detail">(' + parts.join(', ') + ')</span>' : '';
                            }
                            const optBadge = isOption ? '<span class="badge" style="background: rgba(139, 92, 246, 0.2); color: #c084fc; margin-left: 0.4rem; text-transform: none;">OPT</span>' : '';
                            
                            posListEl.innerHTML += `
                                <div class="position-row">
                                    <div class="position-symbol-side">
                                        <span class="pos-sym">${sym}</span>
                                        <span class="pos-qty">${qty} ${unitLabel} @ $${avgEntry.toFixed(2)}</span>
                                        ${optBadge}
                                    </div>
                                    <div class="position-value-pnl">
                                        <span class="pos-val">$${marketVal.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                                        <span class="pos-pnl ${pnlClass}">${pnlSign}$${posPnl.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                                        ${optionDetail}
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
                        // Build a lookup of execution attempts keyed by decision_id
                        const executions = (data && data.executions) || [];
                        const execByDecision = {};
                        executions.forEach(ex => {
                            if (ex.decision_id != null) {
                                if (!execByDecision[ex.decision_id]) execByDecision[ex.decision_id] = [];
                                execByDecision[ex.decision_id].push(ex);
                            }
                        });

                        // Build a per-symbol "previous cycle" conviction lookup from
                        // ticker_convictions so we can flag conviction/direction
                        // changes (Δ) vs the prior cycle. Rows are newest-first.
                        const tickerConvictions = (data && data.ticker_convictions) || [];
                        const prevConvictionBySymbol = {};
                        const seenSymbols = {};
                        tickerConvictions.forEach(tc => {
                            const sym = (tc.symbol || '').toUpperCase();
                            if (!sym) return;
                            if (!seenSymbols[sym]) {
                                seenSymbols[sym] = true;
                                prevConvictionBySymbol[sym] = tc;
                            }
                        });

                        decisions.forEach(dec => {
                            const isApproved = dec.is_approved === 1;
                            const statusClass = isApproved ? 'approved' : 'rejected';
                            const dateStr = dec.timestamp ? formatToEastern(dec.timestamp) : 'N/A';
                            
                            const action = dec.proposed_action || 'HOLD';
                            let actionBadgeClass = 'badge-hold';
                            if (action === 'BUY') actionBadgeClass = 'badge-buy';
                            if (action === 'SELL') actionBadgeClass = 'badge-sell';

                            // Reconcile decision against execution attempts
                            const decExecs = execByDecision[dec.id] || [];
                            const hasFilled = decExecs.some(ex => ex.status === 'filled' || ex.status === 'partially_filled');
                            const hasFailed = decExecs.some(ex => ex.status === 'failed');
                            const hasSubmitted = decExecs.some(ex => ex.status === 'submitted');

                            let alertIcon = '';
                            if (!isApproved) {
                                alertIcon = `<span class="badge" style="background: rgba(239, 68, 68, 0.15); color: var(--color-crimson); margin-left: 0.5rem; text-transform: none;">REJECTED BY RISK GUARDRAIL: ${dec.rejection_reason || 'Unknown'}</span>`;
                            } else if (action !== 'HOLD') {
                                if (hasFilled) {
                                    alertIcon = `<span class="badge" style="background: rgba(16, 185, 129, 0.15); color: var(--color-green); margin-left: 0.5rem; text-transform: none;">EXECUTED</span>`;
                                } else if (hasFailed) {
                                    const err = decExecs.find(ex => ex.status === 'failed');
                                    alertIcon = `<span class="badge" style="background: rgba(239, 68, 68, 0.15); color: var(--color-crimson); margin-left: 0.5rem; text-transform: none;">EXECUTION FAILED: ${(err && err.error) || 'Unknown'}</span>`;
                                } else if (hasSubmitted) {
                                    alertIcon = `<span class="badge" style="background: rgba(245, 158, 11, 0.15); color: var(--color-amber); margin-left: 0.5rem; text-transform: none;">ORDER SUBMITTED (PENDING)</span>`;
                                } else {
                                    alertIcon = `<span class="badge" style="background: rgba(245, 158, 11, 0.15); color: var(--color-amber); margin-left: 0.5rem; text-transform: none;">DECIDED - NOT EXECUTED</span>`;
                                }
                            }

                            const symText = dec.proposed_symbol ? dec.proposed_symbol : 'PORTFOLIO HOLD';
                            const qtyText = dec.proposed_qty > 0 ? dec.proposed_qty + ' shares' : '';
                            const thoughtText = dec.thought_process || 'No rationale logged.';
                            // Option-routed decision tag (instrument resolved by guardrails)
                            let optionTag = '';
                            if (dec.instrument === 'option') {
                                const optType = dec.option_type ? (dec.option_type.toUpperCase()) : '';
                                optionTag = `<span class="badge" style="background: rgba(139, 92, 246, 0.2); color: #c084fc; margin-left: 0.5rem; text-transform: none;">OPTION ${optType}</span>`;
                            }
                            // Conviction/direction hint
                            let convictionTag = '';
                            if (dec.conviction !== undefined && dec.conviction !== null) {
                                const dir = dec.direction ? dec.direction.toUpperCase() : '';
                                convictionTag = `<span class="badge" style="background: rgba(59, 130, 246, 0.12); color: var(--text-secondary); margin-left: 0.5rem; text-transform: none;">${dir} ${(dec.conviction * 100).toFixed(0)}%</span>`;
                            }
                            // Conviction-change (Δ) indicator vs the prior cycle
                            let changeTag = '';
                            if (dec.proposed_symbol) {
                                const sym = dec.proposed_symbol.toUpperCase();
                                const prev = prevConvictionBySymbol[sym];
                                if (prev && prev.cycle_id !== dec.cycle_id) {
                                    const prevDir = (prev.direction || '').toUpperCase();
                                    const curDir = (dec.direction || '').toUpperCase();
                                    const prevC = prev.conviction != null ? prev.conviction : null;
                                    const curC = dec.conviction != null ? dec.conviction : null;
                                    const dirChanged = prevDir && curDir && prevDir !== curDir;
                                    const convChanged = prevC != null && curC != null && Math.abs(prevC - curC) >= 0.05;
                                    if (dirChanged || convChanged) {
                                        const delta = (prevC != null && curC != null) ? ((curC - prevC) * 100).toFixed(0) : '';
                                        const arrow = (delta && delta > 0) ? '▲' : (delta && delta < 0) ? '▼' : 'Δ';
                                        changeTag = `<span class="badge" style="background: rgba(250, 204, 21, 0.12); color: #facc15; margin-left: 0.5rem; text-transform: none;" title="Conviction changed vs prior cycle">${arrow}${delta ? ' ' + delta + '%' : ''}</span>`;
                                    }
                                }
                            }

                            streamEl.innerHTML += `
                                <div class="thought-card ${statusClass}">
                                    <div class="thought-header">
                                        <div class="thought-meta">
                                            <div class="thought-ticker">
                                                ${symText} 
                                                <span class="badge ${actionBadgeClass}">${action} ${qtyText}</span>
                                                ${optionTag}
                                                ${convictionTag}
                                                ${changeTag}
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

                // 4. Trades Database List — Merge DB trades + live Alpaca broker orders
                const tradesTbody = document.getElementById('trades-tbody');
                if (tradesTbody) {
                    tradesTbody.innerHTML = '';
                    const dbTrades = (data && data.trades) || [];
                    const brokerOrders = (data && data.broker_orders) || [];
                    
                    // Merge: DB trades first, then broker orders not already in DB
                    const dbOrderIds = new Set();
                    dbTrades.forEach(t => { if (t.alpaca_order_id) dbOrderIds.add(t.alpaca_order_id); });
                    const freshBroker = brokerOrders.filter(o => !dbOrderIds.has(o.alpaca_order_id));
                    const allOrders = [...dbTrades, ...freshBroker];
                    // Sort descending by timestamp (newest first)
                    allOrders.sort((a, b) => {
                        const tsA = a.timestamp || '';
                        const tsB = b.timestamp || '';
                        return tsB.localeCompare(tsA);
                    });

                    if (allOrders.length === 0) {
                        tradesTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No orders registered in database.</td></tr>';
                    } else {
                        allOrders.forEach(t => {
                            const side = t.side || 'buy';
                            const sideClass = side.toLowerCase() === 'buy' ? 'text-green' : 'text-crimson';
                            const dateStr = t.timestamp ? formatToEastern(t.timestamp) : 'N/A';
                            const fillPriceStr = t.filled_avg_price ? '$' + t.filled_avg_price.toFixed(2) : '<span style="color: var(--text-muted)">Unfilled</span>';
                            const symbol = t.symbol || 'N/A';
                            const qty = t.qty !== undefined ? t.qty : 0;
                            const status = t.status || 'filled';
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
                    
                    // Populate unique tickers in the select dropdown dynamically (from all orders)
                    const selectEl = document.getElementById('orders-ticker-select');
                    if (selectEl) {
                        const previousValue = selectEl.value;
                        const uniqueSymbols = new Set();
                        allOrders.forEach(t => {
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

                // 5. System Logs live tail — oldest-first, auto-scrolled to bottom
                const terminalPane = document.getElementById('terminal-pane');
                if (terminalPane) {
                    terminalPane.innerHTML = '';
                    const logs = (data && data.logs) || [];
                    // Data comes oldest-first from backend; render in same order
                    logs.forEach(line => {
                        let levelClass = 'terminal-info';
                        if (line.includes('[WARNING]') || line.includes('[WARN]')) levelClass = 'terminal-warn';
                        if (line.includes('[CRITICAL]') || line.includes('[ERROR]') || line.includes('FATAL')) levelClass = 'terminal-error';
                        
                        const escapedLine = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                        terminalPane.innerHTML += `<div class="terminal-line ${levelClass}">${escapedLine}</div>`;
                    });
                    // Auto-scroll to bottom so newest entry is always visible
                    terminalPane.scrollTop = terminalPane.scrollHeight;
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
                let data = null;
                try {
                    data = await response.json();
                } catch (parseErr) {
                    data = null;
                }
                if (!response.ok) {
                    const serverMsg = (data && data.error) ? data.error : `Chat API responded with ${response.status}`;
                    throw new Error(serverMsg);
                }
                
                // Remove loading indicator
                const loadingEl = document.getElementById(loadingId);
                if (loadingEl) loadingEl.remove();
                
                if (data && data.error) {
                    throw new Error(data.error);
                }
                
                let botResponse = data.response || "No response received.";
                
                // Convert Markdown images: ![alt](url) to beautiful premium responsive img tags with hover-zoom and scroll-to-bottom
                botResponse = botResponse.replace(/!\\[(.*?)\\]\\(((?:\\([^()]*\\)|[^()]+)*)\\)/g, function(match, alt, url) {
                    let processedUrl = url;
                    if (processedUrl.includes('quickchart.io')) {
                        processedUrl = processedUrl.replace(/%/g, '%25').replace(/#/g, '%23').replace(/ /g, '%20');
                    }
                    return '<div class="chat-image-container" style="margin: 12px 0; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.2); box-shadow: 0 4px 20px rgba(0,0,0,0.3); transition: transform 0.3s ease; max-width: 100%;"><img src="' + processedUrl + '" alt="' + alt + '" style="width: 100%; height: auto; display: block; border-radius: 12px; transition: transform 0.3s ease;" onmouseover="this.style.transform=\\\'scale(1.02)\\\'" onmouseout="this.style.transform=\\\'scale(1)\\\'" onload="const l = document.getElementById(\\\'chat-log\\\'); if(l) l.scrollTop = l.scrollHeight;" /></div>';
                });

                // Convert Markdown links: [text](url) to standard styled anchor tags
                botResponse = botResponse.replace(/\\[(.*?)\\]\\(((?:\\([^()]*\\)|[^()]+)*)\\)/g, '<a href="$2" target="_blank" style="color: var(--color-teal); text-decoration: underline;">$1</a>');

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
                });
                
                buttons.forEach(btn => {
                    if (btn.getAttribute('onclick').includes(`'${timeframe}'`)) {
                        btn.classList.add('active');
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
                });
                
                buttons.forEach(btn => {
                    if (btn.getAttribute('onclick').includes(`'${metric}'`)) {
                        btn.classList.add('active');
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
                    .replace(/<br\\s*\\/?>/gi, '\\n')
                    .replace(/<strong>(.*?)<\\/strong>/gi, '**$1**')
                    .replace(/<b>(.*?)<\\/b>/gi, '**$1**')
                    .replace(/<em>(.*?)<\\/em>/gi, '*$1*')
                    .replace(/<i>(.*?)<\\/i>/gi, '*$1*')
                    .replace(/<code[^>]*>(.*?)<\\/code>/gi, '`$1`')
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
        // --- Retro Terminal Theme & CRT Effects Controllers ---
        function applyTheme(themeName) {
            // Remove any active theme classes from body
            document.body.classList.remove('theme-green', 'theme-amber', 'theme-cyan', 'theme-modern');
            
            // Add specified theme class
            document.body.classList.add(themeName);
            
            // Save selection to localStorage
            localStorage.setItem('agenttrade-theme', themeName);
            
            // Update active state on the selector buttons
            const buttons = document.querySelectorAll('.theme-knob-btn');
            buttons.forEach(btn => {
                if (btn.getAttribute('data-theme') === themeName) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
            
            // Redraw chart to pick up new colors
            updateChart(latestHistoryCached);
        }

        function toggleCrtEffects() {
            const isCrtOn = !document.body.classList.contains('crt-off');
            const btn = document.getElementById('crt-power-btn');
            
            if (isCrtOn) {
                document.body.classList.add('crt-off');
                localStorage.setItem('agenttrade-crt-on', 'false');
                if (btn) btn.innerText = 'CRT EFFECTS: OFF';
            } else {
                document.body.classList.remove('crt-off');
                localStorage.setItem('agenttrade-crt-on', 'true');
                if (btn) btn.innerText = 'CRT EFFECTS: ON';
            }
        }

        function toggleLogSort() {
            // Sort toggle deprecated — logs are now always oldest-first, auto-scrolled to bottom.
        }

        // Initialize user preferences on DOM content loaded or script execution
        function initRetroPreferences() {
            // Restore theme preference (Default is theme-green)
            const savedTheme = localStorage.getItem('agenttrade-theme') || 'theme-green';
            applyTheme(savedTheme);
            
            // Restore CRT effects preference (Default is true / crt-on)
            const savedCrtOn = localStorage.getItem('agenttrade-crt-on');
            const btn = document.getElementById('crt-power-btn');
            if (savedCrtOn === 'false') {
                document.body.classList.add('crt-off');
                if (btn) btn.innerText = 'CRT EFFECTS: OFF';
            } else {
                document.body.classList.remove('crt-off');
                if (btn) btn.innerText = 'CRT EFFECTS: ON';
            }
        }

        // Invoke preferences init
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initRetroPreferences);
        } else {
            initRetroPreferences();
        }
    </script>
    </div> <!-- Close #crt-frame -->
</body>
</html>
"""

import hashlib

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AGE Desk Security Terminal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --crt-color: #33ff33;
            --crt-color-rgb: 51, 255, 51;
            --crt-bg: #030703;
            --crt-surface: rgba(5, 12, 5, 0.9);
            --crt-surface-elevated: rgba(10, 24, 10, 0.95);
            --crt-border: rgba(51, 255, 51, 0.25);
            --crt-glow: rgba(51, 255, 51, 0.12);
            --crt-text-glow: 0 0 4px rgba(51, 255, 51, 0.55), 0 0 8px rgba(51, 255, 51, 0.25);
            --font-data: 'JetBrains Mono', monospace;
        }

        body.theme-amber {
            --crt-color: #ffb000;
            --crt-color-rgb: 255, 176, 0;
            --crt-bg: #060400;
            --crt-surface: rgba(16, 10, 0, 0.9);
            --crt-surface-elevated: rgba(30, 20, 0, 0.95);
            --crt-border: rgba(255, 176, 0, 0.25);
            --crt-glow: rgba(255, 176, 0, 0.12);
            --crt-text-glow: 0 0 4px rgba(255, 176, 0, 0.55), 0 0 8px rgba(255, 176, 0, 0.25);
        }

        body.theme-cyan {
            --crt-color: #00ffff;
            --crt-color-rgb: 0, 255, 255;
            --crt-bg: #000606;
            --crt-surface: rgba(0, 16, 16, 0.9);
            --crt-surface-elevated: rgba(0, 30, 30, 0.95);
            --crt-border: rgba(0, 255, 255, 0.25);
            --crt-glow: rgba(0, 255, 255, 0.12);
            --crt-text-glow: 0 0 4px rgba(0, 255, 255, 0.55), 0 0 8px rgba(0, 255, 255, 0.25);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: var(--font-data);
            background-color: var(--crt-bg);
            color: var(--crt-color);
            overflow: hidden;
            height: 100vh;
            width: 100vw;
            display: flex;
            justify-content: center;
            align-items: center;
        }

        /* Scanlines and CRT effects */
        #crt-frame {
            position: relative;
            width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            background-color: var(--crt-bg);
            overflow: hidden;
        }

        #crt-screen-overlay {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 100;
        }

        #scanlines {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(
                rgba(18, 16, 16, 0) 50%, 
                rgba(0, 0, 0, 0.25) 50%
            ), linear-gradient(
                90deg,
                rgba(255, 0, 0, 0.06),
                rgba(0, 255, 0, 0.02),
                rgba(0, 0, 255, 0.06)
            );
            background-size: 100% 4px, 6px 100%;
            opacity: 0.9;
        }

        #noise {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
            opacity: 0.025;
        }

        #vignette {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: radial-gradient(
                circle,
                transparent 50%,
                rgba(0, 0, 0, 0.75) 100%
            );
        }

        /* Main Container */
        .login-container {
            width: 100%;
            max-width: 520px;
            padding: 30px;
            background: var(--crt-surface);
            border: 2px solid var(--crt-border);
            border-radius: 4px;
            box-shadow: 0 0 20px var(--crt-glow), inset 0 0 10px var(--crt-glow);
            text-shadow: var(--crt-text-glow);
            z-index: 10;
            position: relative;
            animation: flicker 0.15s infinite alternate;
        }

        @keyframes flicker {
            0% { opacity: 0.99; }
            100% { opacity: 0.97; }
        }

        .header-logo {
            text-align: center;
            margin-bottom: 25px;
            border-bottom: 1px double var(--crt-border);
            padding-bottom: 20px;
        }

        .brand-badge {
            display: inline-block;
            border: 2px solid var(--crt-color);
            padding: 2px 10px;
            font-weight: bold;
            font-size: 14px;
            background: var(--crt-surface-elevated);
            margin-bottom: 10px;
            letter-spacing: 2px;
        }

        .brand-title {
            font-size: 22px;
            font-weight: bold;
            letter-spacing: 1px;
            text-transform: uppercase;
        }

        .system-meta {
            font-size: 11px;
            opacity: 0.7;
            margin-top: 5px;
            line-height: 1.5;
        }

        /* Form styling */
        .form-group {
            margin-bottom: 20px;
        }

        .form-label {
            display: block;
            font-size: 12px;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .input-wrapper {
            position: relative;
            display: flex;
            align-items: center;
        }

        .input-wrapper .prompt-char {
            position: absolute;
            left: 12px;
            font-weight: bold;
            user-select: none;
        }

        .auth-input {
            width: 100%;
            background: var(--crt-surface-elevated);
            border: 1px solid var(--crt-border);
            border-radius: 2px;
            padding: 12px 12px 12px 28px;
            color: var(--crt-color);
            font-family: inherit;
            font-size: 14px;
            letter-spacing: 2px;
            text-shadow: var(--crt-text-glow);
            outline: none;
            transition: all 0.2s ease;
        }

        .auth-input:focus {
            border-color: var(--crt-color);
            box-shadow: 0 0 10px var(--crt-glow);
        }

        .submit-btn {
            width: 100%;
            background: var(--crt-color);
            color: var(--crt-bg);
            border: none;
            border-radius: 2px;
            padding: 12px;
            font-family: inherit;
            font-size: 14px;
            font-weight: bold;
            text-transform: uppercase;
            cursor: pointer;
            letter-spacing: 1.5px;
            transition: all 0.2s ease;
            box-shadow: 0 0 10px var(--crt-glow);
        }

        .submit-btn:hover {
            opacity: 0.9;
            box-shadow: 0 0 15px var(--crt-color);
        }

        .submit-btn:active {
            transform: scale(0.99);
        }

        /* Terminal Logs Area */
        .terminal-logs {
            margin-top: 25px;
            background: rgba(0, 0, 0, 0.5);
            border: 1px solid var(--border-subtle);
            border-radius: 2px;
            padding: 12px;
            height: 120px;
            overflow-y: auto;
            font-size: 11px;
            line-height: 1.6;
            color: var(--crt-color);
            opacity: 0.8;
            font-family: inherit;
        }

        .log-entry {
            margin-bottom: 4px;
            white-space: pre-wrap;
            word-break: break-all;
        }

        .log-entry.error {
            color: #ff0844;
            text-shadow: 0 0 4px rgba(255, 8, 68, 0.4);
        }

        .log-entry.success {
            color: #10b981;
            text-shadow: 0 0 4px rgba(16, 185, 129, 0.4);
        }

        /* Theme Selector */
        .theme-selector {
            position: absolute;
            bottom: 20px;
            right: 20px;
            display: flex;
            gap: 8px;
            z-index: 20;
        }

        .theme-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            cursor: pointer;
            border: 1px solid rgba(255, 255, 255, 0.3);
            transition: transform 0.2s;
        }

        .theme-dot:hover {
            transform: scale(1.2);
        }

        .theme-dot.green { background: #33ff33; }
        .theme-dot.amber { background: #ffb000; }
        .theme-dot.cyan { background: #00ffff; }

        /* Shake animation for incorrect password */
        .shake {
            animation: shake-anim 0.3s ease-in-out;
        }

        @keyframes shake-anim {
            0%, 100% { transform: translateX(0); }
            20%, 60% { transform: translateX(-10px); }
            40%, 80% { transform: translateX(10px); }
        }

        /* Mobile Optimization overrides for Security Login */
        @media (max-width: 576px) {
            .login-container {
                max-width: 92% !important;
                padding: 20px !important;
                margin: 10px !important;
            }
            .brand-title {
                font-size: 18px !important;
            }
            .theme-selector {
                bottom: 12px !important;
                right: 12px !important;
            }
            .auth-input {
                font-size: 13px !important;
                padding: 10px 10px 10px 24px !important;
            }
            .submit-btn {
                padding: 10px !important;
                font-size: 13px !important;
            }
            .terminal-logs {
                height: 100px !important;
                font-size: 10px !important;
            }
        }
    </style>
</head>
<body class="theme-green">
    <div id="crt-frame">
        <div id="crt-screen-overlay">
            <div id="scanlines"></div>
            <div id="noise"></div>
            <div id="vignette"></div>
        </div>

        <div class="login-container" id="login-card">
            <div class="header-logo">
                <div class="brand-badge">[AGNT-SEC]</div>
                <div class="brand-title">AGE Desk Security</div>
                <div class="system-meta">
                    COGNITIVE CO-PILOT SYSTEM PORTAL<br>
                    LOCAL TIME: <span id="time-display">--:--:--</span><br>
                    STATUS: SECURED VIA AES-256 / SHA-256
                </div>
            </div>

            <div class="form-group">
                <label class="form-label" for="auth-key">Authorization Key</label>
                <div class="input-wrapper">
                    <span class="prompt-char">></span>
                    <input type="password" id="auth-key" class="auth-input" autofocus autocomplete="current-password" placeholder="••••••••" onkeydown="handleKeyDown(event)">
                </div>
            </div>

            <button class="submit-btn" id="submit-btn" onclick="submitAuth()">VALIDATE KEY</button>

            <div class="terminal-logs" id="logs-container">
                <div class="log-entry">[*] SYSTEM ACCESS CONTROL IS ACTIVE.</div>
                <div class="log-entry">[*] INITIALIZING ENCRYPTED AUTH SHIELD...</div>
                <div class="log-entry">[!] ACCESS RESTRICTED TO AUTHORIZED TRADERS ONLY.</div>
            </div>
        </div>
    </div>

    <div class="theme-selector">
        <div class="theme-dot green" onclick="setTheme('green')" title="P-1 Green Phosphor"></div>
        <div class="theme-dot amber" onclick="setTheme('amber')" title="P-3 Amber Phosphor"></div>
        <div class="theme-dot cyan" onclick="setTheme('cyan')" title="P-4 Cyan Phosphor"></div>
    </div>

    <script>
        // Set dynamic clock
        function updateTime() {
            const now = new Date();
            const timeStr = now.toTimeString().split(' ')[0];
            document.getElementById('time-display').innerText = timeStr;
        }
        setInterval(updateTime, 1000);
        updateTime();

        // Theme management (matches main dashboard preference storage)
        function setTheme(theme) {
            document.body.className = '';
            document.body.classList.add('theme-' + theme);
            localStorage.setItem('retro_dashboard_theme', 'theme-' + theme);
            addLog(`[*] Color Phosphor configured: ${theme.toUpperCase()}`);
        }

        // Load saved theme
        const savedTheme = localStorage.getItem('retro_dashboard_theme');
        if (savedTheme) {
            const themeName = savedTheme.replace('theme-', '');
            setTheme(themeName);
        }

        function addLog(text, type = '') {
            const container = document.getElementById('logs-container');
            const entry = document.createElement('div');
            entry.className = 'log-entry';
            if (type) entry.classList.add(type);
            
            const now = new Date();
            const stamp = `[${now.toTimeString().split(' ')[0]}] `;
            entry.innerText = stamp + text;
            
            container.appendChild(entry);
            container.scrollTop = container.scrollHeight;
        }

        function handleKeyDown(event) {
            if (event.key === 'Enter') {
                submitAuth();
            }
        }

        async function submitAuth() {
            const inputField = document.getElementById('auth-key');
            const btn = document.getElementById('submit-btn');
            const card = document.getElementById('login-card');
            const password = inputField.value;

            if (!password) {
                addLog('[!] PLEASE ENTER A VALID AUTHORIZATION KEY.', 'error');
                shakeCard();
                return;
            }

            // Lock UI elements
            inputField.disabled = true;
            btn.disabled = true;
            
            addLog('[*] GENERATING CRYPTOGRAPHIC INTEGRITY SIGNATURE...');
            addLog('[*] DISPATCHING VALIDATION PACKET TO CONTROL GATEWAY...');

            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ password: password })
                });

                const data = await response.json();

                if (response.ok && data.success) {
                    addLog('[+] SIGNATURE DECRYPTED. KEY MATCH DETECTED!', 'success');
                    addLog('[*] INJECTING SECURE SESSION INTERFACE...', 'success');
                    addLog('[*] DECK UNLOCKED. TRANSFERRING CONTROL...', 'success');
                    
                    setTimeout(() => {
                        window.location.reload();
                    }, 1200);
                } else {
                    addLog(`[!] AUTHENTICATION REFUSED: ${data.error || 'INVALID PASSKEY'}`, 'error');
                    shakeCard();
                    inputField.disabled = false;
                    btn.disabled = false;
                    inputField.value = '';
                    inputField.focus();
                }
            } catch (err) {
                addLog(`[!] GATEWAY TIMEOUT: ${err.message}`, 'error');
                shakeCard();
                inputField.disabled = false;
                btn.disabled = false;
            }
        }

        function shakeCard() {
            const card = document.getElementById('login-card');
            card.classList.remove('shake');
            void card.offsetWidth; // Trigger reflow to restart animation
            card.classList.add('shake');
        }
    </script>
</body>
</html>
"""

def get_expected_session_token():
    password = config.DASHBOARD_PASSWORD
    salt = config.SESSION_SALT
    return hashlib.sha256((password + salt).encode('utf-8')).hexdigest()

def is_authenticated(handler):
    if not config.DASHBOARD_PASSWORD:
        return True  # If password is empty, security is bypassed (local dev)
    cookie_header = handler.headers.get('Cookie', '')
    expected = get_expected_session_token()
    return f"age_session={expected}" in cookie_header

class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Prevent logging every static/API poll to stderr to keep the terminal clean
        return

    def do_GET(self):
        if config.DASHBOARD_PASSWORD and not is_authenticated(self):
            if self.path == '/':
                encoded_html = LOGIN_HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded_html)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                self.end_headers()
                self.wfile.write(encoded_html)
            else:
                encoded_err = json.dumps({"error": "Unauthorized. Please authenticate first."}).encode('utf-8')
                self.send_response(401)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_err)))
                self.end_headers()
                self.wfile.write(encoded_err)
            return

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
                        "executions": [],
                        "broker_orders": [],
                        "history": [],
                        "ticker_history": {},
                        "logs": ["Dashboard server is initializing, please wait..."],
                        "trading_universe": config.TRADING_UNIVERSE,
                        "screener_pool": [],
                        "latest_watchlist": [],
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
        # 1. Handle auth endpoint `/api/login` (unauthenticated)
        if self.path == '/api/login':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                request_payload = json.loads(post_data.decode('utf-8'))
                password = request_payload.get("password", "")
                
                if config.DASHBOARD_PASSWORD and password == config.DASHBOARD_PASSWORD:
                    expected_token = get_expected_session_token()
                    # Return success and set secure cookie
                    encoded_res = json.dumps({"success": True}).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded_res)))
                    cookie_val = f"age_session={expected_token}; Path=/; HttpOnly; SameSite=Strict"
                    # Add Secure flag if running over https (Cloud Run adds X-Forwarded-Proto header)
                    is_https = self.headers.get('X-Forwarded-Proto', 'http') == 'https'
                    if is_https:
                        cookie_val += "; Secure"
                    self.send_header('Set-Cookie', cookie_val)
                    self.end_headers()
                    self.wfile.write(encoded_res)
                else:
                    encoded_err = json.dumps({"error": "Incorrect password."}).encode('utf-8')
                    self.send_response(401)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded_err)))
                    self.end_headers()
                    self.wfile.write(encoded_err)
            except Exception as e:
                encoded_err = json.dumps({"error": f"Login processing error: {e}"}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Content-Length', str(len(encoded_err)))
                self.end_headers()
                self.wfile.write(encoded_err)
            return

        # 2. Security Guard: For any other POST endpoint, verify authentication if password is configured
        if config.DASHBOARD_PASSWORD and not is_authenticated(self):
            encoded_err = json.dumps({"error": "Unauthorized. Please authenticate first."}).encode('utf-8')
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', str(len(encoded_err)))
            self.end_headers()
            self.wfile.write(encoded_err)
            return

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

                # The SQLite DB is periodically replaced by the GCS sync worker.
                # Retry transient "database is locked"/"unable to open" errors so
                # a chat request that coincides with a sync doesn't 500.
                def _db_read(fn, *args, **kwargs):
                    last_err = None
                    for attempt in range(3):
                        try:
                            return fn(*args, **kwargs)
                        except sqlite3.OperationalError as e:
                            last_err = e
                            if "locked" in str(e).lower() or "unable to open" in str(e).lower():
                                time.sleep(0.3 * (attempt + 1))
                                continue
                            raise
                    raise last_err

                # Fetch recent decisions (SQLite)
                decisions = _db_read(database.get_recent_decisions, limit=10)
                trades = _db_read(database.get_recent_trades, limit=10)
                
                # Fetch live portfolio state
                client = AlpacaClient()
                account = client.get_account_state()
                positions = client.get_positions()
                
                positions_summary = ""
                if not positions:
                    positions_summary = "No open positions."
                for sym, pos in positions.items():
                    unit = "contract(s)" if pos.get("is_option") else "shares"
                    positions_summary += f"- {sym}: {pos['qty']} {unit} @ ${pos['avg_entry_price']} (Current Val: ${pos['market_value']}, PnL: ${pos['unrealized_pnl']})\n"
                
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
                # Fetch database performance summary (successes and failures)
                perf_summary = _db_read(database.get_performance_summary)
                perf_summary_str = perf_summary.get("text_summary", "No performance history available.")
                daily_performance_str = _db_read(database.get_daily_performance_breakdown, limit=15)

                system_instruction = (
                    "You are the cognitive co-pilot, visual strategist, and expert portfolio analyst for the AGE Desk Autonomous Trading Agent.\n"
                    "The user is a highly particular quant investor who wants to analyze hypotheses, understand historical trends, and evaluate trading performance.\n\n"
                    
                    "=== SYSTEM CAPABILITIES & TRADING FRAMEWORK ===\n"
                    "- Active Technical Indicators: RSI (14), Simple Moving Averages (SMA 20, SMA 50), MACD, and Bollinger Bands.\n"
                    "- Intraday VWAP & Bands: Our system calculates dynamic, daily-resetting Volume Weighted Average Price (VWAP) using 15-minute bars.\n"
                    "  It establishes standard deviation bands at ±1σ (vwap_upper_1, vwap_lower_1) and ±2σ (vwap_upper_2, vwap_lower_2).\n"
                    "- VWAP-Based Execution Strategy:\n"
                    "  1. Execution Quality: The agent targets buying below VWAP and selling above it.\n"
                    "  2. Mean Reversion: Price deviations exceeding ±2σ (vwap_dist_pct >= 2.0% or <= -2.0%) are flagged as extreme overextensions to trigger mean-reversion trades.\n"
                    "  3. Strategic Guardrails: The MetaStrategist dynamically rewrites trading rules to incorporate VWAP-based price-distance triggers.\n\n"
                    
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
                # Log the actual error so intermittent failures are diagnosable
                import traceback
                print(f"[Dashboard Server] /api/chat error: {e}", file=sys.stderr, flush=True)
                traceback.print_exc()
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
                if config.DASHBOARD_PASSWORD:
                    print(f"[*] Security Shield: ACTIVE (Restricted Access enabled)")
                else:
                    print(f"[!] Security Shield: BYPASSED (No password configured in .env)")
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
