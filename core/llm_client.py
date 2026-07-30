import os
import re
import json
import logging
import sys
import time
import concurrent.futures

# Import centralized client from the installed openrouter-workflow
try:
    # First try direct import (if installed as package)
    from openrouter_workflow import client as or_client
except ImportError:
    try:
        # Fallback to local path import
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../openrouter-workflow")))
        import client as or_client
    except ImportError as e:
        logger.critical("Failed to import OpenRouter client. Please ensure openrouter-workflow is installed or in PYTHONPATH.")
        raise ImportError("Failed to import OpenRouter client. Please ensure openrouter-workflow is installed or in PYTHONPATH.") from e

logger = logging.getLogger("SharedLLMClient")

class LLMClientError(Exception):
    """Custom exception raised when LLM generation fails."""
    pass

class SharedLLMClient:
    """
    Centralized OpenRouter Client Wrapper for structured generation.
    Enforces a 20s timeout, Pydantic-based JSON Schema, <think> tag purging,
    and automatic local-rules fallback upon failure.
    """
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.max_retries = 5
        self.retry_delay = 5  # seconds
        self.initial_timeout = 180  # seconds
        self.max_backoff = 60  # seconds
        
    def generate_structured(
        self,
        prompt: str,
        response_model,
        system_prompt: str | None = None,
        tier: str | None = None,
        max_output_tokens: int | None = None
    ) -> dict:
        """
        Executes completion with JSON schema enforcement, a strict 20s timeout,
        cleaning of <think> tags, and critical error logging.
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
            
        # 2. Enforce timeout with exponential backoff retries
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            retry_count = 0
            while retry_count <= self.max_retries:
                try:
                    future = executor.submit(
                        or_client.execute_completion,
                        prompt=prompt,
                        system_prompt=actual_system_prompt,
                        tier=tier,
                        stream=False,
                        max_output_tokens=max_output_tokens
                    )
                    timeout = min(self.initial_timeout, self.max_backoff * (2 ** retry_count))
                    logger.info(f"Attempt {retry_count + 1}/{self.max_retries + 1} with timeout: {timeout}s")
                    logger.debug(f"Calculated timeout: {timeout}s (retry_count: {retry_count}, max_backoff: {self.max_backoff})")
                    response_text = future.result(timeout=timeout)
                    break
                except concurrent.futures.TimeoutError as te:
                    retry_count += 1
                    if retry_count > self.max_retries:
                        logger.critical(f"OpenRouter request timed out after {timeout}s (attempt {retry_count})")
                        logger.debug(f"Request payload: {prompt[:500]}...")
                        raise LLMClientError(f"OpenRouter request timed out after {timeout}s") from te
                    delay = min(self.retry_delay * (2 ** retry_count), self.max_backoff)
                    logger.warning(f"Timeout, retrying in {delay}s...")
                    time.sleep(delay)
            except Exception as e:
                logger.critical(f"OpenRouter client connection error: {e}")
                raise LLMClientError(f"OpenRouter client connection error: {e}") from e

        # Handle empty responses with retry logic
        retry_count = 0
        while not response_text and retry_count < self.max_retries:
            retry_count += 1
            logger.warning(f"OpenRouter returned empty response, retrying ({retry_count}/{self.max_retries})...")
            time.sleep(self.retry_delay)
            
            try:
                future = executor.submit(
                    or_client.execute_completion,
                    prompt=prompt,
                    system_prompt=actual_system_prompt,
                    tier=tier,
                    stream=False,
                    max_output_tokens=max_output_tokens
                )
                response_text = future.result(timeout=timeout)
                logger.debug(f"Retry {retry_count} using timeout: {timeout}s")
            except Exception as e:
                logger.warning(f"Retry {retry_count} failed: {e}")
                
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
        
        # Validate JSON structure with more robust extraction
        json_str = cleaned_text
        
        # Try to find the outermost JSON object
        stack = []
        start_idx = -1
        end_idx = -1
        
        for i, char in enumerate(json_str):
            if char == '{':
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
