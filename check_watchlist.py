import sys, json, sqlite3
sys.path.insert(0, r'Z:\python\projects\agent-trade')
from core import config

conn = sqlite3.connect(str(config.DATABASE_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('DB path:', config.DATABASE_PATH)
cur.execute('SELECT COUNT(*) as c FROM watchlist_history')
print('Total watchlist_history rows:', cur.fetchone()['c'])

cur.execute('SELECT id, watchlist, timestamp FROM watchlist_history ORDER BY id DESC LIMIT 10')
rows = cur.fetchall()
print('--- Last 10 watchlists ---')
for r in rows:
    wl = json.loads(r['watchlist'])
    has_ko = 'KO' in [s.upper() for s in wl]
    print(f"id={r['id']} at={r['timestamp']} count={len(wl)} KO={has_ko} :: {wl}")
conn.close()
