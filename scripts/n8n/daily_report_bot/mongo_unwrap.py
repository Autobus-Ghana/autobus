"""
n8n Code node — after IF should_save, before MongoDB Insert.

Flattens json.mongo_document to the item root so MongoDB Insert
stores a clean daily_reports document (no workflow routing fields).

Keep the WhatsApp reply on a *parallel* branch from the NLU Code node
(IF should_reply → HTTP Request), not after this unwrap.

Language: Python | Mode: Run Once for All Items
"""

results = []
for item in _items:
    data = item.get("json") or {}
    doc = data.get("mongo_document")
    if not isinstance(doc, dict) or not doc.get("report_id"):
        continue
    # Pure Mongo document only
    results.append({"json": dict(doc)})

return results
