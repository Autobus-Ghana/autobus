import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.credits.credit_catalog import (
    CREDIT_ACTIVITIES,
    FEATURE_CREDIT_COSTS,
    WALLET_CREDIT_TYPE,
    feature_cost,
    format_credits,
    get_pack,
    packs_public,
)
from core.credits.model.credit_purchase import CreditPurchase
from core.credits.model.credit_types import ALL_CREDIT_TYPES, CREDIT_TYPE_LABELS
from core.credits.model.credit_usage_log import CreditUsageLog
from core.credits.model.user_credit_balance import UserCreditBalance
from core.user.model.User import User

logger = logging.getLogger(__name__)

CUSTOMER_CHANNEL_PAUSED = "Thanks for your message. This assistant is paused right now."

_ACTIVITY_LABELS = {
    "image_generation": "Generated an image",
    "video_generation": "Generated a video",
    "generation_reference": "Used a reference",
    "media_save": "Saved media",
    "video_concat": "Assembled a video",
    "social_post": "Published a post",
    "workflow_social_post": "Published a post",
    "customer_sms": "Sent an SMS",
    "customer_email": "Sent an email",
    "notification_sms": "Sent an SMS",
    "notification_bulk_sms": "Sent SMS messages",
    "nlu_message": "AI chat",
    "nlu_detect": "AI chat",
    "owner_agent": "AI assistant",
    "owner_agent_image": "Generated an image",
    "owner_agent_video": "Generated a video",
    "intelligence_chat": "AI assistant",
    "agent_command": "AI assistant",
    "embed_chat": "Customer chat",
    "instagram_inbound": "Instagram reply",
    "whatsapp_inbound": "WhatsApp reply",
    "whatsapp_image": "WhatsApp reply",
    "whatsapp_audio": "WhatsApp reply",
    "simple_chat": "Customer chat",
    "zernio_inbox": "Inbox reply",
    "rag_upload": "Uploaded a file",
    "multi_upload": "Uploaded files",
    "storage_upload": "Uploaded a file",
    "llm_chat": "AI chat",
    "email_send": "Sent an email",
    "sms_send": "Sent an SMS",
    "workflow_intelligence": "Workflow AI",
    "workflow_customer_service": "Workflow chat",
    "workflow_marketing_content": "Marketing text",
}

_WALLET_PERIOD_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WALLET_PERIOD_END = datetime(2099, 1, 1, tzinfo=timezone.utc)


