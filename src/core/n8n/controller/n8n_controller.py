"""Admin-only bridge between the admin portal workflow builder and the self-hosted n8n instance."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from config import settings
from core.auth.dependencies import require_admin
from core.n8n.service.n8n_client import N8nAPIError, N8nClient
from core.user.model.User import User

n8n_routes = APIRouter()


class N8nWorkflowPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    nodes: List[Dict[str, Any]]
    connections: Dict[str, Any]
    settings: Dict[str, Any] = Field(default_factory=lambda: {"executionOrder": "v1"})


def _client() -> N8nClient:
    return N8nClient()


async def _call(coro):
    try:
        return await coro
    except N8nAPIError as exc:
        status = exc.status_code if 400 <= exc.status_code < 600 else 502
        # n8n auth failures are a server misconfiguration, not the admin's session.
        if status in (401, 403):
            status = 502
        raise HTTPException(status_code=status, detail=str(exc))


@n8n_routes.get("/status")
async def n8n_status(admin: User = Depends(require_admin)):
    _ = admin
    client = _client()
    reachable = await client.healthy()
    authenticated = False
    message = "n8n is not reachable from the backend."
    if reachable and not client.configured:
        message = "n8n is running but N8N_API_KEY is not set on the backend."
    elif reachable:
        try:
            await client.list_workflows(limit=1)
            authenticated = True
            message = "Connected to n8n."
        except N8nAPIError as exc:
            message = f"n8n rejected the backend API key ({exc.status_code})."
    return {
        "reachable": reachable,
        "authenticated": authenticated,
        "message": message,
        "editorUrl": settings.N8N_PUBLIC_URL,
    }


@n8n_routes.get("/workflows")
async def list_workflows(
    limit: int = Query(100, ge=1, le=250),
    cursor: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
):
    _ = admin
    return await _call(_client().list_workflows(limit=limit, cursor=cursor))


@n8n_routes.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, admin: User = Depends(require_admin)):
    _ = admin
    return await _call(_client().get_workflow(workflow_id))


@n8n_routes.post("/workflows")
async def create_workflow(payload: N8nWorkflowPayload, admin: User = Depends(require_admin)):
    _ = admin
    return await _call(_client().create_workflow(payload.model_dump()))


@n8n_routes.put("/workflows/{workflow_id}")
async def update_workflow(
    workflow_id: str, payload: N8nWorkflowPayload, admin: User = Depends(require_admin)
):
    _ = admin
    return await _call(_client().update_workflow(workflow_id, payload.model_dump()))


@n8n_routes.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, admin: User = Depends(require_admin)):
    _ = admin
    return await _call(_client().delete_workflow(workflow_id))


@n8n_routes.post("/workflows/{workflow_id}/activate")
async def activate_workflow(workflow_id: str, admin: User = Depends(require_admin)):
    _ = admin
    return await _call(_client().set_active(workflow_id, True))


@n8n_routes.post("/workflows/{workflow_id}/deactivate")
async def deactivate_workflow(workflow_id: str, admin: User = Depends(require_admin)):
    _ = admin
    return await _call(_client().set_active(workflow_id, False))


@n8n_routes.get("/executions")
async def list_executions(
    workflow_id: Optional[str] = Query(None, alias="workflowId"),
    status: Optional[str] = Query(None, pattern="^(error|success|waiting|running|canceled)$"),
    limit: int = Query(20, ge=1, le=250),
    admin: User = Depends(require_admin),
):
    _ = admin
    return await _call(_client().list_executions(workflow_id=workflow_id, limit=limit, status=status))


@n8n_routes.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    include_data: bool = Query(False, alias="includeData"),
    admin: User = Depends(require_admin),
):
    _ = admin
    return await _call(_client().get_execution(execution_id, include_data=include_data))


@n8n_routes.post("/webhooks/{path:path}")
async def trigger_webhook(
    path: str,
    payload: Any = Body(default_factory=dict),
    test: bool = Query(False),
    admin: User = Depends(require_admin),
):
    _ = admin
    return await _call(_client().trigger_webhook(path, payload, test=test))
