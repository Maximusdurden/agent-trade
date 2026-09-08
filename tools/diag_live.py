"""Diagnose live dashboard decision stream + watchlist state."""
import sqlite3, json, sys

def diag(db_path):
    print(f"\n===== {db_path} =====")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("TABLES:", tables)

    if "decisions" in tables:
        cur.execute("SELECT COUNT(*) FROM decisions")
        print("decisions rows:", cur.fetchone()[0])
        cur.execute("SELECT MAX(timestamp) FROM decisions")
        print("max decision ts:", cur.fetchone()[0])
        cur.execute("SELECT id,timestamp,proposed_symbol,proposed_action,is_approved,rejection_reason,cycle_id FROM decisions ORDER BY id DESC LIMIT 12")
        print("--- last 12 decisions ---")
        for r in cur.fetchall():
            print(dict(r))

    if "watchlist_history" in tables:
        cur.execute("SELECT COUNT(*) FROM watchlist_history")
        print("watchlist rows:", cur.fetchone()[0])
        cur.execute("SELECT id,timestamp,watchlist FROM watchlist_history ORDER BY id DESC LIMIT 8")
        print("--- last 8 watchlists ---")
        for r in cur.fetchall():
            try:
                wl = json.loads(r["watchlist"])
            except Exception:
                wl = r["watchlist"]
            print(f"id={r['id']} at={r['timestamp']} count={len(wl) if isinstance(wl,list) else '?'} :: {wl}")

    if "ticker_convictions" in tables:
        cur.execute("SELECT COUNT(*) FROM ticker_convictions")
        print("ticker_convictions rows:", cur.fetchone()[0])
        cur.execute("SELECT MAX(timestamp) FROM ticker_convictions")
        print("max conviction ts:", cur.fetchone()[0])
        cur.execute("SELECT cycle_id,symbol,direction,conviction,timestamp FROM ticker_convictions ORDER BY id DESC LIMIT 12")
        print("--- last 12 convictions ---")
        for r in cur.fetchall():
            print(dict(r))
    conn.close()

for db in sys.argv[1:]:
    diag(db)