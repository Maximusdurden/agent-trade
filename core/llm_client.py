import os
import re
import json
import logging
import sys
import time
import concurrent.futures
from openai import OpenAI
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Ensure environment variables are loaded from .env file
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env"))
load_dotenv(env_path, override=True)

logger = logging.getLogger("SharedLLMClient")

class LLMClientError(Exception):
    """Custom exception raised when LLM generation fails."""
    pass


def _repair_truncated_json(text: str):
    """Attempt to repair a truncated JSON object (e.g. OpenRouter cut it off).

    OpenRouter frequently truncates long responses mid-string or mid-object,
    leaving an unterminated string and unbalanced braces. This walks the text
    once, tracking string/escape state and brace depth, then:
      - closes any unterminated string with a closing quote,
      - appends the missing closing braces/brackets to balance the structure.
    Returns the repaired JSON string on success, or None if the text does not
    even start with a '{' (i.e. it is not a truncated JSON object at all).
    """
    text = (text or "").strip()
    if not text.startswith("{"):
        return None

    in_string = False
    escaped = False
    depth = 0
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1

    repaired = text
    # Close an unterminated string (text ends inside a quoted value).
    if in_string:
        repaired += '"'
    # Append missing closing braces to balance the structure. The strategist and
    # brain both output JSON objects, so '}' is the correct closer.
    if depth > 0:
        repaired += "}" * depth

    try:
        json.loads(repaired)
        return repaired
    except Exception:
        return None


def _repair_literal_whitespace_in_strings(text: str):
    """Escape literal newlines/tabs/carriage-returns that appear INSIDE JSON
    string values.

    The sanitize step strips most control chars but deliberately keeps ``\\n``
    (0x0a) and ``\\r`` (0x0d). When a model emits a *literal* newline inside a
    quoted string value (e.g. a multi-line ``meta_reasoning``), ``json.loads``
    fails with ``Expecting ',' delimiter: line 2 column NNN`` — the exact
    failure signature of the TMCL-946..962 strategist tickets. This walks the
    text string-aware and replaces any raw ``\\n``/``\\r``/``\\t`` found inside a
    quoted value with its escaped form.

    Returns the repaired string, or the original if nothing needed fixing.
    """
    if not text:
        return text
    out = []
    in_string = False
    escaped = False
    changed = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch in "\n\r\t":
                # Literal whitespace inside a string value -> escape it.
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
                changed = True
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out) if changed else text


def _repair_unescaped_quotes_in_strings(text: str):
    """Escape unescaped double-quotes that appear INSIDE JSON string values.

    The strategist LLM (OpenRouter) intermittently emits a string value that
    itself contains a literal double-quote (e.g. ``"He said "buy now" and hold"``).
    ``json.loads`` then terminates the string at the inner quote and fails with
    ``Expecting ',' delimiter: line N column M`` — the exact failure signature of
    the TMCL-963..974 strategist tickets. The whitespace repair only handles
    ``\\n``/``\\t``/``\\r``, so this walks the text string-aware and escapes any
    quote that appears *inside* a quoted value (i.e. not the structural quote
    that opens/closes the value).

    The heuristic: a quote is structural if it is the FIRST non-whitespace char
    after ``{``, ``,``, ``:``, or ``[`` (a key or value opener), or if it is
    followed by ``:``, ``,``, ``}``, ``]``, or whitespace-then-one-of-those (a
    value closer). Any other quote inside a string is treated as literal and
    escaped. Returns the repaired string, or the original if nothing changed.
    """
    if not text:
        return text
    out = []
    in_string = False
    escaped = False
    changed = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                # Peek ahead: if this quote is followed by a value-closer
                # (:, ,, }, ], or whitespace then one of those), treat it as the
                # structural closing quote. Otherwise it is a literal quote
                # inside the value and must be escaped.
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] in ":,}]":
                    in_string = False
                    out.append(ch)
                else:
                    out.append("\\\"")
                    changed = True
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out) if changed else text


