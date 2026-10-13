from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from utilities.dbconfig import Base


def _generate_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


class AutomationRequest(Base):
    """Automation request entity collected from the portal survey or created by admins."""

    __tablename__ = "automation_requests"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_generate_request_id
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(60), nullable=False, default="Other", index=True
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Normal"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="Submitted", index=True
    )
    requested_by: Mapped[str] = mapped_column(
        String(150), nullable=False, default="Merchant"
    )
    business_name: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True, index=True
    )
    contact_email: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    workflow_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("ai_workflows.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Stores the raw or structured survey answers, questionnaires, and additional requirements
    survey_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )


# Additional query indexes
Index("ix_automation_requests_status_category", AutomationRequest.status, AutomationRequest.category)
