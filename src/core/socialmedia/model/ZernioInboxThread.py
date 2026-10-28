"""Maps an Autobus conversation key to a Zernio inbox conversation."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from utilities.dbconfig import Base


class ZernioInboxThread(Base):
    __tablename__ = "zernio_inbox_threads"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.id"), nullable=False, index=True
    )
    autobus_key: Mapped[str] = mapped_column(String(160), nullable=False)
    zernio_conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    zernio_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    participant_username: Mapped[Optional[str]] = mapped_column(String(255))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("autobus_key", name="uq_zernio_inbox_autobus_key"),
    )
