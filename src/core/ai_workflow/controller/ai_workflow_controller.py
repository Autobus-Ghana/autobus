"""Platform Admin API for AI Workflow Automation Engine."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from core.ai_workflow.dto.ai_workflow_dto import (
    BusinessCreateRequest,
    BusinessResponse,
    WorkflowCreateRequest,
    WorkflowResponse,
    WorkflowSyncN8nRequest,
    WorkflowToggleActiveRequest,
    WorkflowUpdateRequest,
)
from core.ai_workflow.service.ai_workflow_service import AIWorkflowService
from core.auth.dependencies import require_admin
from core.user.model.User import User
from utilities.dbconfig import get_db

ai_workflow_routes = APIRouter()


# -------------------------------------------------------------
# Business Folder Endpoints
# -------------------------------------------------------------

@ai_workflow_routes.get(
    "/businesses",
    response_model=List[BusinessResponse],
    summary="List all workflow business folders",
)
def list_businesses(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).list_businesses()


@ai_workflow_routes.post(
    "/businesses",
    response_model=BusinessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new workflow business folder",
)
def create_business(
    payload: BusinessCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).create_business(payload)


@ai_workflow_routes.delete(
    "/businesses/{id_or_name}",
    status_code=status.HTTP_200_OK,
    summary="Delete a business folder and its workflows",
)
def delete_business(
    id_or_name: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AIWorkflowService(db).delete_business(id_or_name)
    return {"success": True, "message": f"Business '{id_or_name}' deleted."}


# -------------------------------------------------------------
# Workflow Endpoints
# -------------------------------------------------------------

@ai_workflow_routes.get(
    "",
    response_model=List[WorkflowResponse],
    summary="List all saved AI workflows",
)
def list_workflows(
    business_name: Optional[str] = Query(None, alias="businessName"),
    category: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    q: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).list_workflows(
        business_name=business_name,
        category=category,
        active=active,
        search=q,
    )


@ai_workflow_routes.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Get single workflow by ID",
)
def get_workflow(
    workflow_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).get_workflow(workflow_id)


@ai_workflow_routes.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new workflow",
)
def create_workflow(
    payload: WorkflowCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).create_workflow(payload)


@ai_workflow_routes.put(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Save and update workflow",
)
def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).update_workflow(workflow_id, payload)


@ai_workflow_routes.patch(
    "/{workflow_id}/active",
    response_model=WorkflowResponse,
    summary="Toggle or set active status of a workflow",
)
def toggle_workflow_active(
    workflow_id: str,
    payload: Optional[WorkflowToggleActiveRequest] = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    new_active = payload.active if payload else None
    return AIWorkflowService(db).toggle_active(workflow_id, new_active)


@ai_workflow_routes.post(
    "/{workflow_id}/duplicate",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate an existing workflow",
)
def duplicate_workflow(
    workflow_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).duplicate_workflow(workflow_id)


@ai_workflow_routes.delete(
    "/{workflow_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a workflow",
)
def delete_workflow(
    workflow_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AIWorkflowService(db).delete_workflow(workflow_id)
    return {"success": True, "message": f"Workflow '{workflow_id}' deleted."}


@ai_workflow_routes.patch(
    "/{workflow_id}/n8n-sync",
    response_model=WorkflowResponse,
    summary="Update n8n sync ID for a workflow",
)
def sync_n8n(
    workflow_id: str,
    payload: WorkflowSyncN8nRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AIWorkflowService(db).sync_n8n(workflow_id, payload.resolved_id())
