import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
cursor.execute('''
    INSERT OR REPLACE INTO trades (id, decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status)
    VALUES 
    (266, NULL, 'f6846c30-f4ac-4703-8988-68d84e912a2b', '2026-10-19T18:48:17', 'AMD', 'buy', 4.0, 539.96, 'filled'),
    (267, NULL, '727f3f75-cc3e-4cc2-a28e-27eecf532801', '2026-10-19T13:34:30', 'AMD', 'sell', 4.0, 547.40, 'filled')
''')
conn.commit()
print("Inserted trades into trades table. Total trades:", cursor.execute('SELECT COUNT(*) FROM trades').fetchone()[0])
conn.close()
