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
        thought_process: str = Field(description="Detailed explanation of technical indicators analyzed, how news and advanced anchors/pivots influenced direction, your confluence reasoning for the chosen dynamic trade size, and compliance with the mandatory strategy rule. MUST also explicitly state: (1) your conviction level and why, and (2) if the symbol is in the options universe, whether you'd express the view via options (leverage) vs shares, and why.")
        action: str = Field(description="The trading action: BUY, SELL, or HOLD.")
        symbol: str = Field(description="The ticker symbol to trade (must be one of the symbols provided in the active market data list, or an empty string).")
        quantity: float = Field(description="The quantity of shares or coins to trade (use 0 for HOLD).")
        take_profit_price: float | None = Field(default=None, description="Propose a dynamic take-profit target price for BUY actions on equities/non-crypto assets based on Fibonacci or psychological levels; null otherwise.")
        stop_loss_price: float | None = Field(default=None, description="Propose a dynamic stop-loss price for BUY actions on equities/non-crypto assets based on Fibonacci or psychological levels; null otherwise.")
        # --- Conviction-threshold model (options-aware) ---
        # The agent expresses a directional view + conviction score. It does NOT
        # choose the instrument (stock vs option) directly — a deterministic rule
        # maps conviction -> instrument. This prevents stock<->option whipsaw.
        direction: str = Field(default="neutral", description="Directional view for the symbol: 'bullish', 'bearish', or 'neutral'. This expresses market view, not an instrument choice.")
        conviction: float = Field(default=0.0, description="Conviction score 0.0-1.0 indicating how strongly you believe in the direction. High conviction (>= config threshold) may be expressed via options for leverage; lower conviction via shares.")
        option_dte_min: int | None = Field(default=None, description="Optional requested minimum days-to-expiry for an option (overrides the default window within hard safety bounds). Null to use the configured default.")
        option_dte_max: int | None = Field(default=None, description="Optional requested maximum days-to-expiry for an option (overrides the default window within hard safety bounds). Null to use the configured default.")
        option_strike_otm_pct: float | None = Field(default=None, description="Optional requested OTM% for strike selection (e.g. 0.05 = 5% out-of-the-money). Null to use the configured default.")

    class TradingDecisionSet(BaseModel):
        """Container for MULTIPLE per-ticker trading decisions (Option B).

        The agent emits one decision per appraised ticker. Each is validated
        and executed independently by the runner.
        """
        decisions: list[TradingDecision] = Field(default_factory=list,
            description="One trading decision for each appraised ticker in the market data list. Must include EVERY ticker (even HOLDs). Each may have a different action, direction, and conviction.")

from core import config
from core import database

logger = logging.getLogger("TradingBrain")

