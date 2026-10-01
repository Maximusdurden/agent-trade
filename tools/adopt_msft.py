#!/usr/bin/env python3
"""Adopt the orphaned MSFT position into flatbase state and upload to GCS."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload_flatbase as fb_cfg
fb_cfg.apply_sideload_overrides()

from core.alpaca_client import AlpacaClient
from sideload import flatbase_state as fb_state

client = AlpacaClient()

# Show current broker positions
positions = client.get_positions()
print("Broker positions:")
for sym, pos in positions.items():
    print(f"  {sym}: qty={pos.get('qty')} avg_entry={pos.get('avg_entry_price')}")

# Run reconcile (adopts MSFT into state)
print("\nRunning reconcile_with_broker...")
adopted = fb_state.reconcile_with_broker(client)
print(f"Adopted: {adopted}")

# Show updated state
state = fb_state.load_state()
print(f"\nUpdated state active_positions: {list(state['active_positions'].keys())}")
for sym, pos in state["active_positions"].items():
    print(f"  {sym}: entry={pos['entry_price']:.2f} stop={pos['stop_loss']:.2f} "
          f"target_3r={pos['target_3r']:.2f} target_5r={pos['target_5r']:.2f} "
          f"shares={pos['remaining_shares']:.2f}")

# Upload to GCS so the cloud monitor picks it up
print("\nUploading state to GCS...")
fb_state.sync_up_to_gcs()
print("Done.")