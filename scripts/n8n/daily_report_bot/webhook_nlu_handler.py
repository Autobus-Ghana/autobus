"""
n8n Code node — Python (Native) — mode: Run Once for All Items

Paste this entire file into an n8n Code node (language: Python).

Flow this node expects:
  WhatsApp / Webhook Trigger  →  THIS CODE  →  IF should_save
                                              ├─ MongoDB Insert (mongo_document)
                                              └─ HTTP Request (WhatsApp reply with reply_text)

Constraints (n8n native Python):
  - No HTTP / filesystem from this node
  - Prefer bracket access: item["json"]["field"]
  - Keep imports minimal (stdlib only); on n8n Cloud remove imports if blocked

Extracted / slimmed from Autobus webhook parsing patterns
(webhookscontroller.handle_incoming_message) + a lightweight report NLU
(not the full LLM IntentDetector — that needs an HTTP Request + LLM node).
"""

from datetime import datetime, timezone
import re
import uuid


# ---------------------------------------------------------------------------
# Lightweight NLU (keyword + structure heuristics — no LLM call)
# ---------------------------------------------------------------------------

REPORT_HINTS = (
    "daily report",
    "end of day",
    "eod",
    "status update",
    "standup",
    "stand-up",
    "work report",
    "progress report",
    "today i",
    "today's update",
    "todays update",
    "report:",
    "#report",
    "submit report",
    "my report",
)

GREETING_HINTS = (
    "hi",
    "hello",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "hola",
)

HELP_HINTS = (
    "help",
    "how does this work",
    "what can you do",
    "commands",
)


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def detect_intent(message_text):
    """
    Returns (intent, confidence, slots)
    intents: submit_daily_report | greeting | help | unknown
    """
    text = (message_text or "").strip()
    low = _norm(text)
    slots = {}

    if not low:
        return "unknown", 0.0, slots

    # Explicit prefix forms: "report: ..." / "#report ..."
    m = re.match(r"^(?:#?\s*report\s*[:\-]|daily\s+report\s*[:\-])\s*(.+)$", text, re.I | re.S)
    if m:
        slots["body"] = m.group(1).strip()
        slots["report_date"] = _extract_date(low) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return "submit_daily_report", 0.95, slots

    for hint in REPORT_HINTS:
        if hint in low:
            slots["body"] = text
            slots["report_date"] = _extract_date(low) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Pull common sections if present
            slots.update(_extract_sections(text))
            return "submit_daily_report", 0.85, slots

    # Long operational updates without the word "report" (heuristic)
    if len(text) >= 80 and _looks_like_status_update(low):
        slots["body"] = text
        slots["report_date"] = _extract_date(low) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slots.update(_extract_sections(text))
        return "submit_daily_report", 0.65, slots

    if any(low == g or low.startswith(g + " ") or low.startswith(g + "!") for g in GREETING_HINTS):
        return "greeting", 0.9, slots

    if any(h in low for h in HELP_HINTS):
        return "help", 0.9, slots

    return "unknown", 0.4, slots


def _extract_date(low):
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", low)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        # Prefer ISO when day looks like day (d<=31); ambiguous — keep as-is YYYY-MM-DD best-effort
        if mo <= 12 and d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"
    return None


