import os
import json
import logging
import argparse
import sys
from datetime import datetime

from core import config
from core import database
from core.alpaca_client import AlpacaClient

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] MetaStrategist: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE)
    ]
)
from core import logger_setup
logger_setup.setup_logging(app_name="agent-trade", env="production")
logger = logging.getLogger("MetaStrategist")

class MetaStrategist:
    """The high-level portfolio strategist that runs daily to audit performance and generate dynamic rules."""
    
    def __init__(self):
        self.provider = getattr(config, "LLM_PROVIDER", "gemini").lower()
        self.is_mock = False
        
        if self.provider == "openrouter":
            try:
                from core.llm_client import SharedLLMClient
                self.llm_client = SharedLLMClient()
                logger.info("Initialized OpenRouter SharedLLMClient for Meta-Strategist (Heavyweight Tier).")
            except Exception as e:
                logger.error(f"Failed to initialize OpenRouter SharedLLMClient: {e}. Falling back to rule-based strategist.")
                self.is_mock = True
        else:
            self.api_key = config.GEMINI_API_KEY
            self.model_name = config.GEMINI_MODEL
            if not GENAI_AVAILABLE:
                logger.warning("google-genai package is not installed. Falling back to rule-based strategist.")
                self.is_mock = True
            elif not self.api_key or self.api_key == "your_gemini_api_key_here":
                logger.warning("Gemini API key is missing. Falling back to rule-based strategist.")
                self.is_mock = True
            else:
                try:
                    self.client = genai.Client(api_key=self.api_key)
                    logger.info(f"Initialized Gemini Meta-Strategist Brain with model {self.model_name}.")
                except Exception as e:
                    logger.error(f"Failed to initialize Gemini Client: {e}. Falling back to rule-based strategist.")
                    self.is_mock = True

    def run_daily_strategy_refinement(self, alpaca_client: AlpacaClient):
        """Runs the strategist routine for all tickers in the trading universe."""
        logger.info("Executing daily strategy review and rule refinement...")
        
        # 1. Fetch account & portfolio state
        try:
            account_state = alpaca_client.get_account_state()
            positions = alpaca_client.get_positions()
        except Exception as e:
            logger.error(f"Error fetching account state for strategist: {e}")
            return

        # 2. Fetch last 5 executed trades and last 5 decisions from DB for audit context
        try:
            recent_trades = database.get_recent_trades(limit=5)
            recent_decisions = database.get_recent_decisions(limit=5)
        except Exception as e:
            logger.error(f"Error fetching DB trade/decision records: {e}")
            recent_trades, recent_decisions = [], []

        # 3. Process each ticker in the universe
        for ticker in config.TRADING_UNIVERSE:
            logger.info(f"Analyzing daily regime for {ticker}...")
            
            # A. Fetch historical bar data (last 30 daily candles)
            try:
                bars_df = alpaca_client.get_historical_bars(ticker, limit=30)
                bars_summary = self._summarize_bars(bars_df)
            except Exception as e:
                logger.error(f"Failed to fetch market data for {ticker}: {e}")
                continue

            # B. Get active strategy rules from yesterday
            # Since get_active_strategy has a default fallback, this will always return a valid starting rule
            yesterdays_rules = database.get_active_strategy(ticker)
            
            # C. Generate refined strategy rules
            logger.info(f"Generating optimized rules for {ticker} using AI...")
            result = self._optimize_strategy_for_ticker(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                bars_summary=bars_summary,
                recent_trades=recent_trades,
                recent_decisions=recent_decisions,
                account_state=account_state,
                positions=positions
            )
            
            # D. Log the new strategy into history
            db_id = database.log_strategy_history(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                todays_rules=result["todays_rules"],
                meta_reasoning=result["meta_reasoning"]
            )
            
            logger.info(f"Strategy updated for {ticker}. Database ID: {db_id}")
            logger.info(f"Meta-Reasoning: {result['meta_reasoning']}")
            logger.info(f"New Rules: {result['todays_rules']}\n")

    def _summarize_bars(self, df) -> str:
        """Utility to convert recent candles into a dense textual representation."""
        if df.empty:
            return "No price history available."
        
        summary = ""
        # Show last 5 days individually
        summary += "Last 5 Days Close Prices:\n"
        for idx, row in df.tail(5).iterrows():
            date_str = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)
            summary += f"- {date_str}: Open: ${row['open']:.2f}, High: ${row['high']:.2f}, Low: ${row['low']:.2f}, Close: ${row['close']:.2f}, Vol: {int(row['volume']):,}\n"
            
        # Overall trend metrics
        closes = df["close"]
        price_change_pct = (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0] * 100
        volatility = closes.pct_change().std() * 100
        
        summary += f"\n30-Day Technical Overview:\n"
        summary += f"- 30-Day Price Change: {price_change_pct:.2f}%\n"
        summary += f"- 30-Day Volatility (std dev of daily returns): {volatility:.2f}%\n"
        summary += f"- 30-Day Max High: ${df['high'].max():.2f} | 30-Day Min Low: ${df['low'].min():.2f}\n"
        
        return summary

    def _optimize_strategy_for_ticker(self, ticker: str, yesterdays_rules: str, bars_summary: str,
                                      recent_trades: list, recent_decisions: list,
                                      account_state: dict, positions: dict) -> dict:
        """Sends the contextual prompt to Gemini and parses the structured response."""
        if self.is_mock:
            return self._generate_mock_refinement(ticker, yesterdays_rules)
            
        # Fetch historical successes/failures performance summary
        perf_summary = database.get_performance_summary()
        perf_summary_str = perf_summary.get("text_summary", "No performance history available.")

        # Compile contextual lists
        trades_str = ""
        if not recent_trades:
            trades_str = "No trades executed recently."
        for t in recent_trades:
            if t.get("symbol") == ticker:
                trades_str += f"- Trade: {t.get('side').upper()} {t.get('qty')} shares, filled at ${t.get('filled_avg_price')}, status: {t.get('status')}\n"

        decisions_str = ""
        if not recent_decisions:
            decisions_str = "No recent ticks analyzed."
        for d in recent_decisions:
            decisions_str += f"- Tick: {d.get('proposed_action')} {d.get('proposed_qty')} {d.get('proposed_symbol')}, Approved: {d.get('is_approved')}, Logic: {d.get('thought_process')[:120]}...\n"

        current_position_str = "No active position in this ticker."
        if ticker in positions:
            p = positions[ticker]
            current_position_str = f"Currently holding {p['qty']} shares, value: ${p['market_value']:,.2f}, avg entry price: ${p['avg_entry_price']:,.2f}, unrealized PnL: ${p['unrealized_pnl']:,.2f}"

        prompt = f"""
ROLE:
You are the Lead Quantitative Portfolio Strategist for an elite AI trading desk. Your objective is to review the daily performance and market regime for ticker **{ticker}** and write a concise, high-impact **Trading Strategy Rule** paragraph for the Execution Brain to follow during its 15-minute tick loops.

=== SYSTEM CONTEXT ===
- Total Portfolio Net Equity: ${account_state.get('equity', 0.0):,.2f}
- Cash Balance: ${account_state.get('cash', 0.0):,.2f}
- Allowed Ticker Universe: {config.TRADING_UNIVERSE}

=== HISTORICAL PORTFOLIO PERFORMANCE (SUCCESS & FAILURE LEARNINGS) ===
{perf_summary_str}

=== MARKET REGIME FOR {ticker} (Last 30 Days) ===
{bars_summary}

=== ACTIVE HOLDINGS IN {ticker} ===
{current_position_str}

=== RECENT EXECUTED TRADES ===
{trades_str}

=== RECENT TICK-BY-TICK DECISIONS ===
{decisions_str}

=== YESTERDAY'S STRATEGY RULES ===
"{yesterdays_rules}"

DIRECTIONS:
1. Review the daily price candles, 30-day volatility, and trends. Is the ticker trending, range-bound, overbought, or oversold?
2. Audit the trade outcomes. Were our recent trades profitable? Did we experience whipsaws or losses?
3. Decide if yesterday's rules are working, or if they need adjustment to match the current market regime. 
4. Review the HISTORICAL PORTFOLIO PERFORMANCE section carefully. Your rules MUST adapt based on these successes and failures:
   - If historical win rate is low (<50%) or max drawdown is high (>15%), design significantly more conservative rules with tighter, safer conditional thresholds to avoid over-allocating on speculative moves.
   - If our historical realized profit is negative, suggest smaller starter positions and require stronger support confirmations (e.g. confluences of Fibonacci support and psychological round numbers) before triggers.
   - Ensure the execution agent strictly avoids repeating previous errors or whipsaws.
5. Write a single, highly refined paragraph of execution instructions. YOU MUST INCLUDE AT LEAST ONE CONDITIONAL "IF/THEN" THRESHOLD for risk control based on price, intraday VWAP, or vwap_dist_pct (e.g. "If SPY breaks below $740, halt all buying immediately", "If QQQ price stretches >= 2% above its VWAP (vwap_dist_pct >= 2.0), sell 50% to lock in gains", or "If SOL drops below $75, exit 50% of positions and wait"). This is a deterministic condition that the tick loop brain can evaluate dynamically. Be extremely specific.
6. If the ticker is Solana (SOL/USD), keep in mind it is a 24/7 crypto asset with higher volatility and wider swings than index stocks like SPY/QQQ. Suggest rules with slightly wider tolerance thresholds.

OUTPUT FORMAT:
Your response must be a single, valid JSON object ONLY.
Schema:
{{
  "meta_reasoning": "Your analytical breakdown of yesterday's performance, the technical trend regime, and your quant reasoning for keeping or modifying the strategy.",
  "todays_rules": "The concise, refined paragraph of trading rules for the execution loop. Must be under 3 sentences."
}}
"""

        if self.provider == "openrouter":
            try:
                from pydantic import BaseModel, Field
                
                class StrategistResponse(BaseModel):
                    meta_reasoning: str = Field(description="Analytical breakdown of yesterday's performance and market trend.")
                    todays_rules: str = Field(description="The concise paragraph of trading rules. Must be under 3 sentences and include at least one IF/THEN condition.")

                result = self.llm_client.generate_structured(
                    prompt=prompt,
                    response_model=StrategistResponse,
                    tier=config.STRATEGIST_MODEL_TIER
                )
                return {
                    "meta_reasoning": result.get("meta_reasoning", "Maintained previous rule set due to matching market trend."),
                    "todays_rules": result.get("todays_rules", yesterdays_rules)
                }
            except Exception as e:
                logger.error(f"Error calling OpenRouter AI strategist for {ticker}: {e}. Falling back to yesterday's rules.")
                try:
                    import sys
                    from agent_jira.jira_logger import log_exception
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    log_exception(
                        exc_type, exc_value, exc_tb,
                        app_name="agent-trade",
                        env="production",
                        metadata={"Ticker": ticker, "Context": "Daily Strategy Optimization Failure"}
                    )
                except Exception as ex:
                    logger.error(f"Failed to log exception to JIRA: {ex}")
                return {
                    "meta_reasoning": f"Failed to contact OpenRouter strategist AI client: {e}. Falling back to yesterday's guidelines.",
                    "todays_rules": yesterdays_rules
                }

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "meta_reasoning": types.Schema(type=types.Type.STRING),
                            "todays_rules": types.Schema(type=types.Type.STRING),
                        },
                        required=["meta_reasoning", "todays_rules"]
                    )
                )
            )
            
            # Clean up potential markdown code block wrappers
            text = response.text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()

            result = json.loads(text)
            return {
                "meta_reasoning": result.get("meta_reasoning", "Maintained previous rule set due to matching market trend."),
                "todays_rules": result.get("todays_rules", yesterdays_rules)
            }
        except Exception as e:
            logger.error(f"Error calling AI strategist for {ticker}: {e}. Falling back to yesterday's rules.")
            return {
                "meta_reasoning": f"Failed to contact strategist AI client: {e}. Falling back to yesterday's guidelines.",
                "todays_rules": yesterdays_rules
            }

    def run_single_ticker_refinement(self, ticker: str, alpaca_client: AlpacaClient):
        """Runs an emergency intraday strategy refinement for a single ticker."""
        ticker = ticker.upper()
        logger.info(f"[EMERGENCY] Intraday shock detected for {ticker}. Running immediate strategy re-evaluation...")
        
        try:
            account_state = alpaca_client.get_account_state()
            positions = alpaca_client.get_positions()
            recent_trades = database.get_recent_trades(limit=5)
            recent_decisions = database.get_recent_decisions(limit=5)
            
            bars_df = alpaca_client.get_historical_bars(ticker, limit=30)
            bars_summary = self._summarize_bars(bars_df)
            
            yesterdays_rules = database.get_active_strategy(ticker)
            
            result = self._optimize_strategy_for_ticker(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                bars_summary=bars_summary,
                recent_trades=recent_trades,
                recent_decisions=recent_decisions,
                account_state=account_state,
                positions=positions
            )
            
            db_id = database.log_strategy_history(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                todays_rules=result["todays_rules"],
                meta_reasoning=f"[EMERGENCY INTRADAY RE-EVALUATION] {result['meta_reasoning']}"
            )
            logger.info(f"Emergency rules updated for {ticker}. DB ID: {db_id}")
            logger.info(f"Emergency Rules: {result['todays_rules']}\n")
        except Exception as e:
            logger.error(f"Failed to run emergency refinement for {ticker}: {e}")

    def _generate_mock_refinement(self, ticker: str, yesterdays_rules: str) -> dict:
        """Fallback mock rules generator if no Gemini API key is configured."""
        logger.info("[Mock Mode] Strategist generating simple rule iteration...")
        return {
            "meta_reasoning": f"[Mock Strategist] Market is currently stable for {ticker}. Standard baseline trends continue. Minor RSI limits are sufficient.",
            "todays_rules": yesterdays_rules  # Maintain rule set in mock
        }

def main():
    parser = argparse.ArgumentParser(description="Alpaca AI Portfolio Meta-Strategist")
    parser.add_argument("--run", action="store_true", default=True, help="Execute daily strategy review loop.")
    args = parser.parse_args()

    logger.info("Initializing Meta-Strategist agent...")
    alpaca_client = AlpacaClient()
    strategist = MetaStrategist()

    if args.run:
        strategist.run_daily_strategy_refinement(alpaca_client)
        logger.info("Daily strategist cycle complete.")

if __name__ == "__main__":
    main()
