# Antigravity Workspace Guidelines: OpenRouter Delegation

To maintain maximum cost-efficiency and utilize our highly optimized 3-tier model ladder, the following guidelines are **ALWAYS ON** for all agent conversations in this workspace:

## 1. Automatic OpenRouter Delegation
* Whenever the user asks a coding question, requests a refactor, or asks for deep code analysis:
  * Use the standard OpenRouter API via the `openai` library and the `OPENROUTER_API_KEY` in `.env`.
  * Use the appropriate tier depending on task complexity:
    * **`daily_driver`** (runs `google/gemini-2.5-flash`): For standard tasks, unit tests, script modifications, and general debugging.
    * **`heavyweight`** (runs `deepseek/deepseek-r1`): For complex multi-file logic, math, security audits, and deadlocks.
  * Retrieve the generated response and present it to the user.
