"""Sell WFC at market open (one-shot). Created 2026-10-12.

The WFC shares are locked by 3 pending-cancel TP limit orders until the
market opens. This script waits for the cancels to clear, then places a
market sell for the full WFC position.
"""
import os, sys, time
sys.path.insert(0, r"Z:\python\projects\agent-trade")
os.chdir(r"Z:\python\projects\agent-trade")
from dotenv import load_dotenv
load_dotenv(r"Z:\python\projects\agent-trade\.env", override=True)
from core.alpaca_client import AlpacaClient

client = AlpacaClient()

def wait_for_shares(max_wait_s=300):
    """Wait until WFC shares are available (pending cancels cleared)."""
    for i in range(max_wait_s // 5):
        try:
            pos = client.trading_client.get_open_position("WFC")
            qty = float(pos.qty)
            avail = float(pos.qty_available)
            print(f"  t+{i*5}s: qty={qty} available={avail}")
            if qty > 0 and avail == qty:
                return qty
        except Exception as e:
            print(f"  t+{i*5}s: {e}")
        time.sleep(5)
    return None

if __name__ == "__main__":
    print("=== WFC market-open sell ===")
    qty = wait_for_shares()
    if qty is None:
        print("ERROR: WFC shares never became available. Manual intervention needed.")
        sys.exit(1)
    print(f"Selling {qty} WFC @ market...")
    result = client.execute_market_order("WFC", qty, "sell")
    print(f"Result: {result}")
    if result.get("status") == "filled":
        print(f"WFC SOLD at {result.get('filled_avg_price')}")
    else:
        print(f"WFC sell status: {result.get('status')}")
