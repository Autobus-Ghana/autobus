# WhatsApp daily-report bot (n8n + MongoDB)

Slim extract of Autobus WhatsApp webhook parsing + lightweight report NLU, stored in **MongoDB via an n8n MongoDB node** (not inside the Python Code sandbox).

## Architecture

n8n Python Code **cannot open DB sockets**. Mongo lives in the workflow as a **MongoDB node** with credentials.

| Step | Node |
|------|------|
| Parse + classify + build document + reply text | Code → `webhook_nlu_handler.py` |
| Persist | MongoDB → Insert → collection `daily_reports` |
| Reply | HTTP Request → Meta WhatsApp |
| Optional LLM NLU | Code prepare → HTTP → Code parse |
| CTO summary | Cron → MongoDB Find → `cto_summary_prepare.py` → LLM → send → MongoDB Update |

## Minimal workflow

```
Webhook (Meta WhatsApp POST)
  → Code: webhook_nlu_handler.py
  → IF should_save === true
       → Code: mongo_unwrap.py          # optional; clean insert payload
       → MongoDB: Insert (daily_reports)
  → IF should_reply === true            # parallel branch from NLU Code
       → HTTP Request: send reply_text
```

Use **two IF branches from the same Code output**: one for Mongo save, one for WhatsApp reply. Do not chain reply after `mongo_unwrap` (that node drops `reply_text`).

## MongoDB node setup

1. Credentials → MongoDB (connection string / host of your Atlas or self-hosted cluster).
2. Node operation: **Insert**
3. Collection: `daily_reports`
4. Document source:
   - After `mongo_unwrap.py`: insert the **whole item** (root fields = document).
   - Or without unwrap: map fields from `{{ $json.mongo_document }}` / flat aliases (`report_id`, `body`, …).

### Document shape

```json
{
  "report_id": "RPT-F3556DD114",
  "sender_phone": "233241234567",
  "sender_name": "Ama",
  "phone_number_id": "12345",
  "report_date": "2026-10-26",
  "body": "…",
  "done": "",
  "blockers": "",
  "plans": "",
  "intent": "submit_daily_report",
  "confidence": 0.95,
  "wa_message_id": "wamid.1",
  "created_at": "2026-10-26T18:00:00+00:00",
  "summarized": false
}
```

Indexes: run `mongo.indexes.js` once in `mongosh`.

## WhatsApp send (HTTP Request)

- Method: `POST`
- URL: `https://graph.facebook.com/v21.0/{{ $json.phone_id }}/messages`
- Header: `Authorization: Bearer <token>`
- Body:

```json
{
  "messaging_product": "whatsapp",
  "to": "{{ $json.phone }}",
  "type": "text",
  "text": { "body": "{{ $json.reply_text }}" }
}
```

## Code node settings

- Language: **Python**
- Mode: **Run Once for All Items**
- Paste file contents as-is (`_items` / `return results`)

## CTO summary (separate workflow)

1. Cron (e.g. 18:00)
2. MongoDB **Find** on `daily_reports`:

```json
{ "report_date": "{{ $now.toFormat('yyyy-MM-dd') }}", "summarized": false }
```

3. Code: `cto_summary_prepare.py`
4. HTTP Request → LLM
5. Send summary to CTO
6. MongoDB **Update** matching those `report_id`s → `{ "summarized": true }`

## Optional stronger NLU

1. `llm_nlu_prepare.py` → `llm_request`
2. HTTP Request → OpenRouter / Groq / OpenAI-compatible
3. `llm_nlu_parse.py` → same `should_save` / `mongo_document` / `reply_text` shape

## Intent examples

| Message | Intent |
|---------|--------|
| `Report: Shipped payments fix` | `submit_daily_report` |
| `Done: X` / `Blockers: Y` / `Plans: Z` | `submit_daily_report` |
| `hi` | `greeting` |
| `help` | `help` |
