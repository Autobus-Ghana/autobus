"""
OPTIONAL n8n Code node — parse LLM NLU response into the same shape as webhook_nlu_handler.

Expects previous HTTP Request node to put the model reply in:
  item["json"]["choices"][0]["message"]["content"]
  (OpenAI-compatible) OR item["json"]["llm_raw"] as plain text.

Paste into Code node (Python Native, Run Once for All Items).
"""

from datetime import datetime, timezone
import json
import re
import uuid


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _extract_llm_text(data):
    if data.get("llm_raw"):
        return str(data["llm_raw"])
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return str(data.get("content") or data.get("text") or "")


def _parse(raw):
    intent = "unknown"
    confidence = 0.5
    slots = {}

    for line in (raw or "").splitlines():
        line = line.strip()
        if line.upper().startswith("INTENT:"):
            intent = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except Exception:
                confidence = 0.5
        elif line.upper().startswith("SLOTS:"):
            blob = line.split(":", 1)[1].strip()
            try:
                slots = json.loads(blob)
            except Exception:
                m = re.search(r"\{.*\}", blob)
                if m:
                    try:
                        slots = json.loads(m.group(0))
                    except Exception:
                        slots = {}
    return intent, confidence, slots if isinstance(slots, dict) else {}


def _reply(intent, report_row=None):
    if intent == "submit_daily_report" and report_row:
        return (
            f"✅ Report saved successfully.\n"
            f"Report ID: {report_row['report_id']}\n"
            f"Date: {report_row['report_date']}\n\n"
            f"It will be included in the CTO summary. Thank you!"
        )
    if intent == "greeting":
        return "Hi! Send your daily report as plain text (tip: start with `Report:`)."
    if intent == "help":
        return "Send `Report: <update>` or Done:/Blockers:/Plans: — you'll get a Report ID."
    return "I didn't detect a daily report. Prefix with `Report:` or ask `help`."


results = []
for item in _items:
    data = item.get("json") or {}
    # Preserve fields from earlier keyword node if present under nested keys
    base = data.get("base") if isinstance(data.get("base"), dict) else data
    phone = base.get("phone") or data.get("phone") or ""
    phone_id = base.get("phone_id") or data.get("phone_id") or ""
    display_name = base.get("display_name") or data.get("display_name") or ""
    message_text = base.get("message_text") or data.get("message_text") or ""

    intent, confidence, slots = _parse(_extract_llm_text(data))
    if not slots.get("body"):
        slots["body"] = message_text
    if not slots.get("report_date"):
        slots["report_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    should_save = intent == "submit_daily_report"
    mongo_document = None
    if should_save:
        mongo_document = {
            "report_id": "RPT-" + uuid.uuid4().hex[:10].upper(),
            "sender_phone": phone,
            "sender_name": display_name,
            "phone_number_id": phone_id,
            "report_date": slots.get("report_date"),
            "body": slots.get("body") or message_text,
            "done": slots.get("done") or "",
            "blockers": slots.get("blockers") or "",
            "plans": slots.get("plans") or "",
            "intent": intent,
            "confidence": confidence,
            "wa_message_id": base.get("message_id") or data.get("message_id") or "",
            "created_at": _now_iso(),
            "summarized": False,
        }

    reply_text = _reply(intent, mongo_document)

    results.append({
        "json": {
            "ok": True,
            "intent": intent,
            "confidence": confidence,
            "slots": slots,
            "should_save": should_save,
            "should_reply": True,
            "phone": phone,
            "phone_id": phone_id,
            "display_name": display_name,
            "message_text": message_text,
            "reply_text": reply_text,
            "report_id": (mongo_document or {}).get("report_id"),
            "mongo_document": mongo_document,
            "report_date": (mongo_document or {}).get("report_date"),
            "body": (mongo_document or {}).get("body"),
            "done": (mongo_document or {}).get("done"),
            "blockers": (mongo_document or {}).get("blockers"),
            "plans": (mongo_document or {}).get("plans"),
            "sender_phone": (mongo_document or {}).get("sender_phone"),
            "sender_name": (mongo_document or {}).get("sender_name"),
            "created_at": (mongo_document or {}).get("created_at"),
            "summarized": (mongo_document or {}).get("summarized"),
        }
    })

return results
