"""Quick sanity check for the string-aware JSON brace-matching in llm_client."""
import json
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm_client import SharedLLMClient

client = SharedLLMClient.__new__(SharedLLMClient)  # bypass __init__ (no API keys needed)

# A complete, valid JSON response whose thought_process strings contain braces.
cleaned_text = (
    '{"decisions": ['
    '{"thought_process": "Rule says {buy if x} and {sell if y}", '
    '"action": "HOLD", "symbol": "JNJ", "quantity": 0.0, '
    '"direction": "neutral", "conviction": 0.5}, '
    '{"thought_process": "Support zone {100-105} and {resistance 110}", '
    '"action": "BUY", "symbol": "NVDA", "quantity": 10.0, '
    '"direction": "bullish", "conviction": 0.8}'
    ']}'
)

# Replicate the extraction logic (string-aware) to confirm it finds the outer object.
stack = []
start_idx = -1
end_idx = -1
in_string = False
escaped = False
for i, char in enumerate(cleaned_text):
    if in_string:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = False
        continue
    if char == '"':
        in_string = True
    elif char == "{":
        if not stack:
            start_idx = i
        stack.append(char)
    elif char == "}":
        if stack:
            stack.pop()
            if not stack:
                end_idx = i
                break

assert start_idx == 0, f"Expected start at 0, got {start_idx}"
assert end_idx == len(cleaned_text) - 1, f"Expected end at {len(cleaned_text)-1}, got {end_idx}"
json_str = cleaned_text[start_idx:end_idx + 1]
parsed = json.loads(json_str)
assert len(parsed["decisions"]) == 2, f"Expected 2 decisions, got {len(parsed['decisions'])}"
print("PASS: string-aware brace matching correctly parsed JSON with braces inside strings.")
print("Decisions:", [d["symbol"] for d in parsed["decisions"]])
