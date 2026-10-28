"""Admin-editable credit prices and signup/referral grants."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from utilities.dbconfig import Base


class CreditPricingRule(Base):
    """How many credits one unit of an API group costs, and its list price."""

    __tablename__ = "credit_pricing_rules"

    group_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    unit_label: Mapped[str] = mapped_column(String(32), nullable=False)
    credits_per_unit: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CreditGrantSettings(Base):
    """Singleton row (id=1) for free signup credits and referral bonuses."""

    __tablename__ = "credit_grant_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    free_credits: Mapped[float] = mapped_column(Float, nullable=False, default=15)
    referral_referrer_credits: Mapped[float] = mapped_column(Float, nullable=False, default=5)
    referral_referee_credits: Mapped[float] = mapped_column(Float, nullable=False, default=5)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
