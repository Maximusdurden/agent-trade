"""Verify the guardrail fix: held positions are tradable even if not in the
static universe or the latest watchlist (the KO scenario)."""
import sys
sys.path.insert(0, r'Z:\python\projects\agent-trade')

from core.guardrails import RiskGuardrails

g = RiskGuardrails()

# Scenario: KO is a HELD position but NOT in TRADING_UNIVERSE and NOT in the
# latest watchlist. The runner appraises held positions, so the brain may
# recommend BUY/SELL. The guardrail must allow it.
decision = {'action': 'BUY', 'symbol': 'KO', 'quantity': 8.5, 'current_price': 87.11}
account = {'equity': 100000.0, 'cash': 50000.0, 'unrealized_pnl': 0.0, 'last_equity': 100000.0}
positions = {'KO': {'qty': 8.0, 'qty_available': 8.0}}  # KO held

ok, msg, adj = g.validate_and_adjust_decision(decision, account, positions)
print('BUY KO (held position):')
print('  approved:', ok)
print('  msg:', msg)
print('  qty:', adj.get('quantity'))
print()

# Also verify a SELL on a held position is allowed
decision2 = {'action': 'SELL', 'symbol': 'KO', 'quantity': 4.0, 'current_price': 87.11}
ok2, msg2, adj2 = g.validate_and_adjust_decision(decision2, account, positions)
print('SELL KO (held position):')
print('  approved:', ok2)
print('  msg:', msg2)
print()

# Sanity: a symbol that is NOT held and NOT in universe/watchlist should still be rejected
decision3 = {'action': 'BUY', 'symbol': 'ZZZZ', 'quantity': 10.0, 'current_price': 50.0}
ok3, msg3, _ = g.validate_and_adjust_decision(decision3, account, {})
print('BUY ZZZZ (not held, not in universe):')
print('  approved:', ok3)
print('  msg:', msg3)