def _escape_control_chars_in_strings(text: str):
    """Escape ANY raw control character (0x00-0x1F, 0x7F) found inside a JSON
    string value.

    The sanitize step strips most control chars but deliberately keeps ``\\n``
    (0x0a), ``\\r`` (0x0d), and ``\\t`` (0x09). When a model emits a *literal*
    control character that is NOT one of those three inside a quoted value,
    ``json.loads`` fails with ``Invalid control character at: line N column M``
    — the exact failure signature of the TMCL-977..988 strategist tickets. This
    walks the text string-aware and replaces ANY raw control char inside a
    quoted value with its escaped form (\\n, \\r, \\t, \\uXXXX otherwise).

    Returns the repaired string, or the original if nothing needed fixing.
    """
    if not text:
        return text
    out = []
    in_string = False
    escaped = False
    changed = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            code = ord(ch)
            if code < 0x20 or code == 0x7f:
                # Raw control char inside a string -> escape it.
                if ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                elif ch == "\t":
                    out.append("\\t")
                else:
                    out.append(f"\\u{code:04x}")
                changed = True
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out) if changed else text


def _repair_stray_quote_comma(text: str):
    """Repair a stray ``",`` / ``";`` fragment that the model sometimes emits
    between JSON key/value pairs.

    Observed (TMCL-982): the model emitted ``...gap.\";\n  \",\n    \"todays_rules\": ...``
    — a literal ``";`` followed by a ``",`` on its own line between
    ``meta_reasoning`` and ``todays_rules``. This is a structural corruption:
    the stray quote breaks string tracking and the brace-matching extraction,
    so the whole response is rejected as "Invalid JSON structure".

    Key insight: a legitimate string OPENER is always followed by content, never
    immediately by a structural character (``,`` ``;`` ``:`` ``}`` ``]``). So
    when we are OUTSIDE a string and encounter a ``"`` that is immediately
    followed (after optional whitespace) by one of those structural characters,
    it is a stray fragment — drop the quote and keep the structural char.

    Returns the repaired string, or the original if nothing changed.
    """
    if not text:
        return text
    out = []
    in_string = False
    escaped = False
    changed = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            # Outside a string: check if this quote is a stray fragment.
            # Peek ahead past whitespace for a structural char.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in ",;":
                # Stray fragment: drop the quote AND the following comma or
                # semicolon (both are leftover corruption, e.g. `";` then `",`
                # in TMCL-982). A legit string opener is never immediately
                # followed by `,` or `;`.
                changed = True
                i = j + 1
                continue
            in_string = True
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out) if changed else text


