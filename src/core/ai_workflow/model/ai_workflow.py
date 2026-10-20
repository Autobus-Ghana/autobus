from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from utilities.dbconfig import Base


def _generate_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AIWorkflowBusiness(Base):
    """Business folder entity representing a merchant/business organizing workflows."""

    __tablename__ = "ai_workflow_businesses"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _generate_id("biz")
    )
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )

    # Relationships
    workflows: Mapped[List["AIWorkflow"]] = relationship(
        "AIWorkflow",
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AIWorkflow(Base):
    """Saved AI automation workflow model storing graph nodes and edges."""

    __tablename__ = "ai_workflows"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _generate_id("wf")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    business_name: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    business_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("ai_workflow_businesses.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="General")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    nodes: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    edges: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    n8n_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    stats: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
        default=lambda: {
            "totalRuns": 0,
            "lastRunAt": "Never",
            "lastRunStatus": "idle",
        },
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )

    # Relationships
    business: Mapped[Optional[AIWorkflowBusiness]] = relationship(
        "AIWorkflowBusiness", back_populates="workflows"
    )


# Indexes for optimal querying
Index("ix_ai_workflows_business_active", AIWorkflow.business_name, AIWorkflow.active)
Index("ix_ai_workflows_category", AIWorkflow.category)


class AIWorkflowAssistantSettings(Base):
    """Single-row provider settings for the workflow assistant. The API key is encrypted."""

    __tablename__ = "ai_workflow_assistant_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="openai/gpt-oss-120b")
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )
