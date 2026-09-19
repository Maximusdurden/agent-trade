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

import json
import logging
import os
import re
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
# Output sanitizer — strips leaked chain-of-thought / safety artifacts
# ---------------------------------------------------------------------------
# Some free-tier / reasoning models emit their internal "thinking process" or
# safety-classification labels directly into the response body. These must never
# reach the published blog. This strips the common patterns defensively so a
# model regression can't leak internal notes to readers again.
#
# Two passes:
#   1. Regex-strip specific leaked labels (e.g. "User Safety: safe") that may be
#      appended to an otherwise-legitimate paragraph.
#   2. Drop any paragraph that is purely leaked reasoning (contains a reasoning
#      header like "thinking process" / "Analyze the Request"). These are always
#      internal and never legitimate blog content.
_LEAK_LABEL_RE = re.compile(
    r"(?:User\s+)?Safety\s*:\s*\w+\s*", re.IGNORECASE
)
_REASONING_MARKERS = [
    "thinking process", "my thinking", "let me think", "let me reason",
    "let me analyze", "brainstorm", "deconstruct constraints",
    "analyze the request", "analyze the trade",
    "persona traits", "system identity", "raw data / context", "your task",
    "sentence limit", "price/duration", "trade grader integration",
    "no introductions", "mandatory limit", "forbidden:",
]


def _sanitize_blog_output(text: str) -> str:
    """Remove leaked reasoning / safety artifacts from an LLM blog response.

    Returns the cleaned text. If cleaning leaves nothing meaningful, returns the
    original so the caller's fallback (raw content) still applies.
    """
    if not text:
        return text
    # Pass 1: strip leaked safety labels anywhere (they may share a paragraph
    # with legitimate content).
    text = _LEAK_LABEL_RE.sub("", text)
    # Pass 2: drop paragraphs that are purely leaked reasoning.
    paragraphs = re.split(r"\n\s*\n", text)
    kept = []
    for para in paragraphs:
        low = para.lower()
        if any(marker in low for marker in _REASONING_MARKERS):
            logger.debug("BlogBrain sanitizer dropped leaked paragraph: %s", para[:80])
            continue
        kept.append(para)
    cleaned = "\n\n".join(kept).strip()
    # If everything was dropped, return the original so the caller's fallback
    # (raw content) still applies rather than publishing an empty blurb.
    return cleaned if cleaned else text


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
            explicit_model=resolved_model,  # respect BLOG_MODEL exactly (see note)
        )
        _record_api_call()
        elapsed = time.time() - started
        logger.debug("BlogBrain LLM call took %.1fs", elapsed)
        return _sanitize_blog_output(result) if result else raw_content
    except Exception as e:
        logger.error("BlogBrain LLM call failed: %s", e)
        return raw_content


# ---------------------------------------------------------------------------
# JSON-tolerant output handling
#
# Some models (e.g. Hermes 3 via OpenRouter) occasionally return a JSON object
# instead of the requested plain-text format — e.g. for the intro they emit
# {"title": ..., "meta": ..., "body": ...} and for a blurb {"text": ..., "persona": ...}.
# These helpers robustly extract the JSON object (handling fences / embedded
# prose / literal control chars) so the caller can pull the field it needs and
# never publish raw JSON to the blog.
# ---------------------------------------------------------------------------
def _sanitize_control_chars_in_json(text: str) -> str:
    """Escape raw control chars inside JSON string values so json.loads works.

    Models often emit literal newlines/tabs inside string values, which are
    invalid in strict JSON. Structural whitespace between tokens is preserved.
    """
    result = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                result.append(ch)
                escape = False
                continue
            if ch == "\\":
                result.append(ch)
                escape = True
                continue
            if ch == '"':
                result.append(ch)
                in_string = False
                continue
            if ch == "\n":
                result.append("\\n")
                continue
            if ch == "\r":
                result.append("\\r")
                continue
            if ch == "\t":
                result.append("\\t")
                continue
            if ord(ch) < 32:
                result.append("\\u%04x" % ord(ch))
                continue
            result.append(ch)
        else:
            if ch == '"':
                in_string = True
            result.append(ch)
    return "".join(result)


def _extract_json_object(text: str) -> dict | None:
    """Robustly extract a JSON object from model output.

    Handles pure JSON, JSON wrapped in ```json ... ``` fences, or JSON embedded
    in surrounding prose. Returns the parsed dict, or None if no object found.
    """
    if not text:
        return None
    text = text.strip()

    def _try_parse(candidate: str) -> dict | None:
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        sanitized = _sanitize_control_chars_in_json(candidate)
        try:
            parsed = json.loads(sanitized)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return None

    parsed = _try_parse(text)
    if parsed:
        return parsed

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        parsed = _try_parse(fence_match.group(1))
        if parsed:
            return parsed

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        parsed = _try_parse(candidate)
                        if parsed:
                            return parsed
                        break
    return None


def _unwrap_json(text: str, *keys: str) -> str | None:
    """If ``text`` is (or contains) a JSON object, return the first present key's
    string value; otherwise return None. Used to normalize a JSON-returning model
    back to plain text for the blog.
    """
    obj = _extract_json_object(text)
    if not obj:
        return None
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


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
    # NOTE: Date and PnL are placed on SEPARATE lines (not adjacent tokens).
    # When they were joined as "Date: 2026-09-18 | PnL: -$105", the model
    # sometimes fused them into "-$105-09-18". Keeping them apart (plus the
    # explicit rule below) prevents that token fusion.
    raw = (f"Date: {date_str}\n"
           f"Total PnL: {formatted_pnl}\n"
           f"Context & News: {context_payload}\n"
           f"Tickers: {active_tickers}")
    task = """
Write the Blog Intro using the persona above.
1. PRIORITY 1: If the context has "DAILY NOTES" or "SPECIAL INSTRUCTIONS", write about that topic FIRST.
2. Discuss the overall market news at a high level and how it impacted the day.
3. MANDATORY LIMIT: Keep the intro to 3-4 sentences maximum.
4. Focus on: What news drove performance? What was the total P/L? State the total P/L exactly as supplied (rounded to nearest dollar).
5. Stay in the persona's voice the whole time. Keep the market discussion macro and simple — do NOT explain technical trading terms (EMAs, Crosses).
6. FORMATTING: Strictly DO NOT use markdown bolding (**).
7. The DATE and the P/L are two SEPARATE values. Never merge or concatenate them (e.g. never write "-$105-09-18"). Always keep the date and the dollar amount distinct.
8. Output format must be strictly:
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
        def _fmt_num(key: str, fmt: str) -> str:
            val = grade_info.get(key)
            if val is None:
                return ""
            try:
                return f"{float(val):{fmt}}"
            except (ValueError, TypeError):
                return ""
        grade_context = (
            f"\n\n--- TRADE GRADER INFO ---\n"
            f"Assigned Letter Grade: {grade_info.get('grade')}\n"
            f"Composite Score: {_fmt_num('composite_score', '.1f')}/100\n"
            f"Alpha vs SPY: {_fmt_num('alpha_vs_spy', '+.2f')}%\n"
            f"Alpha vs Sector: {_fmt_num('alpha_vs_sector', '+.2f')}%\n"
            f"MAE Drawdown: {_fmt_num('mae_pct', '+.2f')}%\n"
            f"MFE Capture: {_fmt_num('capture_ratio', '.1f')}%\n"
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
    result = _apply_persona(raw, task)
    # Some models wrap the blurb in JSON like {"text": "...", "persona": "dexter"}.
    # Unwrap it so only the prose reaches the blog (never raw JSON).
    unwrapped = _unwrap_json(result, "text", "body", "output", "content")
    return unwrapped if unwrapped is not None else result


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("active persona:", BLOG_PERSONA)
    print("example pnl:", format_pnl(-4500.20), "/", format_pnl(4500.20))