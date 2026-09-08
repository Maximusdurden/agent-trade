import sys, json, sqlite3
sys.path.insert(0, r'Z:\python\projects\agent-trade')
from core import config

conn = sqlite3.connect(str(config.DATABASE_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Search all watchlist_history for KO
cur.execute('SELECT id, watchlist, timestamp FROM watchlist_history')
rows = cur.fetchall()
ko_rows = []
for r in rows:
    wl = json.loads(r['watchlist'])
    if 'KO' in [s.upper() for s in wl]:
        ko_rows.append((r['id'], r['timestamp'], wl))

print('=== KO in watchlist_history ===')
print('Total rows with KO:', len(ko_rows))
for rid, ts, wl in ko_rows[:20]:
    print(f"id={rid} at={ts} :: {wl}")

print('=== Recent KO decisions ===')
cur.execute("SELECT id, timestamp, proposed_symbol, proposed_action, proposed_qty, is_approved, rejection_reason FROM decisions WHERE proposed_symbol LIKE '%KO%' ORDER BY id DESC LIMIT 15")
rows = cur.fetchall()
print('KO decision rows:', len(rows))
for r in rows:
    print(dict(r))

print('\n=== Recent rejections (any symbol) ===')
cur.execute("SELECT id, timestamp, proposed_symbol, proposed_action, is_approved, rejection_reason FROM decisions WHERE is_approved=0 ORDER BY id DESC LIMIT 15")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

conn.close()
