import os
import json
import logging
import argparse
import sys
from datetime import datetime

from core import config
from core import database
from core.alpaca_client import AlpacaClient
from core.strategy_rules import is_crypto_symbol, validate_strategy_rule
from core.feedback import feedback_text, next_strategy_version

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


def build_strategy_universe(positions: dict, watchlist_tickers: list[str]) -> list[str]:
    """Return every configured, held, and watched symbol once in stable order.

    OCC option contracts (e.g. ``NVDA261016C00230000``) are excluded: the
    strategist generates *stock* strategy rules, and option positions are
    managed by the option lifecycle/executor instead. Including a contract here
    would try to fetch stock bars for it (Alpaca rejects with ``invalid
    symbol``) and would generate a nonsensical stock rule for a contract.
    """
    from core.guardrails import is_occ_symbol
    return list(dict.fromkeys(
        ticker.upper()
        for ticker in [*config.TRADING_UNIVERSE, *positions.keys(), *watchlist_tickers]
        if not is_occ_symbol(ticker)
    ))

class MetaStrategist:
    """The high-level portfolio strategist that runs daily to audit performance and generate dynamic rules."""
    
    def __init__(self):
        self.provider = getattr(config, "LLM_PROVIDER", "gemini").lower()
        self.is_mock = False
        # Selected OpenRouter model id for THIS run (None => tier_mapping default).
        self.ab_model = None
        self.ab_label = getattr(config, "STRATEGIST_AB_LABEL", "experiment")
        
        if self.provider == "openrouter":
            try:
                from core.llm_client import SharedLLMClient
                self.llm_client = SharedLLMClient()
                # Pick this run's model from the A/B list, if configured.
                self.ab_model = self._pick_ab_model()
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

    @staticmethod
    def _pick_ab_model() -> str | None:
        """Round-robin select an OpenRouter model for this strategist run.

        If STRATEGIST_AB_MODELS is a comma-separated list of models, alternate
        between them day-to-day (based on UTC date) and return the chosen model id.
        Returns None to use the configured tier default (no experiment).
        """
        raw = getattr(config, "STRATEGIST_AB_MODELS", "") or ""
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if len(models) < 2:
            return None
        # Deterministic day-alternation: even/odd UTC date -> first/second model.
        day_index = datetime.utcnow().date().toordinal() % len(models)
        return models[day_index]

    def run_daily_strategy_refinement(self, alpaca_client: AlpacaClient):
        """Runs the strategist routine for all relevant tickers (universe, holdings, and watchlist)."""
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

        # Load watchlist tickers from screener pool
        watchlist_tickers = []
        try:
            from core.screener import load_screener_pool
            watchlist_tickers = load_screener_pool()
        except Exception as se:
            logger.warning(f"Could not load screener pool for daily strategist: {se}")

        # Combine all relevant tickers: TRADING_UNIVERSE + active positions + watchlist tickers
        all_tickers = build_strategy_universe(positions, watchlist_tickers)

        # 3. Process each relevant ticker
        for ticker in all_tickers:
            logger.info(f"Analyzing daily regime for {ticker}...")
            
            # A. Fetch historical bar data (last 30 daily candles)
            try:
                bars_df = alpaca_client.get_historical_bars(ticker, limit=30)
                bars_summary = self._summarize_bars(bars_df)
            except Exception as e:
                logger.error(f"Failed to fetch market data for {ticker}: {e}")
                continue

            # B. Get active strategy rules from yesterday (or the explicit missing-rule notice)
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

            todays_rules = result.get("todays_rules", "").strip()
            is_valid, validation_reason = validate_strategy_rule(ticker, todays_rules)
            if not is_valid:
                logger.error(
                    f"Strategist generated an invalid rule for {ticker} "
                    f"({validation_reason}); leaving the active history unchanged."
                )
                continue
            
            # D. Log the new strategy into history. Embed the authoring model in
            # the version tag so the A/B harness can split outcomes by model.
            authored_by = result.get("ab_model") or (getattr(config, "STRATEGIST_MODEL_TIER", "heavyweight"))
            model_tag = authored_by.replace("/", "-").replace("_", "-") if authored_by else "unknown"
            db_id = database.log_strategy_history(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                todays_rules=todays_rules,
                meta_reasoning=result["meta_reasoning"],
                strategy_version=f"{next_strategy_version()}|model={model_tag}"
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
        """Sends the contextual prompt to Gemini/OpenRouter and parses the structured response."""
        if self.is_mock:
            return self._generate_mock_refinement(ticker, yesterdays_rules)
            
        # Structured per-ticker + global feedback (decayed) from core.feedback
        perf_summary_str = feedback_text(
            symbol=ticker,
            lookback_days=30,
            half_life_days=14,
        )

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

        asset_context = (
            f"{ticker} is a 24/7 crypto asset. The rule must govern {ticker} itself and must not "
            "require SPY, QQQ, or equity-market data to permit a crypto action."
            if is_crypto_symbol(ticker)
            else f"{ticker} is an equity asset and may be traded only during its market window."
        )

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

=== REQUIRED ASSET SCOPE ===
{asset_context}

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
4. Review the STRUCTURED PERFORMANCE FEEDBACK section carefully. Your rules MUST adapt based on these concrete, decay-weighted outcomes:
   - If the "<4h (whipsaw)" bucket dominates the symbol's holding-time buckets, treat it as a strong signal that entries are being whipsawed. Your new rule MUST add a faster regime filter / require stronger intraday-breakout confirmation (e.g. price must close above a VWAP band or a swing-high before a BUY). Do NOT just restate yesterday's rule.
   - If the symbol's profit factor is < 1.0 or expectancy is negative, design more conservative rules: smaller starter size, tighter entry thresholds, and require confluences of Fibonacci support / psychological round numbers before triggers.
   - If max historical drawdown is high (>15%), prioritize capital preservation (defensive 1-3% sizing and a larger cash buffer).
   - Ensure the execution agent strictly avoids repeating previous errors or whipsaws.
5. Write a single, highly refined paragraph that applies ONLY to {ticker}. YOU MUST INCLUDE AT LEAST ONE CONDITIONAL "IF/THEN" threshold for {ticker} based on price, intraday VWAP, or vwap_dist_pct. Do not substitute another ticker in the operative instruction.
6. The new rule MUST cite at least one CONCRETE, guardrail-adjustable knob (an intraday VWAP threshold, an RSI entry band, a max allocation %, or a holding-time exit) so the rule is testable against future round-trips. State that knob explicitly (e.g. "IF vwap_dist_pct > +1.5% THEN no new BUY").
6. For any crypto ticker, preserve 24/7 operation and use volatility-appropriate thresholds without requiring equity-index data.

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
                    tier=config.STRATEGIST_MODEL_TIER,
                    explicit_model=self.ab_model
                )
                return {
                    "meta_reasoning": result.get("meta_reasoning", "Maintained previous rule set due to matching market trend."),
                    "todays_rules": result.get("todays_rules", yesterdays_rules),
                    "ab_model": self.ab_model,
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

    def run_single_ticker_refinement(self, ticker: str, alpaca_client: AlpacaClient) -> bool:
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

            todays_rules = result.get("todays_rules", "").strip()
            is_valid, validation_reason = validate_strategy_rule(ticker, todays_rules)
            if not is_valid:
                logger.error(
                    f"Emergency strategist generated an invalid rule for {ticker} "
                    f"({validation_reason})."
                )
                return False
            
            db_id = database.log_strategy_history(
                ticker=ticker,
                yesterdays_rules=yesterdays_rules,
                todays_rules=todays_rules,
                meta_reasoning=f"[EMERGENCY INTRADAY RE-EVALUATION] {result['meta_reasoning']}",
                strategy_version=f"{next_strategy_version()}|model={result.get('ab_model') or 'default'}"
            )
            logger.info(f"Emergency rules updated for {ticker}. DB ID: {db_id}")
            logger.info(f"Emergency Rules: {todays_rules}\n")
            return True
        except Exception as e:
            logger.error(f"Failed to run emergency refinement for {ticker}: {e}")
            return False

    # ------------------------------------------------------------------
    # OPTIONS STRATEGY TRACK
    # ------------------------------------------------------------------
    def run_option_strategy_refinement(self, alpaca_client: AlpacaClient) -> list[str]:
        """Runs a dedicated options-strategy refinement for every underlying with
        option exposure (held option positions OR options-universe config).

        Options are LEVERAGED instruments with their own instrument knobs (conviction
        threshold, DTE window, OTM% band, max contracts/allocation). Their strategy
        rules are stored under the special ticker key ``OPTIONS/<UNDERLYING>`` and are
        tuned SEPARATELY from the stock track, so leverage risk is learned and managed
        on its own curve. Returns the list of underlyings refined.
        """
        # Determine which underlyings to tune for options.
        underlyings = []
        seen = set()
        for u in getattr(config, "OPTIONS_UNIVERSE", []) or []:
            if u and u.upper() not in seen:
                underlyings.append(u.upper())
                seen.add(u.upper())

        # Always include underlyings with current option holdings (even if they
        # fell out of the static universe) so open leverage is always managed.
        try:
            positions = alpaca_client.get_positions()
            from core.feedback import is_option_contract_symbol, option_underlying
            for sym in positions:
                if is_option_contract_symbol(sym):
                    u = option_underlying(sym)
                    if u and u not in seen:
                        underlyings.append(u)
                        seen.add(u)
        except Exception as e:
            logger.warning(f"Could not scan option positions for strategist: {e}")

        refined = []
        for underlying in underlyings:
            try:
                key = f"OPTIONS/{underlying}"
                logger.info(f"Analyzing OPTIONS strategy for {underlying}...")
                yesterdays_rules = database.get_active_strategy(key)
                options_fb = self._options_feedback_text(underlying)
                bars_summary = ""
                try:
                    bars_df = alpaca_client.get_historical_bars(underlying, limit=30)
                    bars_summary = self._summarize_bars(bars_df)
                except Exception as be:
                    logger.warning(f"No stock bars for {underlying} options track: {be}")

                result = self._optimize_option_strategy_for_underlying(
                    underlying=underlying,
                    yesterdays_rules=yesterdays_rules,
                    bars_summary=bars_summary,
                    options_feedback=options_fb,
                    account_state=alpaca_client.get_account_state(),
                )
                todays_rules = result.get("todays_rules", "").strip()
                if not todays_rules or todays_rules.startswith("No active strategy"):
                    logger.error(f"Options strategist produced empty rule for {underlying}.")
                    continue
                authored_by = result.get("ab_model") or getattr(config, "STRATEGIST_MODEL_TIER", "heavyweight")
                model_tag = authored_by.replace("/", "-").replace("_", "-") if authored_by else "unknown"
                db_id = database.log_strategy_history(
                    ticker=key,
                    yesterdays_rules=yesterdays_rules,
                    todays_rules=todays_rules,
                    meta_reasoning=result["meta_reasoning"],
                    strategy_version=f"{next_strategy_version()}|model={model_tag}|track=options"
                )
                logger.info(f"OPTIONS strategy updated for {underlying} (key={key}). DB ID: {db_id}")
                logger.info(f"OPTIONS Rules: {todays_rules}")
                refined.append(underlying)
            except Exception as e:
                logger.error(f"Options strategy refinement failed for {underlying}: {e}")
        return refined

    def _options_feedback_text(self, underlying: str) -> str:
        """Options-specific performance feedback for one underlying."""
        try:
            from core.feedback import (
                is_option_contract_symbol, option_underlying,
                options_feedback, format_options_feedback,
            )
            fb = options_feedback()
            # Narrow to the requested underlying.
            underlying_fb = {
                "n_closed": fb["n_closed"],
                "total_pnl": fb["total_pnl"],
                "by_underlying": {
                    u: st for u, st in fb.get("by_underlying", {}).items()
                    if u == underlying
                },
            }
            return format_options_feedback(underlying_fb)
        except Exception as e:
            logger.warning(f"Options feedback unavailable for {underlying}: {e}")
            return "No options feedback available."

    def _optimize_option_strategy_for_underlying(self, underlying: str, yesterdays_rules: str,
                                                 bars_summary: str, options_feedback: str,
                                                 account_state: dict) -> dict:
        """Sends an options-focused prompt to the LLM and parses the rule output.

        Returns a dict with ``meta_reasoning`` and ``todays_rules``. The rule must
        govern OPTION trading for the underlying (leverage), NOT stock trading.
        """
        if self.is_mock:
            return {
                "meta_reasoning": f"[Mock Strategist] Holding options exposure for {underlying} under existing config.",
                "todays_rules": (yesterdays_rules if not yesterdays_rules.startswith("No active")
                                 else f"Trade options on {underlying} only with conviction >= 0.7, DTE 30-45, OTM 1-10%, max contracts per allocation."),
            }

        prompt = f"""
ROLE:
You are the Options Risk & Strategy Specialist for an elite AI trading desk. Your job is to write a concise, high-impact **OPTIONS TRADING STRATEGY RULE** paragraph for the underlying **{underlying}**. This governs OPTIONS (long leveraged calls/puts) ONLY — NOT share trades. Options carry a 100x contract multiplier, so leverage discipline is paramount.

=== SYSTEM CONTEXT ===
- Total Portfolio Net Equity: ${account_state.get('equity', 0.0):,.2f}
- Options universe includes: {getattr(config, 'OPTIONS_UNIVERSE', [])}
- Options config knobs you may reference/adjust:
  - OPTIONS_CONVICTION_THRESHOLD (default {getattr(config, 'OPTIONS_CONVICTION_THRESHOLD', 0.7)}): routing gate to the option path.
  - OPTIONS_DTE_MIN/MAX (default {getattr(config, 'OPTIONS_DTE_MIN', 30)}-{getattr(config, 'OPTIONS_DTE_MAX', 45)}): contract days-to-expiry window.
  - OPTIONS_OTM_PERCENT_MIN/MAX (default {getattr(config, 'OPTIONS_OTM_PERCENT_MIN', 0.01)}-{getattr(config, 'OPTIONS_OTM_PERCENT_MAX', 0.10)}): strike out-of-the-money band.
  - OPTIONS_MAX_ALLOCATION_PCT (default {getattr(config, 'OPTIONS_MAX_ALLOCATION_PCT', 0.05)}): max equity % per option position.
  - OPTIONS_MAX_CONTRACTS_PER_TICKER (default {getattr(config, 'OPTIONS_MAX_CONTRACTS_PER_TICKER', 5)}): per-underlying contract cap.

=== OPTIONS PERFORMANCE FEEDBACK (LEVERAGE LEARNING) ===
{options_feedback}

=== UNDERLYING MARKET REGIME (Last 30 Days) ===
{bars_summary if bars_summary else "No bars available."}

=== YESTERDAY'S OPTIONS STRATEGY RULES ===
"{yesterdays_rules}"

DIRECTIONS:
1. Review the options performance feedback. If options on {underlying} are losing money (negative decayed PnL) or underperforming, you MUST tighten option-specific knobs: raise the conviction threshold, shrink DTE/OTM windows, cut the max allocation/contracts, or de-prioritize the option route in favor of shares.
2. If the underlying's option gamma/leverage exposure is risky (high IV, pinned near strike, large drawdowns on the contract), reduce contracts or require a higher conviction before opening leverage.
3. Write ONE conditional "IF/THEN" paragraph governing OPTIONS on {underlying}. It MUST reference at least one concrete option knob (conviction threshold, DTE, OTM%, max allocation %, max contracts) so it is testable against future option round-trips.
4. The rule applies ONLY to option trading for {underlying}; do NOT describe share strategies. Keep it under 3 sentences.

OUTPUT FORMAT:
Your response must be a single, valid JSON object ONLY.
Schema:
{{
  "meta_reasoning": "Your analytical breakdown of the options performance feedback and the option-specific knob tuning you're recommending.",
  "todays_rules": "The concise options trading rule paragraph for {underlying}. Must be under 3 sentences and include at least one option-specific IF/THEN knob."
}}
"""

        if self.provider == "openrouter":
            try:
                from pydantic import BaseModel, Field

                class OptionStrategistResponse(BaseModel):
                    meta_reasoning: str = Field(description="Options performance analysis and knob tuning.")
                    todays_rules: str = Field(description="Options trading rule paragraph for the underlying.")

                result = self.llm_client.generate_structured(
                    prompt=prompt,
                    response_model=OptionStrategistResponse,
                    tier=config.STRATEGIST_MODEL_TIER,
                    explicit_model=self.ab_model,
                )
                return {
                    "meta_reasoning": result.get("meta_reasoning", "Maintained prior options rule."),
                    "todays_rules": result.get("todays_rules", yesterdays_rules),
                    "ab_model": self.ab_model,
                }
            except Exception as e:
                logger.error(f"Error calling OpenRouter options strategist for {underlying}: {e}. Using yesterday's rule.")
                return {"meta_reasoning": f"Options strategist call failed: {e}. Falling back.", "todays_rules": yesterdays_rules}
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
                        required=["meta_reasoning", "todays_rules"],
                    ),
                ),
            )
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
                "meta_reasoning": result.get("meta_reasoning", "Maintained prior options rule."),
                "todays_rules": result.get("todays_rules", yesterdays_rules),
            }
        except Exception as e:
            logger.error(f"Error calling options strategist for {underlying}: {e}. Using yesterday's rule.")
            return {"meta_reasoning": f"Options strategist call failed: {e}. Falling back.", "todays_rules": yesterdays_rules}

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
        # Run the dedicated OPTIONS strategy track so leveraged positions are
        # tuned on their own curve.
        try:
            refined_opts = strategist.run_option_strategy_refinement(alpaca_client)
            logger.info(f"Daily OPTIONS strategist cycle complete. Refined: {refined_opts}")
        except Exception as opt_err:
            logger.error(f"Daily OPTIONS strategist cycle failed: {opt_err}")
        logger.info("Daily strategist cycle complete.")

if __name__ == "__main__":
    main()
