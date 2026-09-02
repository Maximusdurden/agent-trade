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
        max_output_tokens: int | None = None
    ) -> str:
        """Execute a chat completion with OpenRouter primary, Gemini fallback.

        Previously this delegated to the external ``openrouter-workflow`` package.
        Now it uses the ``openai`` SDK pointed at OpenRouter directly so the
        dependency is eliminated.  If the OpenRouter call fails (network, quota,
        model overload) it falls through to the native ``google-genai`` client so
        the trading cycle is never disrupted by a single provider outage.

        Returns the raw text content of the model response.
        """
        resolved_tier = tier.lower() if tier else "daily_driver"
        model_id = self.tier_mapping.get(resolved_tier, self.tier_mapping["daily_driver"])
        
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
                    attempt_timeout = min(20, max(1, max_total_seconds - elapsed))
                    attempt_timeout = max(attempt_timeout, 1)
                    try:
                        future = executor.submit(
                            self._execute_completion,
                            prompt=prompt,
                            system_prompt=actual_system_prompt,
                            tier=tier,
                            max_output_tokens=max_output_tokens
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
                        delay = min(self.retry_delay * (2 ** retry_count), self.max_backoff)
                        # Respect the global budget while backing off.
                        if (time.monotonic() - start) + delay >= max_total_seconds:
                            # Try Gemini directly before giving up on backoff.
                            try:
                                response_text = _try_gemini_direct()
                                break
                            except Exception as gem_ex:
                                logger.critical(
                                    f"OpenRouter backoff would exceed {max_total_seconds}s "
                                    f"and Gemini cross-provider fallback failed: {gem_ex}"
                                )
                            logger.critical(
                                f"Backoff would exceed remaining {max_total_seconds}s budget; giving up."
                            )
                            raise LLMClientError(
                                f"LLM call exceeded total {max_total_seconds}s budget (backoff)."
                            )
                        logger.warning(f"Timeout, retrying in {delay}s...")
                        time.sleep(delay)

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
                            max_output_tokens=max_output_tokens
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
            logger.critical(f"Invalid JSON structure in response. Full response: {response_text}")
            logger.debug(f"Cleaned text: {cleaned_text}")
            raise LLMClientError("Invalid JSON structure in LLM response")
        
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
        for attempt in range(3):
            try:
                result_dict = json.loads(json_str)
                return result_dict
            except json.JSONDecodeError as e:
                if attempt < 2:  # Try recovery on first two attempts
                    logger.warning(f"JSON parse attempt {attempt + 1} failed: {e}")
                    
                    # Attempt to fix common issues
                    if "\"" in json_str:
                        # Try balancing quotes
                        json_str = re.sub(r'(?<!\\)"(?![:,\}\]])', '\"', json_str)
                    
                    # Try removing trailing commas
                    json_str = re.sub(r',\s*([\}\]])(?!\s*[\"\d\{\[])', r'\1', json_str)
                    
                    # Try extracting again if we modified
                    if attempt == 1:
                        json_str = json_str[json_str.find('{'):json_str.rfind('}')+1]
                else:
                    logger.critical(f"Final JSON parse failed. Error: {e}")
                    logger.debug(f"Final JSON attempt: {json_str}")
                    logger.debug(f"Raw response: {response_text}")
                    raise LLMClientError(f"Failed to parse LLM response as JSON: {e}") from e
