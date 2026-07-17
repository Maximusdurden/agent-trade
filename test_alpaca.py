import sys
import os

# Add parent directory of library (Z:\python\projects) to python path
sys.path.insert(0, r"Z:\python\projects")

import config
from alpaca_client import AlpacaClient

def test():
    print("Initializing AlpacaClient...")
    client = AlpacaClient()
    print(f"Is mock? {client.is_mock}")
    if client.is_mock:
        print("Mock client is active.")
        return
    
    print("Fetching account state...")
    try:
        acc = client.get_account_state()
        print("Account State:", acc)
    except Exception as e:
        print("Error fetching account state:", e)

    print("Fetching positions...")
    try:
        pos = client.get_positions()
        print("Positions:", pos)
    except Exception as e:
        print("Error fetching positions:", e)

if __name__ == "__main__":
    test()