class TradingBrain:
    """The AI engine that consumes market and portfolio state and generates a trading decision."""
    
    def __init__(self):
        self.provider = config.LLM_PROVIDER
        self.is_mock = False
        
        if self.provider == "openrouter":
            try:
                from core.llm_client import SharedLLMClient
                self.llm_client = SharedLLMClient()
                logger.info("Successfully initialized OpenRouter SharedLLMClient for TradingBrain (Daily Driver Tier).")
            except Exception as e:
                logger.error(f"Failed to initialize OpenRouter SharedLLMClient: {e}. Falling back to rule-based brain.")
                self.is_mock = True
        elif self.provider == "gemini":
            self.api_key = config.GEMINI_API_KEY
            self.model_name = config.GEMINI_MODEL
            if not GENAI_AVAILABLE:
                logger.warning("google-genai package is not installed. Falling back to mock rule-based brain.")
                self.is_mock = True
            elif not self.api_key or self.api_key == "your_gemini_api_key_here":
                logger.warning("Gemini API key is missing. Attempting fallback to OpenRouter.")
                self.provider = "openrouter"
                try:
                    from core.llm_client import SharedLLMClient
                    self.llm_client = SharedLLMClient()
                    logger.info("Successfully initialized OpenRouter SharedLLMClient for TradingBrain (Daily Driver Tier).")
                except Exception as e:
                    logger.error(f"Failed to initialize OpenRouter SharedLLMClient: {e}. Falling back to mock rule-based brain.")
                    self.is_mock = True
            else:
                try:
                    # Use modern google-genai client
                    self.client = genai.Client(api_key=self.api_key)
                    logger.info(f"Successfully initialized Gemini Strategy Brain with model {self.model_name}.")
                except Exception as e:
                    logger.error(f"Failed to initialize Gemini Client: {e}. Attempting fallback to OpenRouter.")
                    self.provider = "openrouter" # Fallback to OpenRouter
                    try:
                        from core.llm_client import SharedLLMClient
                        self.llm_client = SharedLLMClient()
                        logger.info("Successfully initialized OpenRouter SharedLLMClient for TradingBrain (Daily Driver Tier).")
                    except Exception as e:
                        logger.error(f"Failed to initialize OpenRouter SharedLLMClient: {e}. Falling back to mock rule-based brain.")
                        self.is_mock = True
        else:
            logger.warning(f"Unsupported provider '{self.provider}'. Falling back to rule-based brain.")
            self.is_mock = True

    @staticmethod
    def _normalize_decision(decision: dict) -> dict:
        """Normalizes a single per-ticker decision dict (shared by providers)."""
        decision = dict(decision)
        decision["action"] = str(decision.get("action", "HOLD")).upper()
        decision["symbol"] = str(decision.get("symbol", "")).upper()
        try:
            decision["quantity"] = float(decision.get("quantity", 0.0))
        except (TypeError, ValueError):
            decision["quantity"] = 0.0

        tp = decision.get("take_profit_price")
        sl = decision.get("stop_loss_price")
        decision["take_profit_price"] = float(tp) if tp is not None else None
        decision["stop_loss_price"] = float(sl) if sl is not None else None

        decision["direction"] = str(decision.get("direction", "neutral")).lower()
        if decision["direction"] not in ("bullish", "bearish", "neutral"):
            decision["direction"] = "neutral"
        try:
            decision["conviction"] = float(decision.get("conviction", 0.0))
        except (TypeError, ValueError):
            decision["conviction"] = 0.0
        decision["conviction"] = max(0.0, min(1.0, decision["conviction"]))
        for f in ("option_dte_min", "option_dte_max", "option_strike_otm_pct"):
            v = decision.get(f)
            decision[f] = float(v) if v is not None else None
        return decision

    def make_decision(self, market_data_list: list[dict], account_state: dict, positions: dict, recent_decisions: list[dict]) -> list[dict]:
        """
        Formulates the prompt, calls the LLM (or mock fallback), and returns a
        LIST of structured per-ticker decisions (one per appraised symbol).
        """
        if self.is_mock:
            return self._make_mock_decision(market_data_list, account_state, positions)

        # Build prompt
        prompt = self._build_prompt(market_data_list, account_state, positions, recent_decisions)
        
        if self.provider == "openrouter":
            try:
                # Call generate_structured using TradingDecisionSet (list) model
                result = self.llm_client.generate_structured(
                    prompt=prompt,
                    response_model=TradingDecisionSet,
                    tier=config.BRAIN_MODEL_TIER,
                    max_output_tokens=config.BRAIN_MAX_OUTPUT_TOKENS
                )
                raw_decisions = result.get("decisions", []) if isinstance(result, dict) else []
                decisions = [self._normalize_decision(d) for d in raw_decisions if isinstance(d, dict)]
                logger.info(f"Brain generated {len(decisions)} per-ticker decision(s).")
                for d in decisions:
                    logger.info(f"  -> {d['action']} {d['quantity']} {d['symbol']} | dir={d['direction']} conv={d['conviction']}")
                return decisions
            except Exception as e:
                logger.error(f"Error in OpenRouter LLM decision making: {e}. Falling back to safe rule-based decision.")
                try:
                    import sys
                    from agent_jira.jira_logger import log_exception
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    log_exception(
                        exc_type, exc_value, exc_tb,
                        app_name="agent-trade",
                        env="production",
                        metadata={"Context": "Trading Tick Decision Failure"}
                    )
                except Exception as ex:
                    logger.error(f"Failed to log exception to JIRA: {ex}")
                return self._make_mock_decision(market_data_list, account_state, positions)

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TradingDecisionSet
                )
            )
            decision_json = response.text
            
            # Parse response
            result = json.loads(decision_json)
            # Support both wrapper {"decisions": [...]} and a bare list fallback
            if isinstance(result, dict):
                raw_decisions = result.get("decisions", [])
            elif isinstance(result, list):
                raw_decisions = result
            else:
                raw_decisions = []
            decisions = [self._normalize_decision(d) for d in raw_decisions if isinstance(d, dict)]
            logger.info(f"Brain generated {len(decisions)} per-ticker decision(s).")
            for d in decisions:
                logger.info(f"  -> {d['action']} {d['quantity']} {d['symbol']} | dir={d['direction']} conv={d['conviction']}")
            return decisions
            
        except Exception as e:
            logger.error(f"Error in LLM decision making: {e}. Falling back to safe rule-based decision.")
            return self._make_mock_decision(market_data_list, account_state, positions)

    def _build_prompt(self, market_data_list: list[dict], account_state: dict, positions: dict, recent_decisions: list[dict]) -> str:
        """Constructs a comprehensive system prompt and state description.
        Handles both equities and crypto assets with appropriate rules for each.
        """
        
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
- Intraday VWAP: ${ind.get('vwap')}
- VWAP Upper Band (+1σ): ${ind.get('vwap_upper_1')}
- VWAP Lower Band (-1σ): ${ind.get('vwap_lower_1')}
- VWAP Upper Band (+2σ): ${ind.get('vwap_upper_2')}
- VWAP Lower Band (-2σ): ${ind.get('vwap_lower_2')}
- Price Distance from VWAP: {ind.get('vwap_dist_pct')}%
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

        active_symbols = list(dict.fromkeys(
            data.get("symbol", "").upper()
            for data in market_data_list
            if data and data.get("symbol")
        ))
        allowed_symbols_str = " | ".join([f'"{sym}"' for sym in active_symbols]) + ' | ""'

        # Options guidance block (only relevant when options trading is enabled)
        options_instructions = ""
        if getattr(config, "OPTIONS_ENABLED", False):
            universe_str = " | ".join(f'"{s}"' for s in getattr(config, "OPTIONS_UNIVERSE", []))
            options_instructions = f"""
        OPTIONS-ENABLED RULES (LONG CALLS & PUTS ONLY):
        1. You express a DIRECTIONAL VIEW + CONVICTION score. You do NOT choose the instrument.
        2. Output "direction": "bullish"/"bearish"/"neutral" and "conviction": 0.0-1.0.
        3. High conviction (>= {config.OPTIONS_CONVICTION_THRESHOLD}) on a symbol in the options universe may be expressed via options (leverage). Lower conviction uses shares.
        4. If conviction is high and the symbol is in the options universe, you may optionally request a specific DTE range via "option_dte_min" / "option_dte_max" (default {config.OPTIONS_DTE_MIN}-{config.OPTIONS_DTE_MAX} days) and OTM% via "option_strike_otm_pct".
        5. OPTIONS UNIVERSE: {universe_str}. Only symbols in this set are eligible for options.
        6. For a SELL of an existing option position, set direction opposite your view and keep conviction high; the executor sells the held contracts (never go short).
        7. NARRATION: In "thought_process", always explicitly explain WHY you chose your conviction level and, for an options-universe symbol, whether you are leaning toward shares vs options (leverage) and why (e.g. trend strength, DTE compatibility, risk appetite). Be specific — mention the actual conviction number you are assigning and the stock-vs-option intent.
        """
        
        # Add crypto-specific instructions to the system prompt
        crypto_instructions = """
        CRYPTO-SPECIFIC RULES:
        1. Trade 24/7 with no market close restrictions
        2. Use tighter stop losses (3-5% vs 5-7% for equities)
        3. Expect higher volatility - adjust position sizes accordingly
        4. Watch for weekend gaps and news-driven spikes
        5. Use OCO (One-Cancels-Other) orders when possible
        """
        
        system_instruction = f"""
{crypto_instructions}
{options_instructions}
ROLE:
You are an elite, professional, risk-averse financial quantitative trading agent. Your objective is to formulate an independent high-conviction trade choice (BUY, SELL, or HOLD) for EVERY ticker in the provided market data. You output a "decisions" array with one decision object per appraised ticker.

DIRECTIONS:
1. Analyze the technical indicators (RSI, Moving Averages, MACD, Bollinger Bands, and intraday VWAP with standard deviation ±1σ and ±2σ bands) to judge trends, support/resistance, and overbought/oversold levels. Target buying below VWAP and selling above it, flagging standard deviation stretches of >= ±2σ as highly overextended mean-reversion setups.
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
8. You MUST output a decision object for EVERY ticker provided in the market data (even HOLDs with quantity 0). Do not combine tickers into a single decision.
9. YOU MUST COMPLY WITH THE MANDATORY TRADING RULE SPECIFIED FOR EACH TICKER. DO NOT FORMULATE A DECISION THAT CONTRADICTS THESE RULES.
10. STATE YOUR CONVICTION & INSTRUMENT INTENT: For each ticker, clearly justify its conviction score in "thought_process" (e.g. "Confluence of support + oversold RSI -> conviction 0.8"). For any options-universe symbol, explicitly state whether you'd prefer shares vs options (leverage), and why. This decisioning narrative is displayed to the operator and must be specific and actionable.

OUTPUT FORMAT:
You must reply with a valid JSON object ONLY. Do not wrap in markdown blocks other than standard JSON mime type. The output is an object with a "decisions" array (one element per ticker).
JSON Schema:
{{
  "decisions": [
    {{
      "thought_process": "Per-ticker reasoning: indicators, news, anchors, trade sizing, compliance with the mandatory rule, an explicit justification of your conviction score, and (for options-universe symbols) whether you lean toward shares vs options and why.",
      "action": "BUY" | "SELL" | "HOLD",
      "symbol": {allowed_symbols_str},
      "quantity": float (number of shares, use 0 for HOLD),
      "take_profit_price": float or null,
      "stop_loss_price": float or null,
      "direction": "bullish" or "bearish" or "neutral",
      "conviction": float 0.0-1.0,
      "option_dte_min": integer or null,
      "option_dte_max": integer or null,
      "option_strike_otm_pct": float or null
    }}
  ]
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
        """Fallback rule-based trading agent (useful for testing or if LLM config is missing).

        The fallback is a CONSERVATIVE safety net: it NEVER opens new positions
        (no BUYs, no options). It only SELLs to de-risk an existing overbought
        holding, or HOLDs. This guarantees the fallback can never take on new
        risk (e.g. buying an option contract) when the LLM is unavailable.
        """
        logger.info("Running rule-based strategy fallback.")

        # Simple rule-based strategy:
        # - RSI > 62 and owned -> SELL half (overbought, de-risk)
        # - otherwise -> HOLD (never BUY)
        # Emits a LIST of per-ticker decisions (Option B).
        decisions = []

        for data in market_data_list:
            if not data:
                continue
            symbol = data["symbol"]
            current_price = data["current_price"]
            ind = data.get("indicators", {})
            rsi = ind.get("rsi_14", 50)
            if rsi is None:
                rsi = 50

            direction = "neutral"
            conviction = 0.5

            if rsi > 62 and symbol in positions:
                owned_qty = positions[symbol]["qty"]
                qty = max(1.0, int(owned_qty * 0.5))  # sell half
                decisions.append({
                    "thought_process": f"[Rule Fallback] {symbol} RSI is overbought ({rsi:.1f}); selling {qty} shares.",
                    "action": "SELL", "symbol": symbol, "quantity": float(qty),
                    "take_profit_price": None, "stop_loss_price": None,
                    "direction": "bearish", "conviction": 0.6,
                })
            else:
                # The fallback NEVER buys. Oversold is a HOLD, not a BUY, so the
                # fallback can never open a new position (or an option contract).
                decisions.append({
                    "thought_process": f"[Rule Fallback] {symbol} has no clear signal (RSI {rsi:.1f}). Holding.",
                    "action": "HOLD", "symbol": symbol, "quantity": 0.0,
                    "take_profit_price": None, "stop_loss_price": None,
                    "direction": direction, "conviction": conviction,
                })

        if not decisions:
            decisions.append({
                "thought_process": "[Rule Fallback] No appraised tickers. Holding.",
                "action": "HOLD", "symbol": "", "quantity": 0.0,
                "take_profit_price": None, "stop_loss_price": None,
                "direction": "neutral", "conviction": 0.5,
            })
        return decisions
