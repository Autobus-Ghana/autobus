"""
OPTIONAL scheduled workflow Code node — build a CTO summary prompt from Mongo docs.

Workflow (example, end of day):
  Cron → MongoDB Find { report_date, summarized: false } on daily_reports
       → THIS NODE → HTTP Request (LLM) → WhatsApp / Email to CTO
       → MongoDB Update { summarized: true } for those report_ids

Paste into Code node (Python Native, Run Once for All Items).
Assumes each input item is one Mongo document (or one item with reports[] array).
"""

from datetime import datetime, timezone


def _collect_reports(items):
    reports = []
    for item in items:
        data = item.get("json") or {}
        if isinstance(data.get("reports"), list):
            reports.extend(data["reports"])
        else:
            reports.append(data)
    return reports


reports = _collect_reports(_items)
if not reports:
    return [{"json": {"ok": True, "empty": True, "should_send": False}}]

lines = []
report_ids = []
for r in reports:
    rid = r.get("report_id") or r.get("db_report_id") or ""
    if rid:
        report_ids.append(rid)
    name = r.get("sender_name") or r.get("db_sender_name") or r.get("sender_phone") or "Unknown"
    phone = r.get("sender_phone") or r.get("db_sender_phone") or ""
    body = r.get("body") or r.get("db_body") or ""
    done = r.get("done") or r.get("db_done") or ""
    blockers = r.get("blockers") or r.get("db_blockers") or ""
    plans = r.get("plans") or r.get("db_plans") or ""
    chunk = [f"### {name} ({phone}) — {rid}"]
    if done:
        chunk.append(f"Done: {done}")
    if blockers:
        chunk.append(f"Blockers: {blockers}")
    if plans:
        chunk.append(f"Plans: {plans}")
    if body and body not in (done, blockers, plans):
        chunk.append(body)
    lines.append("\n".join(chunk))

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
bundle = "\n\n".join(lines)

system = (
    "You are an executive assistant. Summarize these team daily reports for the CTO. "
    "Be concise. Structure: Highlights, Blockers/Risks, Asks/Decisions needed. "
    "Do not invent facts."
)

return [{
    "json": {
        "ok": True,
        "empty": False,
        "should_send": True,
        "report_date": today,
        "report_count": len(reports),
        "report_ids": report_ids,
        "raw_bundle": bundle,
        "llm_request": {
            "model": "openai/gpt-4o-mini",
            "temperature": 0.2,
            "max_tokens": 800,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Daily reports for {today}:\n\n{bundle}"},
            ],
        },
    }
}]
