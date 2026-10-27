"""
OPTIONAL n8n Code node — prepare a lightweight LLM NLU request body.

Use when keyword NLU is not enough.

Workflow:
  webhook_nlu_handler (keyword) → IF confidence < 0.7
    → THIS NODE → HTTP Request (OpenRouter/Groq) → llm_nlu_parse.py → IF report → DB

Paste into Code node (Python Native, Run Once for All Items).
HTTP Request node should POST to your LLM chat-completions URL with body = {{ $json.llm_request }}.
"""

SYSTEM_PROMPT = """You classify WhatsApp messages for a team daily-report bot.
Reply with EXACTLY three lines:
INTENT: submit_daily_report|greeting|help|unknown
CONFIDENCE: 0.0-1.0
SLOTS: {"body":"...","report_date":"YYYY-MM-DD","done":"","blockers":"","plans":""}

Rules:
- Past/present work updates, EOD, standup, status → submit_daily_report
- Hi/hello → greeting
- How to use → help
- Otherwise → unknown
- report_date defaults to today UTC if missing
- Keep SLOTS valid JSON on one line
"""

results = []
for item in _items:
    data = item.get("json") or {}
    message = data.get("message_text") or ""
    results.append({
        "json": {
            **data,
            "llm_request": {
                "model": data.get("llm_model") or "openai/gpt-4o-mini",
                "temperature": 0.1,
                "max_tokens": 250,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
            },
        }
    })

return results
