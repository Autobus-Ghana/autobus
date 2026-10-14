import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.admin.dto.automation_request_dto import (
    AutomationRequestResponse,
    CreateAutomationRequestPayload,
    PortalSurveySubmitRequest,
)
from core.admin.model.automation_request import AutomationRequest
from core.admin.service.workflow_seed import MODULE_LABELS, build_starter_graph, parse_requested_modules
from core.ai_workflow.dto.ai_workflow_dto import WorkflowCreateRequest
from core.ai_workflow.model.ai_workflow import AIWorkflow
from core.ai_workflow.service.ai_workflow_service import AIWorkflowService
from core.user.model.User import User

logger = logging.getLogger(__name__)


class AutomationRequestService:
    def __init__(self, db: Session):
        self.db = db

    def _to_response(self, model: AutomationRequest) -> AutomationRequestResponse:
        created_str = (
            model.created_at.isoformat()
            if model.created_at
            else datetime.now(timezone.utc).isoformat()
        )
        return AutomationRequestResponse(
            id=model.id,
            title=model.title,
            description=model.description,
            category=model.category,
            priority=model.priority,
            status=model.status,
            requested_by=model.requested_by,
            created_at=created_str,
            assigned_to=model.assigned_to,
            workflow_id=model.workflow_id,
            business_name=model.business_name,
            contact_email=model.contact_email,
            contact_phone=model.contact_phone,
            survey_data=model.survey_data or {},
            user_id=model.user_id,
        )

    def list_all(
        self,
        q: Optional[str] = None,
        status_filter: Optional[str] = None,
        category: Optional[str] = None,
    ) -> List[AutomationRequestResponse]:
        query = self.db.query(AutomationRequest)

        if status_filter and status_filter.lower() != "all":
            query = query.filter(AutomationRequest.status == status_filter)

        if category and category.lower() != "all":
            query = query.filter(AutomationRequest.category == category)

        if q and q.strip():
            term = f"%{q.strip()}%"
            query = query.filter(
                or_(
                    AutomationRequest.title.ilike(term),
                    AutomationRequest.description.ilike(term),
                    AutomationRequest.requested_by.ilike(term),
                    AutomationRequest.category.ilike(term),
                    AutomationRequest.business_name.ilike(term),
                    AutomationRequest.contact_email.ilike(term),
                )
            )

        rows = query.order_by(AutomationRequest.created_at.desc()).all()
        return [self._to_response(r) for r in rows]

    def get_by_id(self, request_id: str) -> AutomationRequestResponse:
        req = self.db.query(AutomationRequest).filter(AutomationRequest.id == request_id).first()
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Automation request '{request_id}' not found",
            )
        return self._to_response(req)

    def create(
        self,
        payload: CreateAutomationRequestPayload,
        user: Optional[User] = None,
    ) -> AutomationRequestResponse:
        merged_survey = dict(payload.survey_data or {})
        if payload.answers:
            merged_survey.update(payload.answers)

        title = (payload.title or "").strip()
        if not title:
            biz = (payload.business_name or "").strip()
            title = f"Automation Request: {biz}" if biz else "New Automation Request"

        description = (payload.description or "").strip()
        if not description and merged_survey:
            # Construct readable summary from answers
            parts = [f"{k}: {v}" for k, v in merged_survey.items() if v]
            description = "\n".join(parts) if parts else "Automation request from survey."
        elif not description:
            description = "Custom automation request."

        req_by = payload.requested_by
        if not req_by and user:
            name_parts = [p for p in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if p]
            req_by = " ".join(name_parts) if name_parts else user.email
        if not req_by:
            req_by = payload.business_name or "Merchant"

        req = AutomationRequest(
            title=title,
            description=description,
            category=payload.category or "Other",
            priority=payload.priority or "Normal",
            status="Submitted",
            requested_by=req_by,
            business_name=payload.business_name or (user.company if user else None),
            contact_email=payload.contact_email or (user.email if user else None),
            contact_phone=payload.contact_phone or (user.phone if user else None),
            user_id=user.id if user else None,
            survey_data=merged_survey,
        )
        self.db.add(req)
        self.db.commit()
        self.db.refresh(req)
        logger.info("[AUTOMATION_REQUEST] Created request %s: %s", req.id, req.title)
        return self._to_response(req)

    def submit_portal_survey(
        self,
        payload: PortalSurveySubmitRequest,
        user: Optional[User] = None,
    ) -> AutomationRequestResponse:
        """Capture details submitted from the Autobus portal survey into the database."""
        merged_survey = dict(payload.survey_data or {})
        if payload.answers:
            merged_survey.update(payload.answers)

        biz_name = payload.business_name or (getattr(user, "company", None) if user else None)
        contact_name = payload.contact_name or (
            f"{user.first_name} {user.last_name}".strip()
            if user and getattr(user, "first_name", None)
            else (user.email if user else None)
        )
        contact_name = contact_name or biz_name or "Portal User"

        contact_email = payload.contact_email or (getattr(user, "email", None) if user else None)
        contact_phone = payload.contact_phone or (getattr(user, "phone", None) if user else None)

        title = (payload.title or "").strip()
        if not title:
            title = f"Portal Survey: {biz_name}" if biz_name else f"Automation Request - {contact_name}"

        description = (payload.description or "").strip()
        if not description:
            # Build structured text from answers for fast admin review
            details_list = []
            for q_key, answer in merged_survey.items():
                if answer:
                    formatted_key = q_key.replace("_", " ").title()
                    details_list.append(f"• {formatted_key}: {answer}")
            description = (
                "Submitted via Autobus Portal Survey:\n" + "\n".join(details_list)
                if details_list
                else "Portal automation survey submission."
            )

        req = AutomationRequest(
            title=title,
            description=description,
            category=payload.category or "Other",
            priority=payload.priority or "Normal",
            status="Submitted",
            requested_by=contact_name,
            business_name=biz_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            user_id=user.id if user else None,
            survey_data=merged_survey,
        )
        self.db.add(req)
        self.db.commit()
        self.db.refresh(req)
        logger.info(
            "[PORTAL_SURVEY] Successfully saved survey from '%s' (%s) as request %s",
            contact_name,
            biz_name,
            req.id,
        )
        return self._to_response(req)

    def start_workflow(self, request_id: str) -> AutomationRequestResponse:
        """Create the admin canvas for this request, seeded with the modules the business asked for."""
        req = self.db.query(AutomationRequest).filter(AutomationRequest.id == request_id).first()
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Automation request '{request_id}' not found",
            )
        if req.workflow_id:
            existing = self.db.query(AIWorkflow).filter(AIWorkflow.id == req.workflow_id).first()
            if existing:
                return self._to_response(req)

        modules = parse_requested_modules(req.survey_data)
        nodes, edges, category = build_starter_graph(
            req.id, req.user_id or "", req.survey_data
        )
        module_text = ", ".join(MODULE_LABELS[item] for item in modules) or "no modules selected yet"
        business = (req.business_name or "Merchant").strip()
        created = AIWorkflowService(self.db).create_workflow(
            WorkflowCreateRequest(
                name=f"Custom: {business}"[:200],
                businessName=business[:120] or "General",
                description=(
                    f"Built from automation request {req.id}. "
                    f"Requested modules: {module_text}.\n\n{req.description or ''}"
                )[:4000],
                category=category,
                active=False,
                nodes=nodes,
                edges=edges,
            )
        )
        req.workflow_id = created.id
        if req.status in {"Submitted", "Triaged", "Approved"}:
            req.status = "In progress"
        req.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(req)
        logger.info(
            "[AUTOMATION_REQUEST] Started workflow %s for request %s",
            created.id,
            request_id,
        )
        return self._to_response(req)

    def update_status(self, request_id: str, new_status: str) -> AutomationRequestResponse:
        req = self.db.query(AutomationRequest).filter(AutomationRequest.id == request_id).first()
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Automation request '{request_id}' not found",
            )
        req.status = new_status
        req.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(req)
        logger.info("[AUTOMATION_REQUEST] Updated status for %s to %s", request_id, new_status)
        return self._to_response(req)

    def delete(self, request_id: str) -> Dict[str, str]:
        req = self.db.query(AutomationRequest).filter(AutomationRequest.id == request_id).first()
        if not req:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Automation request '{request_id}' not found",
            )
        self.db.delete(req)
        self.db.commit()
        logger.info("[AUTOMATION_REQUEST] Deleted request %s", request_id)
        return {"message": "Automation request deleted successfully"}
