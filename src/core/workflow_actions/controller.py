"""Service endpoints n8n calls when a custom workflow uses an Autobus agent."""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from config import settings
from core.workflow_actions.dto import (
    CustomerServiceActionRequest,
    IntelligenceActionRequest,
    MarketingContentActionRequest,
    SocialPostActionRequest,
)
from core.workflow_actions.service import WorkflowActionService
from utilities.dbconfig import get_db

logger = logging.getLogger(__name__)

workflow_action_routes = APIRouter()


def require_workflow_key(
    x_workflow_key: str = Header(default="", alias="X-Workflow-Key"),
) -> None:
    expected = (settings.WORKFLOW_ACTION_KEY or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Workflow actions are not configured. Set WORKFLOW_ACTION_KEY on the API.",
        )
    provided = (x_workflow_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid workflow key.")


@workflow_action_routes.post("/intelligence")
def run_intelligence(
    payload: IntelligenceActionRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_workflow_key),
):
    """Business intelligence: answer from this merchant's live profile, products, orders, and inbox."""
    return WorkflowActionService(db).intelligence(payload)


@workflow_action_routes.post("/customer-service")
def run_customer_service(
    payload: CustomerServiceActionRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_workflow_key),
):
    """Customer service: draft a reply with the same agent that handles WhatsApp and inbox chats."""
    return WorkflowActionService(db).customer_service(payload)


@workflow_action_routes.post("/marketing-content")
def run_marketing_content(
    payload: MarketingContentActionRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_workflow_key),
):
    """Digital marketing: write caption or campaign copy. Does not publish it."""
    return WorkflowActionService(db).marketing_content(payload)


@workflow_action_routes.get("/social-accounts")
async def list_social_accounts(
    business_user_id: str = Query(..., alias="businessUserId"),
    db: Session = Depends(get_db),
    _: None = Depends(require_workflow_key),
):
    """Connected Postiz accounts for one business. Use the ids in a social-post step."""
    return await WorkflowActionService(db).list_social_accounts(business_user_id)


@workflow_action_routes.post("/social-post")
async def run_social_post(
    payload: SocialPostActionRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_workflow_key),
):
    """Multi-account posting: publish one caption to the Postiz accounts you name."""
    return await WorkflowActionService(db).social_post(payload)