def _extract_sections(text):
    """Optional Done / Blockers / Plans style parsing."""
    sections = {}
    patterns = {
        "done": r"(?:^|\n)\s*(?:done|completed|accomplished)\s*[:\-]\s*(.+?)(?=\n\s*(?:blocker|blockers|plan|plans|next|todo)\s*[:\-]|\Z)",
        "blockers": r"(?:^|\n)\s*(?:blocker|blockers|blocked)\s*[:\-]\s*(.+?)(?=\n\s*(?:done|plan|plans|next|todo)\s*[:\-]|\Z)",
        "plans": r"(?:^|\n)\s*(?:plan|plans|next|todo|tomorrow)\s*[:\-]\s*(.+?)(?=\n\s*(?:done|blocker|blockers)\s*[:\-]|\Z)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.I | re.S)
        if m:
            sections[key] = m.group(1).strip()
    return sections


def _looks_like_status_update(low):
    cues = ("worked on", "finished", "completed", "blocker", "blocked", "deployed", "shipped", "in progress", "today")
    return sum(1 for c in cues if c in low) >= 2


# ---------------------------------------------------------------------------
# WhatsApp / simple-chat payload parsing (from Autobus webhook shapes)
# ---------------------------------------------------------------------------

def parse_inbound(payload):
    """
    Accepts:
      1) Meta WhatsApp Cloud API webhook (object/entry/changes/value/messages)
      2) Simple chat: customer_number + message (+ optional company_number)
      3) Already-flattened item from an n8n WhatsApp node
    """
    if not isinstance(payload, dict):
        return None

    # Already flat (n8n WhatsApp / previous Set node)
    flat_text = _first_str(payload, "message", "text", "body", "webhook_message")
    flat_phone = _first_str(payload, "from", "phone", "wa_id", "customer_number", "customer_phone", "userid")
    flat_phone_id = _first_str(payload, "phone_number_id", "phone_id")
    flat_name = _first_str(payload, "profile_name", "name", "display_name", "pushName")
    if flat_text and flat_phone and "entry" not in payload:
        return {
            "phone": flat_phone,
            "phone_id": flat_phone_id,
            "display_name": flat_name,
            "message_text": flat_text,
            "message_id": _first_str(payload, "message_id", "id", "wamid"),
            "source": "flat",
        }

    # Simple chat shape used by Autobus public webhook
    customer = _first_str(payload, "customer_number", "customer_phone", "customer", "userid")
    msg = _first_str(payload, "message", "webhook_message", "text", "body")
    if customer and msg and "entry" not in payload:
        return {
            "phone": customer,
            "phone_id": _first_str(payload, "company_number", "company_id", "phone_number_id"),
            "display_name": _first_str(payload, "company_name", "name"),
            "message_text": msg,
            "message_id": "",
            "source": "simple_chat",
        }

    # Meta WhatsApp
    if "entry" not in payload:
        return None

    try:
        entry = (payload.get("entry") or [None])[0] or {}
        change = (entry.get("changes") or [None])[0] or {}
        value = change.get("value") or {}
        if "messages" not in value:
            return None  # status / other webhook — ignore

        metadata = value.get("metadata") or {}
        phone_id = metadata.get("phone_number_id") or metadata.get("phone_id") or ""
        contacts = value.get("contacts") or []
        phone = ""
        display_name = ""
        if contacts:
            phone = contacts[0].get("wa_id") or ""
            profile = contacts[0].get("profile") if isinstance(contacts[0].get("profile"), dict) else {}
            display_name = str((profile or {}).get("name") or "").strip()

        message = (value.get("messages") or [None])[0] or {}
        if message.get("type") != "text":
            return {
                "phone": phone,
                "phone_id": phone_id,
                "display_name": display_name,
                "message_text": "",
                "message_id": message.get("id") or "",
                "source": "meta_whatsapp",
                "unsupported_type": message.get("type") or "unknown",
            }

        text_body = ((message.get("text") or {}).get("body")) or ""
        return {
            "phone": phone,
            "phone_id": phone_id,
            "display_name": display_name,
            "message_text": text_body,
            "message_id": message.get("id") or "",
            "source": "meta_whatsapp",
        }
    except Exception:
        return None


def _first_str(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# MongoDB document builder (actual insert = n8n MongoDB node)
# Collection: daily_reports
# ---------------------------------------------------------------------------

def build_mongo_document(inbound, slots, intent, confidence):
    """Document shape for n8n MongoDB → Insert → collection daily_reports."""
    report_id = "RPT-" + uuid.uuid4().hex[:10].upper()
    body = slots.get("body") or inbound.get("message_text") or ""
    return {
        "report_id": report_id,
        "sender_phone": inbound.get("phone") or "",
        "sender_name": inbound.get("display_name") or "",
        "phone_number_id": inbound.get("phone_id") or "",
        "report_date": slots.get("report_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "body": body,
        "done": slots.get("done") or "",
        "blockers": slots.get("blockers") or "",
        "plans": slots.get("plans") or "",
        "intent": intent,
        "confidence": confidence,
        "wa_message_id": inbound.get("message_id") or "",
        "created_at": _now_iso(),
        "summarized": False,
    }


def build_reply(intent, doc=None, unsupported_type=None):
    if unsupported_type:
        return f"Thanks — I currently only process text daily reports (got {unsupported_type})."
    if intent == "submit_daily_report" and doc:
        return (
            f"✅ Report saved successfully.\n"
            f"Report ID: {doc['report_id']}\n"
            f"Date: {doc['report_date']}\n\n"
            f"It will be included in the CTO summary. Thank you!"
        )
    if intent == "greeting":
        return (
            "Hi! Send your daily report as plain text.\n"
            "Tip: start with `Report:` or use sections Done / Blockers / Plans."
        )
    if intent == "help":
        return (
            "How to submit a daily report:\n"
            "1) Message: `Report: <your update>`\n"
            "2) Or include Done: / Blockers: / Plans:\n"
            "You'll get a Report ID when it's saved."
        )
    return (
        "I didn't detect a daily report. "
        "Prefix with `Report:` or ask `help` for the format."
    )


# ---------------------------------------------------------------------------
# n8n entry
# ---------------------------------------------------------------------------

results = []

for item in _items:
    payload = item.get("json") or {}
    inbound = parse_inbound(payload)

    if inbound is None:
        # Likely a delivery status webhook — pass through quietly
        results.append({
            "json": {
                "ok": True,
                "skipped": True,
                "reason": "not_a_message",
                "intent": "skip",
                "should_save": False,
                "should_reply": False,
            }
        })
        continue

    if inbound.get("unsupported_type"):
        reply = build_reply("unknown", unsupported_type=inbound["unsupported_type"])
        results.append({
            "json": {
                "ok": True,
                "skipped": False,
                "intent": "unsupported",
                "confidence": 1.0,
                "should_save": False,
                "should_reply": True,
                "phone": inbound.get("phone"),
                "phone_id": inbound.get("phone_id"),
                "reply_text": reply,
                "inbound": inbound,
            }
        })
        continue

    intent, confidence, slots = detect_intent(inbound.get("message_text") or "")
    mongo_document = None
    should_save = intent == "submit_daily_report"

    if should_save:
        mongo_document = build_mongo_document(inbound, slots, intent, confidence)

    reply_text = build_reply(intent, doc=mongo_document)

    out = {
        "ok": True,
        "skipped": False,
        "intent": intent,
        "confidence": confidence,
        "slots": slots,
        "should_save": should_save,
        "should_reply": True,
        "phone": inbound.get("phone"),
        "phone_id": inbound.get("phone_id"),
        "display_name": inbound.get("display_name"),
        "message_text": inbound.get("message_text"),
        "reply_text": reply_text,
        "report_id": (mongo_document or {}).get("report_id"),
        "inbound": inbound,
        # Pass this object into n8n MongoDB Insert (see README)
        "mongo_document": mongo_document,
        # Flat aliases if your Mongo node maps field-by-field
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
    results.append({"json": out})

return results
