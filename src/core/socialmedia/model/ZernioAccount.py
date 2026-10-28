"""Social account connected through Zernio (Instagram, Facebook, TikTok)."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from utilities.dbconfig import Base


class ZernioAccount(Base):
    __tablename__ = "zernio_accounts"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.id"), nullable=False, index=True
    )
    zernio_profile_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    zernio_account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    platform_user_id: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    messaging_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    publishing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_post_id: Mapped[Optional[str]] = mapped_column(String(64))

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("zernio_account_id", name="uq_zernio_account_remote"),
    )
