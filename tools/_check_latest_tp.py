import sqlite3
c = sqlite3.connect(r"Z:\python\projects\agent-trade\trading_agent_live.db")
c.row_factory = sqlite3.Row
print("### Latest decision's thought_process ###")
for r in c.execute("SELECT id, timestamp, proposed_action, proposed_symbol, direction, conviction, instrument, thought_process FROM decisions ORDER BY id DESC LIMIT 1"):
    d = dict(r)
    print(f"\nID {d['id']} {d['timestamp']} | {d['proposed_action']} {d['proposed_symbol']} | dir={d['direction']} conv={d['conviction']} inst={d['instrument']}")
    print("TP:", (d.get('thought_process') or ''))