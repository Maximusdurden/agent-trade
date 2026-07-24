import sys
import os

# Add parent directory of library (Z:\python\projects) and project root to python path
sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database

def get_portfolio_history():
    try:
        with database.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp ASC LIMIT 100")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error fetching portfolio history: {e}")
        return []

def test():
    print("Testing get_recent_decisions...")
    try:
        dec = database.get_recent_decisions(limit=15)
        print(f"Decisions fetched: {len(dec)}")
    except Exception as e:
        print("Error fetching decisions:", e)

    print("Testing get_recent_trades...")
    try:
        tr = database.get_recent_trades(limit=15)
        print(f"Trades fetched: {len(tr)}")
    except Exception as e:
        print("Error fetching trades:", e)

    print("Testing get_portfolio_history...")
    try:
        hist = get_portfolio_history()
        print(f"History fetched: {len(hist)}")
    except Exception as e:
        print("Error fetching history:", e)

if __name__ == "__main__":
    test()
