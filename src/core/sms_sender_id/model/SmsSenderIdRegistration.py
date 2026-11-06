from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from utilities.dbconfig import Base


class SmsSenderIdStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SmsSenderIdRegistration(Base):
    """User-submitted SMS sender ID awaiting Autobus team approval."""

    __tablename__ = "sms_sender_id_registrations"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)

    user_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.id"), nullable=False, index=True
    )

    sender_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    zernio_sender_id: Mapped[Optional[str]] = mapped_column(String(64))
    company_name: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[SmsSenderIdStatus] = mapped_column(
        SQLEnum(SmsSenderIdStatus, name="sms_sender_id_status", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=SmsSenderIdStatus.PENDING,
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(20), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "sender_id", name="uq_sms_sender_id_user_sender"),
    )
