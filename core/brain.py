#!/usr/bin/env python3
"""Blog writing brain — persona-swappable LLM layer for Treat Motivated Capital.

Ports dexter's ``blog_bot/dexter_brain.py`` into agent-trade's conventions:

- Uses agent-trade's existing ``core.llm_client.SharedLLMClient`` as the transport
  (OpenRouter primary + Gemini fallback, wall-clock budget, bounded retries) instead
  of dexter's hand-rolled client. This removes duplicated code.
- Reads the active persona from ``core.config.BLOG_PERSONA`` (default "dexter") so
  voices are swappable at runtime with no code change. See ``core/personas.py``.

Function surface kept for the blog runner (``tools/blog_update.py``):
    format_pnl, generate_blog_intro, generate_trade_blurb, setup_dexter_logging

BRANDING: prompts never reference internal systems; the persona + branding rules
live in ``core/personas.py``. Readers only ever see Treat Motivated Capital.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from core.config import BLOG_PERSONA  # active persona name ("dexter" default)
from core.personas import PERSONAS
from core.llm_client import SharedLLMClient

# Creative-writing models (OpenRouter), primary -> fallback.
OPENROUTER_MODEL_LIST = [
    "nousresearch/hermes-3-llama-3.1-70b",
    "mistralai/mistral-large-2407",
]

logger = logging.getLogger("BlogBrain")

# One shared, lazily-created client (reuses agent-trade's fallback logic).
_client: SharedLLMClient | None = None
_client_attempted = False


def _get_client() -> SharedLLMClient | None:
    """Build the SharedLLMClient once. Returns None if it can't be constructed."""
    global _client, _client_attempted
    if _client_attempted:
        return _client
    _client_attempted = True
    try:
        _client = SharedLLMClient()
    except Exception as e:
        logger.error("BlogBrain: failed to init SharedLLMClient: %s", e)
        _client = None
    return _client


def setup_dexter_logging():
    """Set up a timestamped log file under ``logs/blog_bot/``.

    Port of dexter's logger so the runner gets the same "brain > ..." console log.
    """
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "blog_bot")
    os.makedirs(log_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"dexter_log_{ts}.txt")

    lgr = logging.getLogger("DexterBrain")
    lgr.setLevel(logging.DEBUG)
    if lgr.hasHandlers():
        lgr.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("brain > %(message)s"))
    lgr.addHandler(fh)
    lgr.addHandler(ch)
    logger.info("BlogBrain logger initialized: %s", log_file)
    return lgr


# ---------------------------------------------------------------------------
# Rate-limit guard (gentle, in-process)
# ---------------------------------------------------------------------------
_api_call_times: list[float] = []
_api_rate_limit = 10  # max calls per minute


def _check_rate_limit() -> bool:
    global _api_call_times
    now = time.time()
    _api_call_times = [t for t in _api_call_times if now - t < 60]
    return len(_api_call_times) < _api_rate_limit


def _record_api_call():
    global _api_call_times
    _api_call_times.append(time.time())


# ---------------------------------------------------------------------------
# Style-transfer core
# ---------------------------------------------------------------------------
def _apply_persona(raw_content: str, task_instruction: str) -> str:
    """Send [persona + task + raw content] to the LLM and return the styled text.

    Uses the persona selected by ``BLOG_PERSONA``. Falls back to returning the
    raw content on failure (like the original) so the runner never hard-crashes.
    """
    persona_def = PERSONAS.get(BLOG_PERSONA, PERSONAS["dexter"])
    full_system = (
        "You write blog content for the public blog. "
        "BRANDING: never mention any internal systems, code names, or how the "
        "numbers are produced. The blog is simply 'Treat Motivated Capital' and "
        "you are its voice. Follow the persona exactly.\n\nPERSONA:\n" + persona_def
    )
    full_prompt = (
        f"RAW DATA / CONTEXT:\n\"{raw_content}\"\n\n"
        f"YOUR TASK:\n{task_instruction}"
    )

    client = _get_client()
    if client is None:
        logger.error("No LLM client available; returning raw content.")
        return raw_content

    if not _check_rate_limit():
        logger.warning("Rate limit active; returning raw content.")
        return raw_content

    resolved_model = os.getenv("BLOG_MODEL", OPENROUTER_MODEL_LIST[0])
    started = time.time()
    try:
        result = client._execute_completion(
            prompt=full_prompt,
            system_prompt=full_system,
            tier="utility",          # cheap creative tier
            max_output_tokens=int(os.getenv("BLOG_MAX_OUTPUT_TOKENS", "1024")),
        )
        _record_api_call()
        # NOTE: _execute_completion doesn't take an explicit model override; if we
        # need BLOG_MODEL respected exactly, construct the client per-call model
        # via a small wrapper. For now the tier mapping drives the model.
        elapsed = time.time() - started
        logger.debug("BlogBrain LLM call took %.1fs", elapsed)
        return result.strip() if result else raw_content
    except Exception as e:
        logger.error("BlogBrain LLM call failed: %s", e)
        return raw_content


