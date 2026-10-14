"""Starter canvas for a custom workflow created from an automation request."""

from typing import Any, Dict, List, Tuple

MODULE_LABELS = {
    "intelligence": "Business intelligence",
    "customer_service": "Customer service",
    "digital_marketing": "Digital marketing (create content)",
    "multi_account_posting": "Multi-account posting",
}

_LABEL_TO_ID = {
    "business intelligence": "intelligence",
    "customer service": "customer_service",
    "digital marketing (create content)": "digital_marketing",
    "digital marketing": "digital_marketing",
    "creating content": "digital_marketing",
    "multi-account posting": "multi_account_posting",
    "multi account posting": "multi_account_posting",
    "intelligence": "intelligence",
    "customer_service": "customer_service",
    "digital_marketing": "digital_marketing",
    "multi_account_posting": "multi_account_posting",
}

_NODE_META = {
    "intelligence": ("autobus-intelligence", "Business intelligence"),
    "customer_service": ("autobus-customer-service", "Customer service"),
    "digital_marketing": ("autobus-marketing", "Create marketing content"),
    "multi_account_posting": ("autobus-social-post", "Post to connected accounts"),
}


def parse_requested_modules(survey: Dict[str, Any] | None) -> List[str]:
    raw = (survey or {}).get("modules")
    parts: List[str] = []
    if isinstance(raw, list):
        parts = [str(item) for item in raw]
    elif isinstance(raw, str):
        parts = raw.replace(",", "|").split("|")
    found: List[str] = []
    for part in parts:
        module_id = _LABEL_TO_ID.get(part.strip().lower())
        if module_id and module_id not in found:
            found.append(module_id)
    return found


def _trigger(survey: Dict[str, Any] | None, request_id: str) -> Tuple[str, str, Dict[str, Any]]:
    raw = str((survey or {}).get("trigger") or "").lower()
    if "schedule" in raw:
        return (
            "trigger-schedule",
            "Schedule Trigger",
            {"rule": {"interval": [{"field": "hours", "hoursInterval": 24}]}},
        )
    if "manual" in raw:
        return ("trigger-manual", "Manual Trigger", {})
    path = f"custom-{request_id.removeprefix('req-')}"
    return (
        "trigger-webhook",
        "Webhook Trigger",
        {
            "httpMethod": "POST",
            "path": path,
            "responseMode": "onReceived",
            "responseData": "allEntries",
        },
    )


def _module_parameters(module_id: str, business_user_id: str) -> Dict[str, Any]:
    shared = {"businessUserId": business_user_id or ""}
    if module_id == "digital_marketing":
        return {
            **shared,
            "prompt": '={{ $json.prompt || $json.message || $json.body?.message || "" }}',
        }
    if module_id == "multi_account_posting":
        return {
            **shared,
            "content": '={{ $json.content || $json.message || "" }}',
            "integrationIds": "",
            "mediaUrls": "",
            "publishNow": True,
            "tiktokPrivacy": "PUBLIC_TO_EVERYONE",
        }
    return {
        **shared,
        "message": '={{ $json.message || $json.body?.message || $json.text || "" }}',
    }


def build_starter_graph(
    request_id: str,
    business_user_id: str,
    survey: Dict[str, Any] | None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """Return React Flow nodes, edges, and a workflow category."""
    modules = parse_requested_modules(survey)
    trigger_type, trigger_label, trigger_params = _trigger(survey, request_id)
    nodes: List[Dict[str, Any]] = [
        {
            "id": "node-trigger",
            "type": "triggerNode",
            "position": {"x": 80, "y": 200},
            "data": {
                "label": trigger_label,
                "subType": trigger_type,
                "parameters": trigger_params,
                "status": "idle",
            },
        }
    ]
    edges: List[Dict[str, Any]] = []
    previous = "node-trigger"
    for index, module_id in enumerate(modules):
        sub_type, label = _NODE_META[module_id]
        node_id = f"node-{module_id}"
        nodes.append(
            {
                "id": node_id,
                "type": "actionNode",
                "position": {"x": 420 + index * 340, "y": 200},
                "data": {
                    "label": label,
                    "subType": sub_type,
                    "parameters": _module_parameters(module_id, business_user_id),
                    "status": "idle",
                },
            }
        )
        edges.append(
            {
                "id": f"edge-{index}",
                "source": previous,
                "target": node_id,
                "animated": True,
            }
        )
        previous = node_id

    if "customer_service" in modules and "digital_marketing" not in modules and "multi_account_posting" not in modules:
        category = "Customer Support"
    elif "digital_marketing" in modules or "multi_account_posting" in modules:
        category = "Marketing"
    else:
        category = "General"
    return nodes, edges, category
