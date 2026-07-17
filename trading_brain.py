import os
import json
import logging

try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel, Field
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

if GENAI_AVAILABLE:
    class TradingDecision(BaseModel):
        thought_process: str = Field(description="Detailed explanation of technical indicators analyzed, how news and advanced anchors/pivots influenced direction, your confluence reasoning for the chosen dynamic trade size, and compliance with the mandatory strategy rule.")
        action: str = Field(description="The trading action: BUY, SELL, or HOLD.")
        symbol: str = Field(description="The ticker symbol to trade (must be one of the symbols provided in the active market data list, or an empty string).")
        quantity: float = Field(description="The quantity of shares or coins to trade (use 0 for HOLD).")
        take_profit_price: float | None = Field(default=None, description="Propose a dynamic take-profit target price for BUY actions on equities/non-crypto assets based on Fibonacci or psychological levels; null otherwise.")
        stop_loss_price: float | None = Field(default=None, description="Propose a dynamic stop-loss price for BUY actions on equities/non-crypto assets based on Fibonacci or psychological levels; null otherwise.")

import config
import database

logger = logging.getLogger("TradingBrain")

class TradingBrain:
    """The AI engine that consumes market and portfolio state and generates a trading decision."""
    
    def __init__(self):
        self.provider = config.LLM_PROVIDER
        self.api_key = config.GEMINI_API_KEY
        self.model_name = config.GEMINI_MODEL
        self.is_mock = False
        
        if self.provider == "gemini":
            if not GENAI_AVAILABLE:
                logger.warning("google-genai package is not installed. Falling back to mock rule-based brain.")
                self.is_mock = True
            elif not self.api_key or self.api_key == "your_gemini_api_key_here":
                logger.warning("Gemini API key is missing. Falling back to mock rule-based brain.")
                self.is_mock = True
            else:
                try:
                    # Use modern google-genai client
                    self.client = genai.Client(api_key=self.api_key)
                    logger.info(f"Successfully initialized Gemini Strategy Brain with model {self.model_name}.")
                except Exception as e:
                    logger.error(f"Failed to initialize Gemini Client: {e}. Falling back to rule-based brain.")
                    self.is_mock = True
        else:
            logger.warning(f"Unsupported provider '{self.provider}'. Falling back to rule-based brain.")
            self.is_mock = True

    def make_decision(self, market_data_list: list[dict], account_state: dict, positions: dict, recent_decisions: list[dict]) -> dict:
        """
        Formulates the prompt, calls the LLM (or mock fallback), and returns a structured decision.
        """
        if self.is_mock:
            return self._make_mock_decision(market_data_list, account_state, positions)

        # Build prompt
        prompt = self._build_prompt(market_data_list, account_state, positions, recent_decisions)
        
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TradingDecision
                )
            )
            decision_json = response.text
            
            # Parse response
            decision = json.loads(decision_json)
            
            # Normalize fields
            decision["action"] = decision.get("action", "HOLD").upper()
            decision["symbol"] = decision.get("symbol", "").upper()
            decision["quantity"] = float(decision.get("quantity", 0.0))
            
            tp = decision.get("take_profit_price")
            sl = decision.get("stop_loss_price")
            decision["take_profit_price"] = float(tp) if tp is not None else None
            decision["stop_loss_price"] = float(sl) if sl is not None else None
            
            logger.info(f"Brain generated decision: {decision['action']} {decision['quantity']} {decision['symbol']} | TP: {decision['take_profit_price']} | SL: {decision['stop_loss_price']}")
            return decision
            
        except Exception as e:
            logger.error(f"Error in LLM decision making: {e}. Falling back to safe rule-based decision.")
            return self._make_mock_decision(market_data_list, account_state, positions)

    def _build_prompt(self, market_data_list: list[dict], account_state: dict, positions: dict, recent_decisions: list[dict]) -> str:
        """Constructs a comprehensive system prompt and state description."""
        
        # Standardize state strings for prompt representation
        portfolio_summary = f"""
- Total Net Equity: ${account_state.get('equity', 0.0):,.2f}
- Cash Balance: ${account_state.get('cash', 0.0):,.2f}
- Buying Power: ${account_state.get('buying_power', 0.0):,.2f}
- Unrealized PnL: ${account_state.get('unrealized_pnl', 0.0):,.2f}
"""

        positions_summary = ""
        if not positions:
            positions_summary = "No current open positions."
        for sym, pos in positions.items():
            positions_summary += f"- {sym}: {pos['qty']} shares, current value: ${pos['market_value']:,.2f}, avg entry price: ${pos['avg_entry_price']:,.2f}, PnL: ${pos['unrealized_pnl']:,.2f}\n"

        market_summary = ""
        for data in market_data_list:
            if not data:
                continue
            symbol = data['symbol']
            ind = data.get("indicators", {})
            
            # Fetch active strategy rule from our daily strategist database
            active_rule = database.get_active_strategy(symbol)
            
            # Extract advanced pivots
            pivots = data.get("advanced_pivots", {})
            fib_str = ", ".join([f"{k}: ${v:,.2f}" for k, v in pivots.get("fib_levels", {}).items()]) if pivots.get("fib_levels") else "None"
            
            psy_levels = pivots.get("psychological_levels", {})
            if psy_levels:
                sup_p = psy_levels.get('closest_support')
                res_p = psy_levels.get('closest_resistance')
                psy_str = f"Support: ${sup_p:,.2f}" if sup_p else "Support: None"
                psy_str += f", Resistance: ${res_p:,.2f}" if res_p else ", Resistance: None"
            else:
                psy_str = "None"
                
            zones = pivots.get("pivot_zones", {})
            if zones:
                swing_sup = zones.get('recent_swing_support')
                swing_res = zones.get('recent_swing_resistance')
                zones_str = f"Support (Swing Low): ${swing_sup:,.2f}" if swing_sup else "Support (Swing Low): None"
                zones_str += f", Resistance (Swing High): ${swing_res:,.2f}" if swing_res else ", Resistance (Swing High): None"
            else:
                zones_str = "None"
                
            # Extract recent news
            news_list = data.get("news", [])
            news_str = ""
            if news_list:
                for idx, n in enumerate(news_list[:3]):
                    news_str += f"  - Headline {idx+1}: {n.get('headline')} (Source: {n.get('source')})\n    Summary: {n.get('summary')}\n"
            else:
                news_str = "  - No recent news items found.\n"
            
            market_summary += f"""
---
Ticker: {symbol}
- Current Price: ${data['current_price']:,.2f}
- Daily Change: {data['daily_return_pct']:.2%}%
- RSI (14-day): {ind.get('rsi_14')}
- SMA 20: ${ind.get('sma_20')}
- SMA 50: ${ind.get('sma_50')}
- MACD Line: {ind.get('macd_line')}
- MACD Signal: {ind.get('macd_signal')}
- MACD Histogram: {ind.get('macd_hist')}
- Bollinger Upper Band: ${ind.get('bollinger_upper')}
- Bollinger Lower Band: ${ind.get('bollinger_lower')}
- ADVANCED PRICE ANCHORS:
  - Fibonacci Retracement Levels: {fib_str}
  - Psychological Levels: {psy_str}
  - Support & Resistance Swing Zones: {zones_str}
- RECENT NEWS & MARKET EVENTS:
{news_str}
- MANDATORY TRADING RULE (Written by Meta-Strategist): "{active_rule}"
"""

        recent_history_summary = ""
        if not recent_decisions:
            recent_history_summary = "No recent decisions recorded."
        for d in recent_decisions[:3]:
            recent_history_summary += f"- Timestamp: {d.get('timestamp')}, Decision: {d.get('proposed_action')} {d.get('proposed_qty')} {d.get('proposed_symbol')}, Approved: {d.get('is_approved')} (Reason: {d.get('rejection_reason')}), Logic: {d.get('thought_process')[:150]}...\n"

        # Fetch historical successes/failures performance summary
        perf_summary = database.get_performance_summary()
        perf_summary_str = perf_summary.get("text_summary", "No performance history available.")

        allowed_symbols_str = " | ".join([f'"{sym}"' for sym in config.TRADING_UNIVERSE]) + ' | ""'
        
        system_instruction = f"""
ROLE:
You are an elite, professional, risk-averse financial quantitative trading agent. Your objective is to formulate a single high-conviction trade choice (BUY, SELL, or HOLD) that yields a profitable and stable trading strategy.

DIRECTIONS:
1. Analyze the technical indicators (RSI, Moving Averages, MACD, Bollinger Bands) to judge trends, support/resistance, overbought/oversold levels.
2. Observe Advanced Price Anchors (Fibonacci retracements, Psychological levels, Support/Resistance Swing zones) to find key pivot levels. Look for confluences where multiple anchors line up.
3. Evaluate Recent News and Market Events for underlying sentiment. Bullish news should bolster buy conviction; bearish news or market distress should warrant extreme safety or sell execution.
4. Scale your trade size (quantity) dynamically based on conviction and indicators:
   - MAXIMUM LIMIT: Any single trade's cost MUST NOT exceed 10% of total portfolio equity.
   - DEFENSIVE SIZING (1% - 3% of equity): Use this when trends are unclear, when RSI is neutral (40-60), or when the price is in "no man's land" between key support and resistance zones.
   - AGGRESSIVE / CONVICTION SIZING (5% - 10% of equity): Scale up to this level ONLY when there is high confluence:
     - BUY CONFLUENCE: Price is sitting directly on major Fibonacci support (50.0% or 61.8%), near a round psychological support, near a recent swing low zone, RSI is oversold (<35), and recent news is supportive or consolidating.
     - SELL CONFLUENCE: Price is sitting directly on major Fibonacci resistance (61.8% or 100%), near psychological resistance, RSI is overbought (>70), or price breaks down below key swing support under negative news.
5. Observe your current portfolio state. Ensure you hold a stock before deciding to SELL. Verify you have enough cash to BUY.
6. LEARN FROM HISTORICAL PERFORMANCE SUCCESSES & FAILURES:
   - Carefully review the HISTORICAL PORTFOLIO PERFORMANCE section below. 
   - If our historical win rate is low (<50%) or max drawdown is high (>15%), you MUST be extremely conservative: scale down trade sizes to 1-3%, avoid buying any asset with high recent failures, and keep a larger cash buffer.
   - Adjust your buy thresholds and exit levels dynamically to avoid repeating past whipsaws and losing patterns.
7. Be highly decisive but risk-conscious. Do not over-trade. Avoid whipsawing (reversing recent trades without strong technical reasons).
8. You must trade ONLY one symbol from the provided tickers per step, or choose HOLD/NO_ACTION.
9. YOU MUST COMPLY WITH THE MANDATORY TRADING RULE SPECIFIED FOR EACH TICKER. DO NOT FORMULATE A DECISION THAT CONTRADICTS THESE RULES.

OUTPUT FORMAT:
You must reply with a valid JSON object ONLY. Do not wrap in markdown blocks other than standard JSON mime type.
JSON Schema:
{{
  "thought_process": "Detailed explanation of technical indicators analyzed, how news and advanced anchors/pivots influenced direction, your confluence reasoning for the chosen dynamic trade size, and compliance with the mandatory strategy rule.",
  "action": "BUY" | "SELL" | "HOLD",
  "symbol": {allowed_symbols_str},
  "quantity": float (number of shares, use 0 for HOLD),
  "take_profit_price": float or null (propose a dynamic take-profit target price for BUY actions on equities based on Fibonacci ratios or psychological resistance; null otherwise),
  "stop_loss_price": float or null (propose a dynamic stop-loss price for BUY actions on equities based on Fibonacci/psychological levels or recent swing lows; null otherwise)
}}
"""

        prompt = f"""
{system_instruction}

=== CURRENT PORTFOLIO STATE ===
{portfolio_summary}

=== CURRENT HOLDINGS ===
{positions_summary}

=== HISTORICAL PORTFOLIO PERFORMANCE (SUCCESS & FAILURE LEARNINGS) ===
{perf_summary_str}

=== MARKET DATA & INDICATORS ===
{market_summary}

=== RECENT DECISION LOGS ===
{recent_history_summary}

Generate your trade decision JSON:
"""
        return prompt

    def _make_mock_decision(self, market_data_list: list[dict], account_state: dict, positions: dict) -> dict:
        """Fallback rule-based trading agent (useful for testing or if LLM config is missing)."""
        logger.info("Running rule-based strategy fallback.")
        
        # Super simple rule-based strategy:
        # If QQQ or SPY RSI < 35 -> Buy a few shares (oversold)
        # If QQQ or SPY RSI > 70 and we own it -> Sell a few shares (overbought)
        # Else -> HOLD
        
        for data in market_data_list:
            if not data:
                continue
            symbol = data["symbol"]
            current_price = data["current_price"]
            ind = data.get("indicators", {})
            rsi = ind.get("rsi_14", 50)
            
            if rsi is None:
                rsi = 50
                
            # Buy signal (RSI < 42 / Oversold-ish)
            if rsi < 42:
                # Calculate safe mock purchase qty (~5% of equity)
                equity = account_state.get("equity", 100000.0)
                qty = max(1.0, int((equity * 0.05) // current_price))
                tp = round(current_price * 1.05, 2)
                sl = round(current_price * 0.97, 2)
                return {
                    "thought_process": f"[Rule Fallback] {symbol} RSI is oversold ({rsi:.1f}). Buying a small safe position of {qty} shares.",
                    "action": "BUY",
                    "symbol": symbol,
                    "quantity": float(qty),
                    "take_profit_price": float(tp),
                    "stop_loss_price": float(sl)
                }
            
            # Sell signal (RSI > 62 / Overbought-ish)
            elif rsi > 62 and symbol in positions:
                owned_qty = positions[symbol]["qty"]
                qty = max(1.0, int(owned_qty * 0.5))  # sell half the position
                return {
                    "thought_process": f"[Rule Fallback] {symbol} RSI is overbought ({rsi:.1f}) and we own {owned_qty} shares. Selling {qty} shares.",
                    "action": "SELL",
                    "symbol": symbol,
                    "quantity": float(qty),
                    "take_profit_price": None,
                    "stop_loss_price": None
                }

        # Default action
        return {
            "thought_process": "[Rule Fallback] No clear oversold or overbought conditions detected. Holding current positions.",
            "action": "HOLD",
            "symbol": "",
            "quantity": 0.0,
            "take_profit_price": None,
            "stop_loss_price": None
        }
