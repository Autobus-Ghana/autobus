from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.ai_workflow.dto.ai_workflow_dto import (
    BusinessCreateRequest,
    BusinessResponse,
    WorkflowCreateRequest,
    WorkflowResponse,
    WorkflowUpdateRequest,
)
from core.ai_workflow.model.ai_workflow import AIWorkflow, AIWorkflowBusiness

logger = logging.getLogger(__name__)


def _iso(dt: Optional[datetime]) -> str:
    if not dt:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.isoformat()


class AIWorkflowService:
    def __init__(self, db: Session):
        self.db = db

    # ---------------------------------------------------------
    # Business Folder Operations
    # ---------------------------------------------------------

    def ensure_business(self, name: str) -> AIWorkflowBusiness:
        cleaned = name.strip()
        if not cleaned:
            cleaned = "General"

        biz = (
            self.db.query(AIWorkflowBusiness)
            .filter(func.lower(AIWorkflowBusiness.name) == func.lower(cleaned))
            .first()
        )
        if not biz:
            biz = AIWorkflowBusiness(
                id=f"biz-{uuid.uuid4().hex[:10]}",
                name=cleaned,
                description=f"Automations workspace for {cleaned}",
            )
            self.db.add(biz)
            self.db.commit()
            self.db.refresh(biz)
        return biz

    def list_businesses(self) -> List[BusinessResponse]:
        # Fetch all businesses
        businesses = (
            self.db.query(AIWorkflowBusiness)
            .order_by(AIWorkflowBusiness.name.asc())
            .all()
        )

        # Get workflow count per business
        counts = dict(
            self.db.query(
                AIWorkflow.business_name, func.count(AIWorkflow.id)
            )
            .group_by(AIWorkflow.business_name)
            .all()
        )

        results: List[BusinessResponse] = []
        seen_names = set()

        for b in businesses:
            seen_names.add(b.name.lower())
            count = counts.get(b.name, 0)
            results.append(
                BusinessResponse(
                    id=b.id,
                    name=b.name,
                    description=b.description,
                    workflow_count=count,
                    created_at=_iso(b.created_at),
                    updated_at=_iso(b.updated_at) if b.updated_at else None,
                )
            )

        # If there are workflows whose business_name is not yet in businesses table, sync them
        for biz_name, count in counts.items():
            if biz_name and biz_name.lower() not in seen_names:
                new_biz = self.ensure_business(biz_name)
                seen_names.add(biz_name.lower())
                results.append(
                    BusinessResponse(
                        id=new_biz.id,
                        name=new_biz.name,
                        description=new_biz.description,
                        workflow_count=count,
                        created_at=_iso(new_biz.created_at),
                        updated_at=_iso(new_biz.updated_at) if new_biz.updated_at else None,
                    )
                )

        return sorted(results, key=lambda x: x.name.lower())

    def create_business(self, payload: BusinessCreateRequest) -> BusinessResponse:
        cleaned = payload.name.strip()
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Business name cannot be empty.",
            )

        existing = (
            self.db.query(AIWorkflowBusiness)
            .filter(func.lower(AIWorkflowBusiness.name) == func.lower(cleaned))
            .first()
        )
        if existing:
            # Count existing workflows
            count = (
                self.db.query(func.count(AIWorkflow.id))
                .filter(AIWorkflow.business_name == existing.name)
                .scalar()
                or 0
            )
            return BusinessResponse(
                id=existing.id,
                name=existing.name,
                description=existing.description,
                workflow_count=count,
                created_at=_iso(existing.created_at),
                updated_at=_iso(existing.updated_at) if existing.updated_at else None,
            )

        biz = AIWorkflowBusiness(
            id=f"biz-{uuid.uuid4().hex[:10]}",
            name=cleaned,
            description=payload.description or f"Automations workspace for {cleaned}",
        )
        self.db.add(biz)
        self.db.commit()
        self.db.refresh(biz)

        return BusinessResponse(
            id=biz.id,
            name=biz.name,
            description=biz.description,
            workflow_count=0,
            created_at=_iso(biz.created_at),
            updated_at=_iso(biz.updated_at) if biz.updated_at else None,
        )

    def delete_business(self, business_id_or_name: str) -> None:
        target = (
            self.db.query(AIWorkflowBusiness)
            .filter(
                or_(
                    AIWorkflowBusiness.id == business_id_or_name,
                    func.lower(AIWorkflowBusiness.name) == func.lower(business_id_or_name),
                )
            )
            .first()
        )
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Business folder not found",
            )

        # Delete all workflows under this business as well
        self.db.query(AIWorkflow).filter(
            or_(
                AIWorkflow.business_id == target.id,
                func.lower(AIWorkflow.business_name) == func.lower(target.name),
            )
        ).delete(synchronize_session=False)

        self.db.delete(target)
        self.db.commit()

    # ---------------------------------------------------------
    # Workflow Operations
    # ---------------------------------------------------------

    def _to_response(self, wf: AIWorkflow) -> WorkflowResponse:
        return WorkflowResponse(
            id=wf.id,
            name=wf.name,
            businessName=wf.business_name,
            description=wf.description or "",
            category=wf.category or "General",
            active=bool(wf.active),
            createdAt=_iso(wf.created_at),
            updatedAt=_iso(wf.updated_at) if wf.updated_at else _iso(wf.created_at),
            nodes=wf.nodes or [],
            edges=wf.edges or [],
            n8nId=wf.n8n_id,
            stats=wf.stats
            or {
                "totalRuns": 0,
                "lastRunAt": "Never",
                "lastRunStatus": "idle",
            },
        )

    def list_workflows(
        self,
        business_name: Optional[str] = None,
        category: Optional[str] = None,
        active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[WorkflowResponse]:
        query = self.db.query(AIWorkflow)

        if business_name:
            query = query.filter(
                func.lower(AIWorkflow.business_name) == func.lower(business_name.strip())
            )

        if category and category != "All":
            query = query.filter(AIWorkflow.category == category.strip())

        if active is not None:
            query = query.filter(AIWorkflow.active == active)

        if search:
            term = f"%{search.strip().lower()}%"
            query = query.filter(
                or_(
                    func.lower(AIWorkflow.name).ilike(term),
                    func.lower(AIWorkflow.description).ilike(term),
                    func.lower(AIWorkflow.business_name).ilike(term),
                )
            )

        rows = query.order_by(AIWorkflow.updated_at.desc(), AIWorkflow.created_at.desc()).all()
        return [self._to_response(row) for row in rows]

    def get_workflow(self, workflow_id: str) -> WorkflowResponse:
        wf = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not wf:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )
        return self._to_response(wf)

    def create_workflow(self, payload: WorkflowCreateRequest) -> WorkflowResponse:
        biz_name = payload.resolved_business_name()
        biz = self.ensure_business(biz_name)

        workflow_id = f"wf-{int(datetime.now().timestamp() * 1000)}"

        wf = AIWorkflow(
            id=workflow_id,
            name=payload.name.strip(),
            business_name=biz.name,
            business_id=biz.id,
            description=payload.description or "Custom automated workflow.",
            category=payload.category or "General",
            active=payload.active,
            nodes=payload.nodes or [],
            edges=payload.edges or [],
            n8n_id=payload.resolved_n8n_id(),
            stats=payload.stats
            or {
                "totalRuns": 0,
                "lastRunAt": "Never",
                "lastRunStatus": "idle",
            },
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.db.add(wf)
        self.db.commit()
        self.db.refresh(wf)
        return self._to_response(wf)

    def update_workflow(
        self, workflow_id: str, payload: WorkflowUpdateRequest
    ) -> WorkflowResponse:
        wf = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not wf:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )

        if payload.name is not None:
            wf.name = payload.name.strip()

        biz_name = payload.resolved_business_name()
        if biz_name is not None:
            biz = self.ensure_business(biz_name)
            wf.business_name = biz.name
            wf.business_id = biz.id

        if payload.description is not None:
            wf.description = payload.description

        if payload.category is not None:
            wf.category = payload.category

        if payload.active is not None:
            wf.active = payload.active

        if payload.nodes is not None:
            wf.nodes = payload.nodes

        if payload.edges is not None:
            wf.edges = payload.edges

        if payload.resolved_n8n_id() is not None:
            wf.n8n_id = payload.resolved_n8n_id()

        if payload.stats is not None:
            wf.stats = payload.stats

        wf.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(wf)
        return self._to_response(wf)

    def toggle_active(
        self, workflow_id: str, new_active: Optional[bool] = None
    ) -> WorkflowResponse:
        wf = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not wf:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )

        if new_active is not None:
            wf.active = new_active
        else:
            wf.active = not bool(wf.active)

        wf.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(wf)
        return self._to_response(wf)

    def duplicate_workflow(self, workflow_id: str) -> WorkflowResponse:
        target = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )

        clone_id = f"wf-{int(datetime.now().timestamp() * 1000)}"

        # Deep-copy nodes with fresh ids
        old_to_new_node_ids = {}
        cloned_nodes = []
        for node in target.nodes or []:
            old_id = node.get("id", "")
            fresh_id = f"{old_id}-copy-{uuid.uuid4().hex[:4]}"
            old_to_new_node_ids[old_id] = fresh_id
            cloned_nodes.append({**node, "id": fresh_id})

        # Remap edges
        cloned_edges = []
        for edge in target.edges or []:
            source = old_to_new_node_ids.get(edge.get("source"), edge.get("source"))
            target_id = old_to_new_node_ids.get(edge.get("target"), edge.get("target"))
            cloned_edges.append(
                {
                    **edge,
                    "id": f"e-{uuid.uuid4().hex[:8]}",
                    "source": source,
                    "target": target_id,
                }
            )

        cloned_wf = AIWorkflow(
            id=clone_id,
            name=f"{target.name} (Copy)",
            business_name=target.business_name,
            business_id=target.business_id,
            description=target.description,
            category=target.category,
            active=False,
            nodes=cloned_nodes,
            edges=cloned_edges,
            n8n_id=None,
            stats={
                "totalRuns": 0,
                "lastRunAt": "Never",
                "lastRunStatus": "idle",
            },
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.db.add(cloned_wf)
        self.db.commit()
        self.db.refresh(cloned_wf)
        return self._to_response(cloned_wf)

    def delete_workflow(self, workflow_id: str) -> None:
        wf = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not wf:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )
        self.db.delete(wf)
        self.db.commit()

    def sync_n8n(self, workflow_id: str, n8n_id: str) -> WorkflowResponse:
        wf = self.db.query(AIWorkflow).filter(AIWorkflow.id == workflow_id).first()
        if not wf:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found.",
            )
        wf.n8n_id = n8n_id.strip()
        wf.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(wf)
        return self._to_response(wf)
