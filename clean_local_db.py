"""
Clean mock-order trades from the downloaded database and re-upload.
"""
import sqlite3
import os

db_path = os.path.join(os.environ["TEMP"], "trading_agent.db")

if not os.path.exists(db_path):
    print(f"ERROR: {db_path} not found")
    sys.exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Show current state
cursor.execute("SELECT COUNT(*) FROM trades")
total = cursor.fetchone()[0]
print(f"Total trades in DB: {total}")

cursor.execute("SELECT COUNT(*) FROM trades WHERE alpaca_order_id LIKE 'mock-order-%'")
mock_count = cursor.fetchone()[0]
print(f"Mock-order trades: {mock_count}")

# Show all trades before deletion
cursor.execute("SELECT id, alpaca_order_id, symbol, side, qty, filled_avg_price, status, timestamp FROM trades ORDER BY id DESC")
all_trades = cursor.fetchall()
print(f"\nAll trades in DB:")
for r in all_trades:
    print(f"  id={r[0]}, order_id={r[1]}, symbol={r[2]}, side={r[3]}, qty={r[4]}, price={r[5]}, status={r[6]}, ts={r[7]}")

# Delete all trades with mock-order IDs
if mock_count > 0:
    cursor.execute("DELETE FROM trades WHERE alpaca_order_id LIKE 'mock-order-%'")
    conn.commit()
    print(f"\nDeleted {mock_count} mock-order trades")

    # Verify
    cursor.execute("SELECT COUNT(*) FROM trades")
    remaining = cursor.fetchone()[0]
    print(f"Remaining trades: {remaining}")
else:
    print("\nNo mock orders to clean")

conn.close()
print("\nDatabase cleaned. Ready for re-upload.")