class SharedLLMClient:
    """
    Centralized OpenRouter Client Wrapper for structured generation.
    Enforces a hard per-call wall-clock budget (LLM_MAX_TOTAL_SECONDS, default
    180s), Pydantic-based JSON Schema, think-tag purging, bounded retries with
    thread cancellation, and automatic local-rules fallback upon failure.
    """
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.max_retries = 5
        self.retry_delay = 5  # seconds
        self.initial_timeout = 180  # seconds
        self.max_backoff = 60  # seconds
        
        # Initialize OpenAI client pointing to OpenRouter
        self.client = None
        if self.api_key and "your_openrouter_api_key_here" not in self.api_key:
            try:
                self.client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                )
                logger.info("Successfully initialized OpenRouter client.")
            except Exception as e:
                logger.error(f"Failed to initialize OpenRouter client: {e}")
                
        # Initialize Gemini client as fallback
        self.gemini_client = None
        if self.gemini_api_key and "your_gemini_api_key_here" not in self.gemini_api_key:
            try:
                self.gemini_client = genai.Client(api_key=self.gemini_api_key)
                logger.info("Successfully initialized Gemini client as fallback.")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                
        if not self.client and not self.gemini_client:
            logger.critical("Neither OpenRouter nor Gemini API keys are configured or valid.")
            raise LLMClientError("Neither OpenRouter nor Gemini API keys are configured or valid.")
        
        # Map tiers to models from environment variables
        self.tier_mapping = {
            "heavyweight": os.getenv("MODEL_HEAVYWEIGHT", "deepseek/deepseek-r1"),
            "daily_driver": os.getenv("MODEL_DAILY_DRIVER", "google/gemini-2.5-flash"),
            "utility": os.getenv("MODEL_UTILITY", "openrouter/free")
        }
        
    def _execute_completion(
        self,
        prompt: str,
        system_prompt: str,
        tier: str | None = None,
        max_output_tokens: int | None = None,
        explicit_model: str | None = None
    ) -> str:
        """Execute a chat completion with OpenRouter primary, Gemini fallback.

        Previously this delegated to the external ``openrouter-workflow`` package.
        Now it uses the ``openai`` SDK pointed at OpenRouter directly so the
        dependency is eliminated.  If the OpenRouter call fails (network, quota,
        model overload) it falls through to the native ``google-genai`` client so
        the trading cycle is never disrupted by a single provider outage.

        ``explicit_model`` (optional): an exact OpenRouter model id to use,
        bypassing the tier->model mapping. Used by the blog brain so ``BLOG_MODEL``
        is respected exactly (the tier mapping alone would route blog content to
        ``openrouter/free``, a free-tier model that leaks chain-of-thought).

        Returns the raw text content of the model response.
        """
        resolved_tier = tier.lower() if tier else "daily_driver"
        model_id = explicit_model or self.tier_mapping.get(resolved_tier, self.tier_mapping["daily_driver"])
        
        # Try OpenRouter first
        if self.client:
            try:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
                
                final_max_tokens = max_output_tokens or int(os.getenv("MAX_OUTPUT_TOKENS", "2048"))
                
                extra_headers = {
                    "HTTP-Referer": "https://github.com/google-antigravity",
                    "X-Title": "Antigravity CLI Agent Flow",
                }
                
                response = self.client.chat.completions.create(
                    model=model_id,
                    messages=messages,
                    max_tokens=final_max_tokens,
                    temperature=0.2,
                    # Constrain the model to emit a valid JSON object. This is the
                    # strongest defense against the malformed-JSON failures
                    # (TMCL-963..974): OpenRouter's json_object mode makes the
                    # model produce well-formed JSON, so the string-value quote
                    # and whitespace issues largely disappear at the source.
                    response_format={"type": "json_object"},
                    extra_headers=extra_headers
                )
                
                return response.choices[0].message.content or ""
            except Exception as e:
                logger.warning(f"OpenRouter request failed: {e}. Falling back to Gemini.")
                
        # Fallback to Gemini
        if self.gemini_client:
            try:
                # Map OpenRouter model names to Gemini models if needed, or use configured GEMINI_MODEL
                gemini_model_name = self.gemini_model
                if "gemini-2.5-flash" in model_id:
                    gemini_model_name = "gemini-2.5-flash"
                elif "gemini-2.5-pro" in model_id:
                    gemini_model_name = "gemini-2.5-pro"
                elif "gemini-3.1-pro" in model_id:
                    gemini_model_name = "gemini-3.1-pro-preview"
                elif "gemini-3.5-flash" in model_id:
                    gemini_model_name = "gemini-2.5-flash" # fallback to 2.5-flash if 3.5-flash is not supported yet
                
                logger.info(f"Executing fallback completion using Gemini model: {gemini_model_name}")
                
                response = self.gemini_client.models.generate_content(
                    model=gemini_model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.2,
                    )
                )
                return response.text or ""
            except Exception as e:
                logger.critical(f"Gemini fallback request failed: {e}")
                raise LLMClientError(f"Both OpenRouter and Gemini fallback failed. Gemini error: {e}") from e
                
        raise LLMClientError("OpenRouter failed and Gemini fallback is not configured.")

    def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        tier: str | None = None,
        max_output_tokens: int | None = None,
        explicit_model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        """Execute a free-form (non-JSON) chat completion.

        Thin wrapper over ``_execute_completion`` for conversational / prose
        callers (e.g. the dashboard chat widget) that do NOT need the strict
        JSON-schema enforcement of ``generate_structured``. OpenRouter is
        primary; Gemini is the cross-provider fallback. ``explicit_model``
        bypasses the tier->model mapping (used to mirror the brain A/B model
        selection so the chat uses the same model that is actually trading).

        Returns the raw text content of the model response.
        """
        return self._execute_completion(
            prompt=prompt,
            system_prompt=system_prompt or "",
            tier=tier,
            max_output_tokens=max_output_tokens,
            explicit_model=explicit_model,
        )

    @staticmethod
    def _is_transient_error(err: Exception) -> bool:
        """Return True if ``err`` is a transient provider error worth retrying.

        OpenRouter/Gemini return 503 (high demand), 429 (rate limit), and
        connection resets under load. These are transient and MUST be retried
        with backoff rather than treated as fatal — otherwise a single 503
        bubbles straight out of ``future.result()`` and the whole call gives up
        (the TMCL-896..902 failure mode). Non-transient errors (schema, auth,
        bad request) are re-raised immediately.
        """
        msg = str(err).lower()
        transient_markers = (
            "503", "429", "unavailable", "high demand", "rate limit",
            "rate_limit", "connection", "reset", "timeout", "temporarily",
            "overloaded", "try again later", "service unavailable",
        )
        return any(m in msg for m in transient_markers)

    def _backoff_or_fallback(self, retry_count: int, start: float, max_total_seconds: int,
                             _try_gemini_direct, kind: str):
        """Compute backoff delay; if it would exceed the budget, try Gemini and give up.

        Returns:
          - a non-empty str if a Gemini fallback succeeded (caller should use it and break),
          - None if the caller should sleep the backoff delay and retry,
          - raises LLMClientError if the budget is exhausted and Gemini also failed.
        """
        delay = min(self.retry_delay * (2 ** retry_count), self.max_backoff)
        if (time.monotonic() - start) + delay >= max_total_seconds:
            try:
                response_text = _try_gemini_direct()
                if response_text:
                    return response_text
            except Exception as gem_ex:
                logger.critical(
                    f"OpenRouter {kind} would exceed {max_total_seconds}s budget and "
                    f"Gemini cross-provider fallback failed: {gem_ex}"
                )
            logger.critical(
                f"{kind} would exceed remaining {max_total_seconds}s budget; giving up."
            )
            raise LLMClientError(
                f"LLM call exceeded total {max_total_seconds}s budget ({kind.lower()})."
            )
        logger.warning(f"{kind}, retrying in {delay}s...")
        time.sleep(delay)
        return None

    def _generate_via_gemini(self, prompt, system_prompt, model_id):
        """Execute a completion using the native google-genai Gemini client only.

        Used as a true cross-provider fallback when OpenRouter HANGS (times out
        at the executor level, which never reaches ``_execute_completion``'s own
        inner Gemini fallback). This keeps the trading cycle alive during a
        transient OpenRouter outage instead of silently degrading to rule-based.
        """
        if not self.gemini_client:
            raise LLMClientError("OpenRouter failed and Gemini fallback is not configured.")
        gemini_model_name = self.gemini_model
        if "gemini-2.5-flash" in model_id:
            gemini_model_name = "gemini-2.5-flash"
        elif "gemini-2.5-pro" in model_id:
            gemini_model_name = "gemini-2.5-pro"
        elif "gemini-3.1-pro" in model_id:
            gemini_model_name = "gemini-3.1-pro-preview"
        elif "gemini-3.5-flash" in model_id:
            gemini_model_name = "gemini-2.5-flash"  # fallback if 3.5-flash unsupported
        logger.info(f"Executing cross-provider fallback completion using Gemini model: {gemini_model_name}")
        response = self.gemini_client.models.generate_content(
            model=gemini_model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
            )
        )
        return response.text or ""
        
    def generate_structured(
        self,
        prompt: str,
        response_model,
        system_prompt: str | None = None,
        tier: str | None = None,
        max_output_tokens: int | None = None,
        explicit_model: str | None = None
    ) -> dict:
        """
        Executes completion with JSON schema enforcement, a strict 20s timeout,
        cleaning of  thinking tags, and critical error logging.

        ``explicit_model`` (optional): an exact OpenRouter model id to use,
        bypassing the tier->model mapping. Used by the strategist A/B experiment
        to alternate between two heavyweight models without a redeploy.
        """
        # 1. Enforce JSON schema using Pydantic models
        try:
            if hasattr(response_model, "model_json_schema"):
                schema = response_model.model_json_schema()
            else:
                schema = response_model.schema()
            schema_json = json.dumps(schema, indent=2)
        except Exception as e:
            logger.warning(f"Could not extract JSON schema from response_model: {e}")
            schema_json = str(response_model)

        schema_instruction = (
            f"\n\nCRITICAL: You MUST return a valid JSON object matching this exact schema:\n"
            f"{schema_json}\n"
            f"IMPORTANT RULES:\n"
            f"1. Return ONLY the raw JSON object, no markdown wrappers or backticks\n"
            f"2. Ensure all strings are properly quoted and escaped\n"
            f"3. All brackets and braces must be balanced\n"
            f"4. Example of valid output: {{\"key\": \"value\"}}\n"
            f"FAILURE TO FOLLOW THESE RULES WILL RESULT IN PARSING ERRORS"
        )
        
        if system_prompt:
            actual_system_prompt = system_prompt + schema_instruction
        else:
            actual_system_prompt = (
                "You are an elite financial trading assistant. Return only high-quality output."
                + schema_instruction
            )
            
        # 2. Enforce timeout with exponential backoff retries.
        # A bounded wall-clock envelope keeps the whole call within a hard budget
        # so a hung OpenRouter/Gemini request can never blow the Cloud Run job
        # timeout (600s). The background thread is cancelled on each timeout so
        # cumulative wall-time stays bounded across retries.
        max_total_seconds = int(os.getenv("LLM_MAX_TOTAL_SECONDS", "120"))
        start = time.monotonic()
        resolved_tier = tier.lower() if tier else "daily_driver"
        if explicit_model:
            model_id = explicit_model  # A/B experiment override
        else:
            model_id = self.tier_mapping.get(resolved_tier, self.tier_mapping["daily_driver"])

        def _try_gemini_direct() -> str:
            """Last-resort cross-provider fallback to Gemini when OpenRouter hangs.
            Runs the native Gemini call inside the same bounded executor so a hung
            Gemini request still can't blow the Cloud Run job timeout."""
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as gem_exec:
                fut = gem_exec.submit(
                    self._generate_via_gemini,
                    prompt, actual_system_prompt, model_id,
                )
                try:
                    return fut.result(timeout=max_total_seconds)
                except concurrent.futures.TimeoutError:
                    fut.cancel()
                    raise LLMClientError("Gemini cross-provider fallback timed out.")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            response_text = None
            retry_count = 0
            try:
                while True:
                    elapsed = time.monotonic() - start
                    if elapsed >= max_total_seconds:
                        # Try Gemini directly before giving up (transient OpenRouter hang).
                        try:
                            response_text = _try_gemini_direct()
                            break
                        except Exception as gem_ex:
                            logger.critical(
                                f"OpenRouter exhausted {max_total_seconds}s budget and "
                                f"Gemini cross-provider fallback also failed: {gem_ex}"
                            )
                        raise LLMClientError(
                            f"LLM call exceeded total {max_total_seconds}s budget (retries={retry_count})."
                        )
                    # Remaining budget caps this attempt's timeout to the global deadline.
                    # Base timeout is 20s, but scale it up for large-output calls
                    # (e.g. the brain's 16-ticker batch with max_output_tokens=8192).
                    # Verbose models like deepseek/deepseek-v4-flash-0731 can take
                    # >20s to stream a large JSON response, which previously caused
                    # 4x timeouts -> Gemini fallback on every deepseek day. Scale the
                    # per-attempt timeout with the requested output size so a large
                    # batch call gets enough time to complete on the primary model.
                    base_timeout = 20
                    if max_output_tokens and max_output_tokens > 2048:
                        # ~1s per 512 output tokens beyond the 2048 baseline, capped
                        # so a hung request still can't blow the global budget.
                        base_timeout = min(60, 20 + (max_output_tokens - 2048) // 512)
                    attempt_timeout = min(base_timeout, max(1, max_total_seconds - elapsed))
                    attempt_timeout = max(attempt_timeout, 1)
                    try:
                        future = executor.submit(
                            self._execute_completion,
                            prompt=prompt,
                            system_prompt=actual_system_prompt,
                            tier=tier,
                            max_output_tokens=max_output_tokens,
                            explicit_model=explicit_model
                        )
                        logger.info(f"Attempt {retry_count + 1} with timeout: {attempt_timeout}s")
                        response_text = future.result(timeout=attempt_timeout)
                        break
                    except concurrent.futures.TimeoutError:
                        future.cancel()  # mark cancelled to free the thread
                        retry_count += 1
                        if retry_count > self.max_retries:
                            # Try Gemini directly before giving up on timeouts.
                            try:
                                response_text = _try_gemini_direct()
                                break
                            except Exception as gem_ex:
                                logger.critical(
                                    f"OpenRouter timed out after {max_retries} and Gemini "
                                    f"cross-provider fallback failed: {gem_ex}"
                                )
                            logger.critical(
                                f"OpenRouter request timed out after {attempt_timeout}s (attempt {retry_count})"
                            )
                            logger.debug(f"Request payload: {prompt[:500]}...")
                            raise LLMClientError(
                                f"OpenRouter request timed out after {attempt_timeout}s"
                            ) from None
                        fb = self._backoff_or_fallback(retry_count, start, max_total_seconds,
                                                      _try_gemini_direct, "Timeout")
                        if fb:
                            response_text = fb
                            break
                    except Exception as attempt_err:
                        # Transient provider errors (503 high-demand, 429 rate limit,
                        # connection reset) must be RETRIED with backoff, not treated
                        # as fatal. Previously only TimeoutError was retried, so a
                        # single 503 from OpenRouter (with Gemini also 503ing) bubbled
                        # straight to the outer except and gave up immediately —
                        # producing the "backoff would exceed 180s" / "empty rule"
                        # failures (TMCL-896..902).
                        future.cancel()
                        if not self._is_transient_error(attempt_err):
                            raise
                        retry_count += 1
                        if retry_count > self.max_retries:
                            try:
                                response_text = _try_gemini_direct()
                                break
                            except Exception as gem_ex:
                                logger.critical(
                                    f"OpenRouter transient error after {max_retries} retries and "
                                    f"Gemini cross-provider fallback failed: {gem_ex}"
                                )
                            raise LLMClientError(
                                f"OpenRouter transient error after {max_retries} retries: {attempt_err}"
                            ) from None
                        fb = self._backoff_or_fallback(retry_count, start, max_total_seconds,
                                                       _try_gemini_direct, "Transient error")
                        if fb:
                            response_text = fb
                            break

                # Handle empty responses with retry logic (also bounded by the budget).
                while not response_text and retry_count < self.max_retries:
                    if (time.monotonic() - start) >= max_total_seconds:
                        logger.critical(
                            f"Retry for empty response exceeded {max_total_seconds}s budget."
                        )
                        break
                    retry_count += 1
                    logger.warning(
                        f"OpenRouter returned empty response, retrying ({retry_count}/{self.max_retries})..."
                    )
                    time.sleep(self.retry_delay)

                    try:
                        future = executor.submit(
                            self._execute_completion,
                            prompt=prompt,
                            system_prompt=actual_system_prompt,
                            tier=tier,
                            max_output_tokens=max_output_tokens,
                            explicit_model=explicit_model
                        )
                        response_text = future.result(timeout=attempt_timeout)
                        logger.debug(f"Retry {retry_count} using timeout: {attempt_timeout}s")
                    except Exception as e:
                        logger.warning(f"Retry {retry_count} failed: {e}")
            except Exception as e:
                logger.critical(f"OpenRouter client connection error: {e}")
                raise LLMClientError(f"OpenRouter client connection error: {e}") from e

        if not response_text:
            logger.critical("OpenRouter returned empty response after retries.")
            raise LLMClientError("OpenRouter returned empty response after retries.")

        # 3. Clean and validate response text
        try:
            # Extract reasoning blocks if present
            reasoning_match = re.search(r"<think>(.*?)</think>", response_text, flags=re.DOTALL)
            if reasoning_match:
                reasoning = reasoning_match.group(1).strip()
                logger.info(f"Extracted LLM reasoning block:\n{reasoning}")
        except Exception as e:
            logger.warning(f"Failed to extract reasoning block: {e}")

        # Remove <think> tags and markdown wrappers
        cleaned_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
        
        # Remove markdown code block wrappers and any surrounding text
        if "```" in cleaned_text:
            # Extract content between first and last ```
            match = re.search(r"```(?:json\n)?(.*?)\n?```", cleaned_text, flags=re.DOTALL)
            if match:
                cleaned_text = match.group(1).strip()
            else:
                # Fallback to simple removal if pattern not matched
                cleaned_text = re.sub(r"```", "", cleaned_text)

        # Sanitize control characters (except \t, \n, \r)
        cleaned_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", cleaned_text)
        
        # Validate JSON structure with more robust extraction.
        # The brace-matching below is STRING-AWARE: braces inside quoted string
        # values (e.g. a thought_process containing "{" or "}") must not be
        # counted as structural delimiters, otherwise a perfectly valid response
        # is misdetected as unbalanced and we fall back to rule-based trading.
        json_str = cleaned_text

        # Try to find the outermost JSON object
        stack = []
        start_idx = -1
        end_idx = -1
        in_string = False
        escaped = False

        for i, char in enumerate(json_str):
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == '{':
                if not stack:
                    start_idx = i
                stack.append(char)
            elif char == '}':
                if stack:
                    stack.pop()
                    if not stack:
                        end_idx = i
                        break
        
        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            # The response is likely TRUNCATED (OpenRouter cut it off mid-string or
            # mid-object). Before giving up, attempt a truncation repair: close any
            # unterminated string and append the missing closing braces so the JSON
            # becomes parseable. This is the #1 cause of the morning strategist
            # "Invalid JSON structure" failures (SPY/META/TXN).
            repaired = _repair_truncated_json(cleaned_text)
            if repaired is not None:
                json_str = repaired
                logger.warning("Repaired truncated JSON response by closing unterminated string/braces.")
            else:
                logger.critical(f"Invalid JSON structure in response. Full response: {response_text}")
                logger.debug(f"Cleaned text: {cleaned_text}")
                raise LLMClientError("Invalid JSON structure in LLM response")
        else:
            json_str = json_str[start_idx:end_idx+1]
        
        # Validate string termination
        quote_count = json_str.count('"')
        if quote_count % 2 != 0:
            # Attempt to fix unterminated strings
            if json_str[-1] != '"':
                json_str += '"'
            else:
                json_str = json_str[:-1]
                end_idx -= 1
                json_str = cleaned_text[start_idx:end_idx+1]

        # Parse JSON with multiple recovery attempts
        for attempt in range(4):
            try:
                result_dict = json.loads(json_str)
                return result_dict
            except json.JSONDecodeError as e:
                if attempt < 3:  # Try recovery on first three attempts
                    logger.warning(f"JSON parse attempt {attempt + 1} failed: {e}")

                    if attempt == 0:
                        # Root cause of the TMCL-977..988 strategist tickets:
                        # a stray `",` / `";` fragment between key/value pairs
                        # (e.g. TMCL-982). This MUST run before any string-aware
                        # escaping, because the stray quote makes the whitespace
                        # repair treat the rest of the object as one giant string
                        # and escape all its newlines, mangling the structure.
                        repaired = _repair_stray_quote_comma(json_str)
                        if repaired != json_str:
                            json_str = repaired
                            continue

                    if attempt == 1:
                        # Root cause of "Expecting ',' delimiter: line 2 column NNN":
                        # a LITERAL newline/tab inside a quoted string value. Escape
                        # them string-aware before any other heuristic.
                        repaired = _repair_literal_whitespace_in_strings(json_str)
                        if repaired != json_str:
                            json_str = repaired
                            continue

                    if attempt == 2:
                        # Root cause of the TMCL-963..974 strategist tickets: the
                        # model emits a string value containing an UNESCAPED
                        # double-quote (e.g. "He said "buy now""). The whitespace
                        # repair above doesn't touch quotes, so escape them
                        # string-aware here before the fragile regex heuristics.
                        repaired = _repair_unescaped_quotes_in_strings(json_str)
                        if repaired != json_str:
                            json_str = repaired
                            continue

                    if attempt == 3:
                        # Root cause of the TMCL-988 strategist ticket: a raw
                        # control character that is NOT \t/\n/\r surviving inside
                        # a string value -> "Invalid control character at: line N
                        # column M". Escape any remaining control char string-aware.
                        repaired = _escape_control_chars_in_strings(json_str)
                        if repaired != json_str:
                            json_str = repaired
                            continue

                    # Attempt to fix common issues
                    if "\"" in json_str:
                        # Try balancing quotes
                        json_str = re.sub(r'(?<!\\)"(?![:,\}\]])', '\"', json_str)

                    # Try removing trailing commas
                    json_str = re.sub(r',\s*([\}\]])(?!\s*[\"\d\{\[])', r'\1', json_str)

                    # Try extracting again if we modified
                    if attempt == 2:
                        json_str = json_str[json_str.find('{'):json_str.rfind('}')+1]
                else:
                    logger.critical(f"Final JSON parse failed. Error: {e}")
                    logger.debug(f"Final JSON attempt: {json_str}")
                    logger.debug(f"Raw response: {response_text}")
                    raise LLMClientError(f"Failed to parse LLM response as JSON: {e}") from e
