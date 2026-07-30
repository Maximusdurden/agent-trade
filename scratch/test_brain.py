# Z:\python\projects\agent-trade\scratch\test_brain.py
import sys
import os

# Add core path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.trading_brain import TradingBrain

def main():
    print("Initializing TradingBrain...")
    brain = TradingBrain()
    
    # Mock parameters
    market_data = [
        {
            "symbol": "SOL/USD",
            "current_price": 150.25,
            "daily_return_pct": 0.015,
            "indicators": {
                "rsi_14": 45.0,
                "macd_line": 0.5,
                "macd_signal": 0.3,
                "macd_hist": 0.2,
                "sma_20": 149.5,
                "sma_50": 148.0,
                "vwap": 149.8,
                "bollinger_upper": 152.0,
                "bollinger_lower": 146.0,
                "vwap_upper_1": 150.5,
                "vwap_lower_1": 149.1,
                "vwap_upper_2": 151.2,
                "vwap_lower_2": 148.4,
                "vwap_dist_pct": 0.3
            },
            "advanced_pivots": {
                "fib_levels": {
                    "0.236": 152.0,
                    "0.382": 150.0,
                    "0.5": 148.5,
                    "0.618": 147.0
                },
                "psychological_levels": {
                    "closest_support": 150.0,
                    "closest_resistance": 155.0
                },
                "pivot_zones": {
                    "recent_swing_support": 145.0,
                    "recent_swing_resistance": 155.0
                }
            },
            "news": [
                {
                    "headline": "Solana exhibits strong daily transactions growth.",
                    "source": "CryptoNews",
                    "summary": "Solana blockchain network has shown impressive growth in user transactions over the past week."
                }
            ]
        }
    ]
    
    account_state = {
        "equity": 10000.0,
        "cash": 5000.0,
        "buying_power": 5000.0,
        "unrealized_pnl": 0.0
    }
    
    positions = {}
    recent_decisions = []
    
    print("Calling make_decision on TradingBrain...")
    try:
        decision = brain.make_decision(market_data, account_state, positions, recent_decisions)
        print("\n=== SUCCESS ===")
        print("Decision generated:")
        import json
        print(json.dumps(decision, indent=2))
    except Exception as e:
        print("\n=== FAILURE ===")
        print("Error during make_decision:", e)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