# ---------------------------------------------------------------------------
# Public: content generators
# ---------------------------------------------------------------------------
def format_pnl(val) -> str:
    """Round PnL to nearest dollar with proper negative placement.
    e.g. -4500.20 -> -$4,500 ; 4500.20 -> $4,500
    """
    try:
        rounded = round(float(val))
        return f"${rounded:,}" if rounded >= 0 else f"-${abs(rounded):,}"
    except (ValueError, TypeError):
        return str(val)


def generate_blog_intro(date_str, pnl, context_payload, active_tickers) -> str:
    """Generate the blog intro (Title / Meta / Body) in the active persona's voice."""
    logger.info("Generating blog intro (%s persona) for %s...", BLOG_PERSONA, date_str)
    formatted_pnl = format_pnl(pnl)
    raw = (f"Date: {date_str} | PnL: {formatted_pnl} | "
           f"Context & News: {context_payload} | Tickers: {active_tickers}")
    task = """
Write the Blog Intro using the persona above.
1. PRIORITY 1: If the context has "DAILY NOTES" or "SPECIAL INSTRUCTIONS", write about that topic FIRST.
2. Discuss the overall market news at a high level and how it impacted the day.
3. MANDATORY LIMIT: Keep the intro to 3-4 sentences maximum.
4. Focus on: What news drove performance? What was the total P/L? State the total P/L exactly as supplied (rounded to nearest dollar).
5. Stay in the persona's voice the whole time. Keep the market discussion macro and simple — do NOT explain technical trading terms (EMAs, Crosses).
6. FORMATTING: Strictly DO NOT use markdown bolding (**).
7. Output format must be strictly:
   TITLE: [Title]
   META: [SEO Description]
   BODY: [The content]
"""
    return _apply_persona(raw, task)


def generate_trade_blurb(ticker, pnl, logs, grade_info=None) -> str:
    """Generate a per-ticker trade analysis paragraph in the active persona's voice."""
    logger.info("Generating trade blurb (%s persona) for %s...", BLOG_PERSONA, ticker)
    grade_context = ""
    grade_instruction = ""
    if grade_info:
        grade_context = (
            f"\n\n--- TRADE GRADER INFO ---\n"
            f"Assigned Letter Grade: {grade_info.get('grade')}\n"
            f"Composite Score: {grade_info.get('composite_score'):.1f}/100\n"
            f"Alpha vs SPY: {grade_info.get('alpha_vs_spy'):+.2f}%\n"
            f"Alpha vs Sector: {grade_info.get('alpha_vs_sector'):+.2f}%\n"
            f"MAE Drawdown: {grade_info.get('mae_pct'):+.2f}%\n"
            f"MFE Capture: {grade_info.get('capture_ratio'):.1f}%\n"
        )
        grade_instruction = (
            "\n6. TRADE GRADER INTEGRATION: explicitly reference and critique the "
            "assigned grade and score. Explain the logic in simple terms for readers "
            "learning about stocks, connecting it to execution (e.g. why a lower grade "
            "because we didn't sell near the peak, or higher for strong relative strength). "
            "You may refer to Mom/Dad in the third person for warmth, but never address "
            "anyone directly. Keep the whole blurb to 2-3 sentences max."
        )

    formatted_pnl = format_pnl(pnl)
    raw = f"Ticker: {ticker} | PnL: {formatted_pnl} | Logs: {logs}{grade_context}"

    task = f"""
Analyze the trade for {ticker} in the persona's voice.
1. MANDATORY LIMIT: Keep the analysis to 2-3 sentences maximum.
2. High-level explanation using the provided data: bought at [price], held for [duration], sold at [price]. If multiple trades, summarize concisely.
3. FORBIDDEN: No play-by-play of logs or deep technical detail.
4. NO INTRODUCTIONS: Do not greet the reader or introduce yourself — dive straight into the analysis.
5. Use ONE high-quality, playful-but-professional metaphor, varied naturally. Do not repeat stock phrases.
6. FORMATTING: Strictly DO NOT use markdown bolding (**).{grade_instruction}
"""
    return _apply_persona(raw, task)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("active persona:", BLOG_PERSONA)
    print("example pnl:", format_pnl(-4500.20), "/", format_pnl(4500.20))