class CreditService:
    def __init__(self, db: Session):
        self.db = db

    def resolve_user_id(self, identifier: Optional[str]) -> Optional[str]:
        """Resolve internal user id from id, email, or phone."""
        if not identifier:
            return None
        user = (
            self.db.query(User)
            .filter(
                (User.id == identifier)
                | (User.email == identifier)
                | (User.phone == identifier)
            )
            .first()
        )
        return user.id if user else None

    def _ensure_internal_user_id(self, identifier: str) -> str:
        user_id = self.resolve_user_id(identifier)
        if not user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user_id

    def _wallet_row(self, user_id: str) -> Optional[UserCreditBalance]:
        return (
            self.db.query(UserCreditBalance)
            .filter(
                UserCreditBalance.user_id == user_id,
                UserCreditBalance.credit_type == WALLET_CREDIT_TYPE,
            )
            .first()
        )

    def _legacy_category_rows(self, user_id: str) -> list[UserCreditBalance]:
        return (
            self.db.query(UserCreditBalance)
            .filter(
                UserCreditBalance.user_id == user_id,
                UserCreditBalance.credit_type != WALLET_CREDIT_TYPE,
            )
            .all()
        )

    def _legacy_wallet_value(self, user_id: str) -> float:
        total = 0.0
        for row in self._legacy_category_rows(user_id):
            unit = float(self._costs().get(row.credit_type, FEATURE_CREDIT_COSTS.get(row.credit_type, 0.0)))
            total += max(0.0, float(row.remaining or 0)) * unit
        return total

    def _create_wallet(self, user_id: str, amount: float, allocated: Optional[float] = None) -> UserCreditBalance:
        value = max(0.0, float(amount))
        wallet = UserCreditBalance(
            user_id=user_id,
            subscription_id=None,
            credit_type=WALLET_CREDIT_TYPE,
            allocated=float(allocated if allocated is not None else value),
            remaining=value,
            period_start=_WALLET_PERIOD_START,
            period_end=_WALLET_PERIOD_END,
        )
        self.db.add(wallet)
        self.db.commit()
        self.db.refresh(wallet)
        return wallet

    def ensure_wallet(self, user_id: str) -> UserCreditBalance:
        """Lifetime wallet. Migrates leftover category credits, then grants starter."""
        user_id = self._ensure_internal_user_id(user_id)
        wallet = self._wallet_row(user_id)
        if wallet:
            return wallet

        converted = self._legacy_wallet_value(user_id)
        if converted > 0:
            return self._create_wallet(user_id, converted, allocated=converted)

        grant = self._free_credits()
        return self._create_wallet(user_id, grant, allocated=grant)

    def grant_starter_credits(self, user_id: str) -> None:
        """Idempotent signup grant."""
        try:
            self.ensure_wallet(user_id)
        except Exception as exc:
            logger.warning("Starter credit grant failed for %s: %s", user_id, exc)

    def add_credits(
        self,
        user_id: str,
        amount: float,
        operation: str = "purchase",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UserCreditBalance:
        user_id = self._ensure_internal_user_id(user_id)
        wallet = self.ensure_wallet(user_id)
        delta = max(0.0, float(amount))
        wallet.remaining = float(wallet.remaining) + delta
        wallet.allocated = float(wallet.allocated) + delta
        wallet.updated_at = datetime.now(timezone.utc)
        self.db.add(
            CreditUsageLog(
                user_id=user_id,
                credit_type=WALLET_CREDIT_TYPE,
                amount=-delta,
                operation=operation,
                metadata_json=json.dumps(metadata) if metadata else None,
            )
        )
        self.db.commit()
        self.db.refresh(wallet)
        return wallet

    def grant_pack(
        self,
        user_id: str,
        pack_id: str,
        provider: str,
        transaction_id: str,
        product_id: Optional[str] = None,
        amount: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Grant a catalog pack once per provider+transaction_id."""
        user_id = self._ensure_internal_user_id(user_id)
        pack = get_pack(pack_id)
        if not pack:
            return {"success": False, "message": f"Unknown credit pack '{pack_id}'"}

        txn = (transaction_id or "").strip()
        if not txn:
            return {"success": False, "message": "Missing purchase transaction id"}

        existing = (
            self.db.query(CreditPurchase)
            .filter(
                CreditPurchase.provider == provider,
                CreditPurchase.transaction_id == txn,
            )
            .first()
        )
        if existing:
            wallet = self.ensure_wallet(user_id)
            return {
                "success": True,
                "already_granted": True,
                "message": "Credits already applied for this purchase",
                "pack_id": existing.pack_id,
                "credits_granted": existing.credits,
                "wallet_remaining": wallet.remaining,
            }

        credits = float(pack["credits"])
        purchase = CreditPurchase(
            user_id=user_id,
            pack_id=pack["id"],
            credits=credits,
            provider=provider,
            transaction_id=txn,
            product_id=product_id,
            amount=amount,
            status="completed",
        )
        self.db.add(purchase)
        try:
            self.db.flush()
        except Exception:
            self.db.rollback()
            existing = (
                self.db.query(CreditPurchase)
                .filter(
                    CreditPurchase.provider == provider,
                    CreditPurchase.transaction_id == txn,
                )
                .first()
            )
            if existing:
                wallet = self.ensure_wallet(user_id)
                return {
                    "success": True,
                    "already_granted": True,
                    "message": "Credits already applied for this purchase",
                    "pack_id": existing.pack_id,
                    "credits_granted": existing.credits,
                    "wallet_remaining": wallet.remaining,
                }
            raise

        wallet = self.add_credits(
            user_id,
            credits,
            operation=f"{provider}_pack",
            metadata={"pack_id": pack["id"], "transaction_id": txn},
        )
        return {
            "success": True,
            "already_granted": False,
            "message": f"{int(credits) if credits == int(credits) else credits} credits added",
            "pack_id": pack["id"],
            "credits_granted": credits,
            "wallet_remaining": wallet.remaining,
        }

    def refund_purchase(self, provider: str, transaction_id: str, reason: str) -> bool:
        purchase = (
            self.db.query(CreditPurchase)
            .filter(
                CreditPurchase.provider == provider,
                CreditPurchase.transaction_id == transaction_id,
            )
            .first()
        )
        if not purchase or purchase.status == "refunded":
            return False
        wallet = self._wallet_row(purchase.user_id)
        clawback = min(float(purchase.credits), float(wallet.remaining) if wallet else 0.0)
        if wallet and clawback > 0:
            wallet.remaining = max(0.0, float(wallet.remaining) - clawback)
            wallet.updated_at = datetime.now(timezone.utc)
        purchase.status = "refunded"
        purchase.updated_at = datetime.now(timezone.utc)
        self.db.add(
            CreditUsageLog(
                user_id=purchase.user_id,
                credit_type=WALLET_CREDIT_TYPE,
                amount=clawback,
                operation="refund",
                metadata_json=json.dumps({"reason": reason, "transaction_id": transaction_id}),
            )
        )
        self.db.commit()
        return True

    def get_user_credits(self, user_id: str) -> Dict[str, Any]:
        wallet = self.ensure_wallet(user_id)
        remaining = float(wallet.remaining)
        allocated = float(wallet.allocated)
        used = max(0.0, allocated - remaining)

        costs = self._costs()
        credits: Dict[str, Dict[str, Any]] = {}
        for credit_type in ALL_CREDIT_TYPES:
            unit = float(costs.get(credit_type, FEATURE_CREDIT_COSTS.get(credit_type, 1.0)))
            actions = remaining / unit if unit > 0 else remaining
            credits[credit_type] = {
                "credit_type": credit_type,
                "label": CREDIT_TYPE_LABELS.get(credit_type, credit_type),
                "allocated": allocated / unit if unit > 0 else allocated,
                "remaining": actions,
                "used": max(0.0, (allocated - remaining) / unit) if unit > 0 else used,
                "wallet_cost": unit,
                "period_start": wallet.period_start.isoformat(),
                "period_end": wallet.period_end.isoformat(),
            }

        return {
            "user_id": user_id,
            "plan_id": None,
            "plan_name": "Credits",
            "has_active_subscription": True,
            "wallet": {
                "remaining": remaining,
                "allocated": allocated,
                "used": used,
            },
            "costs": costs,
            "packs": packs_public(),
            "credits": credits,
        }

    def get_remaining(self, user_id: str, credit_type: str) -> float:
        wallet = self.ensure_wallet(user_id)
        if credit_type == WALLET_CREDIT_TYPE:
            return float(wallet.remaining)
        unit = feature_cost(credit_type, 1.0, self._costs())
        if unit <= 0:
            return float("inf")
        return float(wallet.remaining) / unit

    def has_credits(self, user_id: str, credit_type: str, amount: float = 1.0) -> bool:
        needed = feature_cost(credit_type, amount, self._costs())
        if needed <= 0:
            return True
        wallet = self.ensure_wallet(user_id)
        return float(wallet.remaining) >= needed

    def assert_can_spend(self, user_id: str, credit_type: str, amount: float = 1.0) -> None:
        """Raise a detailed 402 without deducting, so a failed call is not billed."""
        user_id = self._ensure_internal_user_id(user_id)
        costs = self._costs()
        needed = feature_cost(credit_type, amount, costs)
        if needed <= 0:
            return
        wallet = self.ensure_wallet(user_id)
        if float(wallet.remaining) < needed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=self.shortage_detail(
                    credit_type,
                    remaining=float(wallet.remaining),
                    required=needed,
                    amount=amount,
                    unit_cost=feature_cost(credit_type, 1.0, costs),
                ),
            )

    def shortage_detail(
        self,
        credit_type: str,
        *,
        remaining: float,
        required: float,
        amount: float = 1.0,
        unit_cost: Optional[float] = None,
        includes_extras: bool = False,
    ) -> Dict[str, Any]:
        verb, noun, unit_name = CREDIT_ACTIVITIES.get(
            credit_type,
            (
                f"use {CREDIT_TYPE_LABELS.get(credit_type, credit_type)}",
                CREDIT_TYPE_LABELS.get(credit_type, credit_type),
                "use",
            ),
        )
        unit = feature_cost(credit_type, 1.0, self._costs()) if unit_cost is None else float(unit_cost)
        shortfall = max(0.0, float(required) - float(remaining))
        if includes_extras:
            body = (
                f"This generation and its references need {format_credits(required)} credits."
            )
        else:
            body = (
                f"{noun} costs {format_credits(unit)} credits per {unit_name}. "
                f"This action needs {format_credits(required)} credits."
            )
        message = (
            f"You can't {verb} because you don't have enough credits. "
            f"{body} You have {format_credits(remaining)} credits left, "
            f"so you need {format_credits(shortfall)} more. Buy more credits to continue."
        )
        return {
            "code": "insufficient_credits",
            "message": message,
            "activity": noun,
            "credit_type": credit_type,
            "required": float(required),
            "remaining": float(remaining),
            "shortfall": shortfall,
            "unit_cost": unit,
            "amount": float(amount),
        }

    def check_and_deduct(
        self,
        user_id: str,
        credit_type: str,
        amount: float = 1.0,
        operation: str = "usage",
        metadata: Optional[Dict[str, Any]] = None,
        raise_on_insufficient: bool = True,
    ) -> bool:
        user_id = self._ensure_internal_user_id(user_id)
        costs = self._costs()
        needed = feature_cost(credit_type, amount, costs)
        wallet = self.ensure_wallet(user_id)

        if needed > 0 and float(wallet.remaining) < needed:
            if raise_on_insufficient:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=self.shortage_detail(
                        credit_type,
                        remaining=float(wallet.remaining),
                        required=needed,
                        amount=amount,
                        unit_cost=feature_cost(credit_type, 1.0, costs),
                    ),
                )
            return False

        if needed > 0:
            wallet.remaining = max(0.0, float(wallet.remaining) - needed)
            wallet.updated_at = datetime.now(timezone.utc)

        log_meta = dict(metadata or {})
        log_meta["feature"] = credit_type
        log_meta["wallet_cost"] = needed
        self.db.add(
            CreditUsageLog(
                user_id=user_id,
                credit_type=credit_type,
                amount=needed,
                operation=operation,
                metadata_json=json.dumps(log_meta),
            )
        )
        self.db.commit()
        return True

    def require_credits(
        self,
        user_id: str,
        credit_type: str,
        amount: float = 1.0,
        operation: str = "usage",
    ) -> None:
        self.check_and_deduct(
            user_id=user_id,
            credit_type=credit_type,
            amount=amount,
            operation=operation,
            raise_on_insufficient=True,
        )

    def require_charges(
        self,
        user_id: str,
        charges: Sequence[tuple[str, float, str]],
        *,
        primary_type: Optional[str] = None,
    ) -> None:
        """Check the combined cost, then deduct each charge in one commit."""
        user_id = self._ensure_internal_user_id(user_id)
        costs = self._costs()
        wallet = self.ensure_wallet(user_id)
        normalized: list[tuple[str, float, str, float]] = []
        needed = 0.0
        for credit_type, amount, operation in charges:
            cost = feature_cost(credit_type, amount, costs)
            needed += cost
            normalized.append((credit_type, float(amount), operation, cost))

        if needed > 0 and float(wallet.remaining) < needed:
            primary = primary_type or (charges[0][0] if charges else WALLET_CREDIT_TYPE)
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=self.shortage_detail(
                    primary,
                    remaining=float(wallet.remaining),
                    required=needed,
                    amount=sum(amount for _, amount, _ in charges) or 1.0,
                    unit_cost=feature_cost(primary, 1.0, costs),
                    includes_extras=len(charges) > 1,
                ),
            )

        for credit_type, amount, operation, cost in normalized:
            if cost > 0:
                wallet.remaining = max(0.0, float(wallet.remaining) - cost)
                wallet.updated_at = datetime.now(timezone.utc)
            log_meta = {"feature": credit_type, "wallet_cost": cost, "units": amount}
            self.db.add(
                CreditUsageLog(
                    user_id=user_id,
                    credit_type=credit_type,
                    amount=cost,
                    operation=operation,
                    metadata_json=json.dumps(log_meta),
                )
            )
        self.db.commit()

    def consume_or_pause(
        self,
        user_id: Optional[str],
        credit_type: str,
        amount: float = 1.0,
        operation: str = "inbound_llm",
    ) -> Optional[str]:
        """Deduct for a customer-channel reply. Return a pause line when the wallet is short."""
        if not user_id:
            return None
        resolved = self.resolve_user_id(user_id)
        if not resolved:
            return None
        if not self.has_credits(resolved, credit_type, amount):
            logger.info("Paused %s for %s: not enough credits", operation, resolved)
            return CUSTOMER_CHANNEL_PAUSED
        self.require_credits(resolved, credit_type, amount, operation)
        return None

    def _costs(self) -> Dict[str, float]:
        from core.credits.service.credit_pricing_service import CreditPricingService

        return CreditPricingService(self.db).cost_map()

    def _free_credits(self) -> float:
        from core.credits.service.credit_pricing_service import CreditPricingService

        return float(CreditPricingService(self.db).grants()["free_credits"])

    def _grant_amounts(self) -> Dict[str, float]:
        from core.credits.service.credit_pricing_service import CreditPricingService

        return CreditPricingService(self.db).grants()

    def ensure_referral_code(self, user: User) -> str:
        existing = (getattr(user, "referral_code", None) or "").strip()
        if existing:
            return existing
        candidate = _preview_referral_code(user)
        taken = (
            self.db.query(User)
            .filter(User.referral_code == candidate, User.id != user.id)
            .first()
        )
        if taken:
            candidate = secrets.token_hex(4).upper()
        user.referral_code = candidate
        self.db.commit()
        self.db.refresh(user)
        return candidate

    def apply_referral(self, user_id: str, code: Optional[str]) -> None:
        raw = (code or "").strip().upper()
        if not raw:
            return
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user or getattr(user, "referred_by_user_id", None):
            return
        referrer = (
            self.db.query(User)
            .filter(func.upper(User.referral_code) == raw, User.id != user.id)
            .first()
        )
        if not referrer:
            logger.info("Signup referral code %s did not match a user", raw)
            return
        user.referred_by_user_id = referrer.id
        self.db.commit()
        grants = self._grant_amounts()
        welcome = float(grants.get("referral_referee_credits") or 0)
        reward = float(grants.get("referral_referrer_credits") or 0)
        if welcome > 0:
            self.add_credits(
                user.id,
                welcome,
                operation="referral_welcome",
                metadata={"referrer_id": referrer.id},
            )
        if reward > 0:
            self.add_credits(
                referrer.id,
                reward,
                operation="referral_reward",
                metadata={"referee_id": user.id},
            )

    def consumption(self, user_id: str, days: int = 30) -> Dict[str, Any]:
        """What this user spent, grouped by API activity, plus the latest charges."""
        user_id = self._ensure_internal_user_id(user_id)
        window = max(1, min(int(days), 365))
        since = datetime.now(timezone.utc) - timedelta(days=window)
        spent_filter = (
            CreditUsageLog.user_id == user_id,
            CreditUsageLog.created_at >= since,
            CreditUsageLog.amount > 0,
            CreditUsageLog.operation != "refund",
        )
        grouped = (
            self.db.query(
                CreditUsageLog.credit_type,
                func.count(CreditUsageLog.id),
                func.coalesce(func.sum(CreditUsageLog.amount), 0.0),
            )
            .filter(*spent_filter)
            .group_by(CreditUsageLog.credit_type)
            .all()
        )
        labels = self._group_labels()
        total = sum(float(amount or 0) for _, _, amount in grouped)
        groups = []
        for credit_type, calls, amount in grouped:
            spent = round(float(amount or 0), 4)
            if spent <= 0:
                continue
            groups.append(
                {
                    "credit_type": credit_type,
                    "label": labels.get(credit_type, credit_type),
                    "calls": int(calls or 0),
                    "credits_used": spent,
                    "share": round(spent / total, 4) if total > 0 else 0.0,
                }
            )
        groups.sort(key=lambda item: item["credits_used"], reverse=True)

        recent_rows = (
            self.db.query(CreditUsageLog)
            .filter(*spent_filter)
            .order_by(CreditUsageLog.created_at.desc())
            .limit(20)
            .all()
        )
        recent = [
            {
                "id": row.id,
                "credit_type": row.credit_type,
                "label": labels.get(row.credit_type, row.credit_type),
                "activity": _ACTIVITY_LABELS.get(row.operation)
                or labels.get(row.credit_type, row.operation.replace("_", " ")),
                "credits": round(float(row.amount or 0), 4),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in recent_rows
        ]
        return {
            "days": window,
            "credits_used": round(total, 4),
            "groups": groups,
            "recent": recent,
        }

    def _group_labels(self) -> Dict[str, str]:
        labels = dict(CREDIT_TYPE_LABELS)
        labels.update(
            {
                key: str(value[1])
                for key, value in CREDIT_ACTIVITIES.items()
                if len(value) > 1
            }
        )
        try:
            from core.credits.service.credit_pricing_service import CreditPricingService

            for group in CreditPricingService(self.db).list_groups():
                name = (group.get("label") or "").strip()
                if name:
                    labels[group["group_key"]] = name
        except Exception:
            logger.debug("Credit group labels fell back to defaults", exc_info=True)
        return labels

    def referral_profile(self, user: User) -> Dict[str, Any]:
        code = self.ensure_referral_code(user)
        invited = (
            self.db.query(User).filter(User.referred_by_user_id == user.id).count()
        )
        joined = (
            self.db.query(User)
            .filter(User.referred_by_user_id == user.id, User.enabled.is_(True))
            .count()
        )
        rewarded = (
            self.db.query(func.coalesce(func.sum(CreditUsageLog.amount), 0.0))
            .filter(
                CreditUsageLog.user_id == user.id,
                CreditUsageLog.operation == "referral_reward",
            )
            .scalar()
        )
        earned = max(0.0, -float(rewarded or 0))
        grants = self._grant_amounts()
        return {
            "referral_code": code,
            "referrals_invited": invited,
            "referrals_joined": joined,
            "referral_rewards": earned,
            "referral_reward_credits": float(grants.get("referral_referrer_credits") or 0),
            "referral_welcome_credits": float(grants.get("referral_referee_credits") or 0),
        }

    # Legacy no-ops so older callers / startup hooks stay safe.
    def get_plan_allocations(self, plan) -> Dict[str, float]:
        return {}

    def sync_plan_credit_allocations(self) -> int:
        return 0

    def initialize_credits_for_subscription(self, user_id: str, subscription=None) -> None:
        self.grant_starter_credits(user_id)


def _preview_referral_code(user: User) -> str:
    """Match the app's preview code so codes shared before this column existed still resolve."""
    name = re.sub(r"[^A-Z0-9]", "", (user.fullname or "AUTOBUS").upper())
    prefix = (name or "AUTOBUS").ljust(4, "X")[:4]
    digits = re.sub(r"[^0-9]", "", user.id or "")
    if not digits:
        digits = str(abs(hash(name)) % 10000)
    suffix = digits.rjust(4, "0")[-4:]
    return f"{prefix}{suffix}"
