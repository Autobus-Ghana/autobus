"""Read and update the credit price card and signup/referral grants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.credits.credit_catalog import (
    CREDIT_USD_VALUE,
    DEFAULT_PRICING_GROUPS,
    DEFAULT_REFERRAL_REFEREE_CREDITS,
    DEFAULT_REFERRAL_REFERRER_CREDITS,
    STARTER_CREDIT_GRANT,
)
from core.credits.model.credit_pricing import CreditGrantSettings, CreditPricingRule
from core.credits.model.credit_usage_log import CreditUsageLog


class CreditPricingService:
    def __init__(self, db: Session):
        self.db = db

    def ensure_defaults(self) -> None:
        existing = {row.group_key for row in self.db.query(CreditPricingRule.group_key).all()}
        added = False
        for item in DEFAULT_PRICING_GROUPS:
            if item["group_key"] in existing:
                continue
            self.db.add(
                CreditPricingRule(
                    group_key=item["group_key"],
                    label=item["label"],
                    description=item["description"],
                    unit_label=item["unit_label"],
                    credits_per_unit=float(item["credits_per_unit"]),
                    price_usd=float(item["price_usd"]),
                    sort_order=int(item["sort_order"]),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            added = True
        grant = self.db.query(CreditGrantSettings).filter(CreditGrantSettings.id == 1).first()
        if grant is None:
            self.db.add(
                CreditGrantSettings(
                    id=1,
                    free_credits=STARTER_CREDIT_GRANT,
                    referral_referrer_credits=DEFAULT_REFERRAL_REFERRER_CREDITS,
                    referral_referee_credits=DEFAULT_REFERRAL_REFEREE_CREDITS,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            added = True
        if added:
            self.db.commit()

    def cost_map(self) -> Dict[str, float]:
        self.ensure_defaults()
        rows = self.db.query(CreditPricingRule).all()
        found = {row.group_key: float(row.credits_per_unit or 0) for row in rows}
        for item in DEFAULT_PRICING_GROUPS:
            found.setdefault(item["group_key"], float(item["credits_per_unit"]))
        return found

    def grants(self) -> Dict[str, float]:
        self.ensure_defaults()
        row = self.db.query(CreditGrantSettings).filter(CreditGrantSettings.id == 1).first()
        if row is None:
            return {
                "free_credits": STARTER_CREDIT_GRANT,
                "referral_referrer_credits": DEFAULT_REFERRAL_REFERRER_CREDITS,
                "referral_referee_credits": DEFAULT_REFERRAL_REFEREE_CREDITS,
            }
        return {
            "free_credits": float(row.free_credits or 0),
            "referral_referrer_credits": float(row.referral_referrer_credits or 0),
            "referral_referee_credits": float(row.referral_referee_credits or 0),
        }

    def list_groups(self) -> List[Dict[str, Any]]:
        self.ensure_defaults()
        rows = (
            self.db.query(CreditPricingRule)
            .order_by(CreditPricingRule.sort_order.asc(), CreditPricingRule.label.asc())
            .all()
        )
        return [self._group_dict(row) for row in rows]

    def update_group(
        self,
        group_key: str,
        *,
        credits_per_unit: float,
        price_usd: float,
        label: str | None = None,
        description: str | None = None,
    ) -> Dict[str, Any]:
        self.ensure_defaults()
        row = self.db.query(CreditPricingRule).filter(CreditPricingRule.group_key == group_key).first()
        if row is None:
            raise KeyError(group_key)
        row.credits_per_unit = max(0.0, float(credits_per_unit))
        row.price_usd = max(0.0, float(price_usd))
        if label is not None and label.strip():
            row.label = label.strip()[:64]
        if description is not None:
            row.description = description.strip()
        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return self._group_dict(row)

    def update_grants(
        self,
        *,
        free_credits: float,
        referral_referrer_credits: float,
        referral_referee_credits: float,
    ) -> Dict[str, float]:
        self.ensure_defaults()
        row = self.db.query(CreditGrantSettings).filter(CreditGrantSettings.id == 1).one()
        row.free_credits = max(0.0, float(free_credits))
        row.referral_referrer_credits = max(0.0, float(referral_referrer_credits))
        row.referral_referee_credits = max(0.0, float(referral_referee_credits))
        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return self.grants()

    def usage_summary(self, days: int = 30) -> Dict[str, Any]:
        self.ensure_defaults()
        window = max(1, min(int(days), 365))
        since = datetime.now(timezone.utc) - timedelta(days=window)
        grouped = (
            self.db.query(
                CreditUsageLog.credit_type,
                func.count(CreditUsageLog.id),
                func.coalesce(func.sum(CreditUsageLog.amount), 0.0),
            )
            .filter(
                CreditUsageLog.created_at >= since,
                CreditUsageLog.amount > 0,
                CreditUsageLog.operation != "refund",
            )
            .group_by(CreditUsageLog.credit_type)
            .all()
        )
        by_key = {str(key): (int(count or 0), float(total or 0)) for key, count, total in grouped}
        rules = {row.group_key: row for row in self.db.query(CreditPricingRule).all()}
        groups: List[Dict[str, Any]] = []
        total_credits = 0.0
        total_usd = 0.0
        total_calls = 0
        for item in DEFAULT_PRICING_GROUPS:
            key = item["group_key"]
            calls, spent = by_key.get(key, (0, 0.0))
            rule = rules.get(key)
            unit_credits = float(rule.credits_per_unit if rule else item["credits_per_unit"])
            unit_usd = float(rule.price_usd if rule else item["price_usd"])
            units = (spent / unit_credits) if unit_credits > 0 else 0.0
            list_value = units * unit_usd
            wallet_value = spent * CREDIT_USD_VALUE
            total_credits += spent
            total_usd += list_value
            total_calls += calls
            groups.append(
                {
                    "group_key": key,
                    "label": rule.label if rule else item["label"],
                    "calls": calls,
                    "credits_used": round(spent, 4),
                    "list_value_usd": round(list_value, 4),
                    "wallet_value_usd": round(wallet_value, 4),
                }
            )
        return {
            "days": window,
            "calls": total_calls,
            "credits_used": round(total_credits, 4),
            "list_value_usd": round(total_usd, 4),
            "wallet_value_usd": round(total_credits * CREDIT_USD_VALUE, 4),
            "credit_usd_value": CREDIT_USD_VALUE,
            "groups": groups,
        }

    @staticmethod
    def _group_dict(row: CreditPricingRule) -> Dict[str, Any]:
        return {
            "group_key": row.group_key,
            "label": row.label,
            "description": row.description or "",
            "unit_label": row.unit_label,
            "credits_per_unit": float(row.credits_per_unit or 0),
            "price_usd": float(row.price_usd or 0),
            "sort_order": int(row.sort_order or 0),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
