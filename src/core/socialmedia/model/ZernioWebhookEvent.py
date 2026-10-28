"""Processed Zernio webhook ids, so at-least-once delivery is applied once."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from utilities.dbconfig import Base


class ZernioWebhookEvent(Base):
    __tablename__ = "zernio_webhook_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
