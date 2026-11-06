from __future__ import annotations

import os
import secrets
import string
import uuid
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.admin.dto.admin_dto import (
    AdminInviteRequest,
    AdminMeResponse,
    AdminRoleLabel,
    AdminUserResponse,
    AdResponse,
    AdUpsertRequest,
    CustomerResponse,
    CustomerUpdateRequest,
    DashboardCurrency,
    DashboardKpi,
    DashboardResponse,
    DashboardSenderQueueItem,
    MerchantResponse,
    OnboardingAnswer,
    PlanAdminResponse,
    PlanUpsertRequest,
    SenderIdAdminResponse,
    TransactionAdminResponse,
)
from core.admin.model.platform_ad import PlatformAd
from core.auth.dependencies import _admin_email_set, _admin_id_set, platform_role_of
from core.auth.service.authservice import AuthService
from core.customers.model.customer import Customer
from core.orders.model.order import Order
from core.otp.service.otpservice import OTPService
from core.paystack.model.transaction import Transaction
from core.sms_sender_id.model.SmsSenderIdRegistration import (
    SmsSenderIdRegistration,
    SmsSenderIdStatus,
)
from core.subscription.model.subscription_plan import BillingPeriod, SubscriptionPlan
from core.subscription.model.user_subscription import UserSubscription
from core.subscription.service.subscription_service import SubscriptionService
from core.user.model.User import User, UserStatus

ROLE_TO_LABEL = {
    "super_admin": "Super Admin",
    "admin": "Admin",
    "support": "Support",
}
LABEL_TO_ROLE = {v: k for k, v in ROLE_TO_LABEL.items()}


def _fmt_date(value: Optional[datetime | date]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        d = value
    else:
        d = datetime(value.year, value.month, value.day)
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def _iso_date(value: Optional[datetime | date]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _pretty_status(raw: Optional[str]) -> str:
    value = (raw or "pending").strip().lower()
    mapping = {
        "success": "successful",
        "successful": "successful",
        "paid": "successful",
        "failed": "failed",
        "abandoned": "failed",
        "pending": "pending",
        "refunded": "refunded",
        "reversed": "refunded",
    }
    return mapping.get(value, value)


class AdminService:
    def __init__(self, db: Session):
        self.db = db
        self.auth = AuthService(db)

    def _merchant_filter(self):
        return or_(User.platform_role.is_(None), User.platform_role == "")

    def _not_deleted(self):
        return or_(User.status.is_(None), func.upper(User.status) != UserStatus.DELETED.value)

    def signin(self, email: str, password: str) -> Dict[str, Any]:
        user = self.auth.authenticate_user(email, password)
        role = platform_role_of(user)
        if not role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account is not authorized for the admin portal",
            )
        tokens = self.auth.issue_session_tokens(user, user.id)
        tokens["user"] = AdminMeResponse(
            id=user.id,
            fullName=user.fullname,
            email=user.email,
            role=ROLE_TO_LABEL[role],
            status="active" if user.enabled else "invited",
        ).model_dump()
        return tokens

    def me(self, user: User) -> AdminMeResponse:
        role = platform_role_of(user) or "support"
        return AdminMeResponse(
            id=user.id,
            fullName=user.fullname,
            email=user.email,
            role=ROLE_TO_LABEL.get(role, "Support"),
            status="active" if user.enabled else "invited",
        )

    def dashboard(self, period: str, custom_from: Optional[str], custom_to: Optional[str]) -> DashboardResponse:
        merchants = (
            self.db.query(User)
            .filter(self._merchant_filter(), self._not_deleted())
            .all()
        )
        start, end = self._period_bounds(period, custom_from, custom_to)
        signups = [m for m in merchants if m.created_at and start <= self._aware(m.created_at) < end]
        pending_sender = (
            self.db.query(SmsSenderIdRegistration)
            .filter(
                SmsSenderIdRegistration.is_active.is_(True),
                SmsSenderIdRegistration.status == SmsSenderIdStatus.PENDING,
            )
            .count()
        )
        active_plans = (
            self.db.query(SubscriptionPlan).filter(SubscriptionPlan.is_active.is_(True)).count()
        )
        enabled = sum(1 for m in merchants if m.enabled)
        disabled = max(len(merchants) - enabled, 0)
        onboarded = sum(1 for m in merchants if m.onboarding_completed)
        onboarding_pct = round((onboarded / len(merchants)) * 100) if merchants else 0

        week_end = datetime.now(timezone.utc).date()
        week_start = week_end - timedelta(days=6)
        week_counts = [0] * 7
        for m in merchants:
            if not m.created_at:
                continue
            d = m.created_at.date()
            if week_start <= d <= week_end:
                week_counts[(d - week_start).days] += 1
        labels = [(week_start + timedelta(days=i)).strftime("%a")[0] for i in range(7)]

        currency_counts: Dict[str, int] = defaultdict(int)
        for m in merchants:
            code = (m.currency_code or "GHS").upper()
            if code not in {"GHS", "NGN", "USD"}:
                code = "Other"
            currency_counts[code] += 1
        total_m = len(merchants) or 1
        breakdown = []
        for code in ("GHS", "NGN", "USD", "Other"):
            pct = round((currency_counts.get(code, 0) / total_m) * 100)
            if pct or code == "GHS":
                breakdown.append(DashboardCurrency(code=code, percent=pct))

        queue_rows = (
            self.db.query(SmsSenderIdRegistration)
            .filter(
                SmsSenderIdRegistration.is_active.is_(True),
                SmsSenderIdRegistration.status == SmsSenderIdStatus.PENDING,
            )
            .order_by(SmsSenderIdRegistration.created_at.desc())
            .limit(8)
            .all()
        )
        queue = [
            DashboardSenderQueueItem(
                id=r.sender_id,
                company=r.company_name or "",
                created=_fmt_date(r.created_at),
            )
            for r in queue_rows
        ]

        kpis = [
            DashboardKpi(
                label="Total Merchants",
                value=str(len(merchants)),
                icon="ri-store-2-line",
                change=f"+{len(signups)} this period",
                bg="#7F03B9",
            ),
            DashboardKpi(
                label="Pending Sender IDs",
                value=str(pending_sender),
                icon="ri-message-3-line",
                change="Needs review" if pending_sender else "All clear",
                bg="#F59E0B",
            ),
            DashboardKpi(
                label="Active Plans",
                value=str(active_plans),
                icon="ri-price-tag-3-line",
                change="Currently offered",
                bg="#10B981",
            ),
            DashboardKpi(
                label="New Signups",
                value=str(len(signups)),
                icon="ri-user-add-line",
                change=period,
                bg="#1B0227",
            ),
        ]
        return DashboardResponse(
            kpis=kpis,
            weekLabels=labels,
            weekSignups=week_counts,
            enabledCount=enabled,
            disabledCount=disabled,
            onboardingCompletion=onboarding_pct,
            currencyBreakdown=breakdown,
            senderIdQueue=queue,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _period_bounds(
        self, period: str, custom_from: Optional[str], custom_to: Optional[str]
    ) -> Tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        key = (period or "Last 30 days").strip().lower()
        if key == "today":
            return today, now + timedelta(days=1)
        if key == "last 7 days":
            return today - timedelta(days=6), now + timedelta(days=1)
        if key == "this month":
            start = today.replace(day=1)
            last = monthrange(today.year, today.month)[1]
            return start, today.replace(day=last) + timedelta(days=1)
        if key == "custom" and custom_from and custom_to:
            start = datetime.fromisoformat(custom_from).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(custom_to).replace(tzinfo=timezone.utc) + timedelta(days=1)
            return start, end
        return today - timedelta(days=29), now + timedelta(days=1)

    def list_merchants(self, q: Optional[str] = None, limit: Optional[int] = None) -> List[MerchantResponse]:
        query = self.db.query(User).filter(self._merchant_filter(), self._not_deleted())
        term = (q or "").strip()
        if term:
            like = f"%{term}%"
            query = query.filter(
                or_(
                    User.company.ilike(like),
                    User.fullname.ilike(like),
                    User.email.ilike(like),
                )
            )
        query = query.order_by(User.created_at.desc())
        if limit:
            query = query.limit(limit)
        return [self._merchant_to_dto(u) for u in query.all()]

    def get_merchant(self, merchant_id: str) -> MerchantResponse:
        user = self._get_merchant_or_404(merchant_id)
        return self._merchant_to_dto(user, detail=True)

    def toggle_merchant(self, merchant_id: str) -> MerchantResponse:
        user = self._get_merchant_or_404(merchant_id)
        user.enabled = not bool(user.enabled)
        self.db.commit()
        self.db.refresh(user)
        return self._merchant_to_dto(user, detail=True)

    def delete_merchant(self, merchant_id: str) -> None:
        user = self._get_merchant_or_404(merchant_id)
        user.status = UserStatus.DELETED.value
        user.enabled = False
        self.db.commit()

    def _get_merchant_or_404(self, merchant_id: str) -> User:
        user = self.db.query(User).filter(User.id == merchant_id, self._merchant_filter()).first()
        if not user or str(user.status or "").upper() == UserStatus.DELETED.value:
            raise HTTPException(status_code=404, detail="Merchant not found")
        return user

    def _merchant_to_dto(self, user: User, detail: bool = False) -> MerchantResponse:
        if user.enabled:
            status_label = "active"
        elif user.onboarding_completed:
            status_label = "suspended"
        else:
            status_label = "unverified"
        answers = None
        if detail and isinstance(user.onboarding_profile, dict):
            answers = [
                OnboardingAnswer(question=str(k), answer=str(v) if v is not None else "")
                for k, v in user.onboarding_profile.items()
            ]
        return MerchantResponse(
            id=user.id,
            company=user.company or user.fullname,
            fullName=user.fullname,
            email=user.email,
            phone=user.phone or "",
            currency=(user.currency_code or "GHS").upper(),
            status=status_label,
            enabled=bool(user.enabled),
            onboardingCompleted=bool(user.onboarding_completed),
            created=_fmt_date(user.created_at),
            location=user.location,
            ghanaCard=user.ghana_card,
            onboardingAnswers=answers,
        )

    def _order_stats(self) -> Dict[str, Tuple[int, float]]:
        rows = (
            self.db.query(
                Order.customer_id,
                func.count(Order.order_id),
                func.coalesce(func.sum(Order.total_amount), 0),
            )
            .filter(Order.customer_id.isnot(None))
            .group_by(Order.customer_id)
            .all()
        )
        stats: Dict[str, Tuple[int, float]] = {}
        for cid, count, total in rows:
            if cid:
                stats[str(cid)] = (int(count or 0), float(total or 0))
        return stats

    def _customer_to_dto(self, customer: Customer, merchant: Optional[User], stats: Dict[str, Tuple[int, float]]) -> CustomerResponse:
        count, spent = stats.get(str(customer.id), (0, 0.0))
        return CustomerResponse(
            id=str(customer.id),
            fullName=customer.name,
            email=customer.email or "",
            phone=customer.customer_number or "",
            merchantId=customer.user_id,
            merchantName=(merchant.company or merchant.fullname) if merchant else "",
            totalOrders=count,
            totalSpent=round(spent, 2),
            currency=(merchant.currency_code if merchant else None) or "GHS",
            status="active" if customer.is_active else "disabled",
            joined=_fmt_date(customer.created_at),
        )

    def list_customers(self, merchant_id: Optional[str] = None) -> List[CustomerResponse]:
        query = self.db.query(Customer)
        if merchant_id:
            query = query.filter(Customer.user_id == merchant_id)
        customers = query.order_by(Customer.created_at.desc()).all()
        merchant_ids = {c.user_id for c in customers}
        merchants = {
            u.id: u
            for u in self.db.query(User).filter(User.id.in_(merchant_ids)).all()
        } if merchant_ids else {}
        stats = self._order_stats()
        return [self._customer_to_dto(c, merchants.get(c.user_id), stats) for c in customers]

    def get_customer(self, customer_id: str) -> CustomerResponse:
        customer = self._get_customer_or_404(customer_id)
        merchant = self.db.query(User).filter(User.id == customer.user_id).first()
        return self._customer_to_dto(customer, merchant, self._order_stats())

    def update_customer(self, customer_id: str, payload: CustomerUpdateRequest) -> CustomerResponse:
        customer = self._get_customer_or_404(customer_id)
        if payload.fullName is not None:
            customer.name = payload.fullName.strip()
        if payload.email is not None:
            customer.email = payload.email.strip() or None
        if payload.phone is not None:
            customer.customer_number = payload.phone.strip()
        customer.updated_at = datetime.now()
        self.db.commit()
        return self.get_customer(customer_id)

    def toggle_customer(self, customer_id: str) -> CustomerResponse:
        customer = self._get_customer_or_404(customer_id)
        customer.is_active = not bool(customer.is_active)
        customer.updated_at = datetime.now()
        self.db.commit()
        return self.get_customer(customer_id)

    def delete_customer(self, customer_id: str) -> None:
        customer = self._get_customer_or_404(customer_id)
        self.db.delete(customer)
        self.db.commit()

    def _get_customer_or_404(self, customer_id: str) -> Customer:
        try:
            cid = int(customer_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer = self.db.query(Customer).filter(Customer.id == cid).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        return customer

    def _apple_ids(self, plan: SubscriptionPlan) -> List[str]:
        ids = []
        if plan.apple_product_id_monthly:
            ids.append(plan.apple_product_id_monthly)
        if plan.apple_product_id_yearly:
            ids.append(plan.apple_product_id_yearly)
        return ids

    def _apply_apple_ids(self, plan: SubscriptionPlan, ids: List[str]) -> None:
        plan.apple_product_id_monthly = ids[0] if len(ids) > 0 else None
        plan.apple_product_id_yearly = ids[1] if len(ids) > 1 else None

    def _plan_to_dto(self, plan: SubscriptionPlan) -> PlanAdminResponse:
        period = plan.billing_period
        period_val = period.value if isinstance(period, BillingPeriod) else str(period or "monthly")
        if period_val not in ("monthly", "annually"):
            period_val = "monthly"
        return PlanAdminResponse(
            id=str(plan.id),
            name=plan.name,
            price=float(plan.price or 0),
            billingPeriod=period_val,  # type: ignore[arg-type]
            billingPeriodCount=int(plan.billing_period_count or 1),
            features=plan.get_features_list(),
            agents=plan.get_agents_list(),
            description=plan.description or "",
            active=bool(plan.is_active),
            appleProductIds=self._apple_ids(plan),
        )

    def list_plans(self) -> List[PlanAdminResponse]:
        plans = self.db.query(SubscriptionPlan).order_by(SubscriptionPlan.id.asc()).all()
        return [self._plan_to_dto(p) for p in plans]

    def create_plan(self, payload: PlanUpsertRequest) -> PlanAdminResponse:
        import json

        result = SubscriptionService(self.db).create_subscription_plan(
            name=payload.name.strip(),
            price=payload.price,
            billing_period=payload.billingPeriod,
            billing_period_count=payload.billingPeriodCount,
            features=json.dumps(payload.features),
            agents=json.dumps(payload.agents),
            description=payload.description or None,
            is_active=payload.active,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("message") or "Failed to create plan")
        plan = result["plan"]
        self._apply_apple_ids(plan, payload.appleProductIds)
        self.db.commit()
        self.db.refresh(plan)
        return self._plan_to_dto(plan)

    def update_plan(self, plan_id: str, payload: PlanUpsertRequest) -> PlanAdminResponse:
        import json

        try:
            pid = int(plan_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Plan not found")
        result = SubscriptionService(self.db).update_subscription_plan(
            pid,
            name=payload.name.strip(),
            price=payload.price,
            billing_period=payload.billingPeriod,
            billing_period_count=payload.billingPeriodCount,
            features=json.dumps(payload.features),
            agents=json.dumps(payload.agents),
            description=payload.description or None,
            is_active=payload.active,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("message") or "Failed to update plan")
        plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.id == pid).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        self._apply_apple_ids(plan, payload.appleProductIds)
        self.db.commit()
        self.db.refresh(plan)
        return self._plan_to_dto(plan)

    def toggle_plan(self, plan_id: str) -> PlanAdminResponse:
        try:
            pid = int(plan_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Plan not found")
        plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.id == pid).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        plan.is_active = not bool(plan.is_active)
        plan.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(plan)
        return self._plan_to_dto(plan)

    def list_sender_ids(self) -> List[SenderIdAdminResponse]:
        rows = (
            self.db.query(SmsSenderIdRegistration)
            .filter(SmsSenderIdRegistration.is_active.is_(True))
            .order_by(SmsSenderIdRegistration.created_at.desc())
            .all()
        )
        return [self._sender_to_dto(r) for r in rows]

    def _sender_to_dto(self, row: SmsSenderIdRegistration) -> SenderIdAdminResponse:
        status_val = row.status.value if isinstance(row.status, SmsSenderIdStatus) else str(row.status)
        return SenderIdAdminResponse(
            id=row.id,
            senderId=row.sender_id,
            company=row.company_name or "",
            notes=row.notes or "",
            status=status_val.lower(),
            created=_fmt_date(row.created_at),
            reviewed=_fmt_date(row.reviewed_at) if row.reviewed_at else None,
            rejectionReason=row.rejection_reason,
        )

    def approve_sender_id(self, registration_id: str, admin: User) -> SenderIdAdminResponse:
        from core.sms_sender_id.controller.sms_sender_id_controller import (
            _ensure_zernio_column,
            _register_with_zernio,
        )

        row = self._get_sender_or_404(registration_id)
        _ensure_zernio_column(self.db)
        if not (row.zernio_sender_id or "").strip():
            remote_id, active = _register_with_zernio(row.sender_id)
            row.zernio_sender_id = remote_id
            if not active:
                raise HTTPException(
                    status_code=409,
                    detail="Zernio has not activated this sender ID yet.",
                )
        row.status = SmsSenderIdStatus.APPROVED
        row.rejection_reason = None
        row.reviewed_by = admin.id
        row.reviewed_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return self._sender_to_dto(row)

    def reject_sender_id(self, registration_id: str, reason: str, admin: User) -> SenderIdAdminResponse:
        from core.sms_sender_id.controller.sms_sender_id_controller import _delete_from_zernio

        row = self._get_sender_or_404(registration_id)
        _delete_from_zernio(row.zernio_sender_id)
        row.zernio_sender_id = None
        row.status = SmsSenderIdStatus.REJECTED
        row.rejection_reason = reason.strip()
        row.reviewed_by = admin.id
        row.reviewed_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)
        return self._sender_to_dto(row)

    def _get_sender_or_404(self, registration_id: str) -> SmsSenderIdRegistration:
        row = (
            self.db.query(SmsSenderIdRegistration)
            .filter(
                SmsSenderIdRegistration.id == registration_id,
                SmsSenderIdRegistration.is_active.is_(True),
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="Sender ID registration not found")
        return row

    def list_transactions(self) -> List[TransactionAdminResponse]:
        rows = self.db.query(Transaction).order_by(Transaction.created_at.desc()).all()
        user_ids = {r.user_id for r in rows}
        users = {
            u.id: u for u in self.db.query(User).filter(User.id.in_(user_ids)).all()
        } if user_ids else {}
        plan_by_ref: Dict[str, str] = {}
        subs = self.db.query(UserSubscription).all()
        plans = {p.id: p.name for p in self.db.query(SubscriptionPlan).all()}
        for sub in subs:
            if sub.payment_reference:
                plan_by_ref[sub.payment_reference] = plans.get(sub.plan_id, "") or ""
        out = []
        for row in rows:
            meta = row.transaction_metadata if isinstance(row.transaction_metadata, dict) else {}
            user = users.get(row.user_id)
            amount = float(row.amount or 0)
            if amount >= 100:
                amount = amount / 100.0
            method = meta.get("channel") or meta.get("payment_method") or row.gateway_response or "Card"
            plan_name = meta.get("plan_name") or plan_by_ref.get(row.reference or "", "") or ""
            currency = (meta.get("currency") or (user.currency_code if user else None) or "GHS").upper()
            out.append(
                TransactionAdminResponse(
                    id=row.id,
                    reference=row.reference,
                    merchantName=(user.company or user.fullname) if user else row.email,
                    planName=plan_name,
                    amount=round(amount, 2),
                    currency=currency,
                    paymentMethod=str(method).replace("_", " ").title(),
                    status=_pretty_status(row.status),
                    date=_fmt_date(row.paid_at or row.created_at),
                    dateISO=_iso_date(row.paid_at or row.created_at),
                )
            )
        return out

    def set_transaction_status(self, txn_id: str, new_status: str) -> TransactionAdminResponse:
        row = self.db.query(Transaction).filter(Transaction.id == txn_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")
        row.status = new_status
        self.db.commit()
        items = {t.id: t for t in self.list_transactions()}
        return items[txn_id]

    def list_ads(self) -> List[AdResponse]:
        rows = self.db.query(PlatformAd).order_by(PlatformAd.created_at.desc()).all()
        return [self._ad_to_dto(r) for r in rows]

    def list_active_ads(self) -> List[AdResponse]:
        today = date.today()
        rows = (
            self.db.query(PlatformAd)
            .filter(
                PlatformAd.is_active.is_(True),
                PlatformAd.start_date <= today,
                PlatformAd.end_date >= today,
            )
            .order_by(PlatformAd.created_at.desc())
            .all()
        )
        return [self._ad_to_dto(r) for r in rows]

    def create_ad(self, payload: AdUpsertRequest) -> AdResponse:
        if payload.endDate < payload.startDate:
            raise HTTPException(status_code=400, detail="End date must be after the start date")
        ad = PlatformAd(
            id=f"ad_{uuid.uuid4().hex[:12]}",
            title=payload.title.strip(),
            type=payload.type,
            media_url=payload.mediaUrl.strip(),
            link_url=(payload.linkUrl or "").strip() or None,
            is_active=payload.active,
            start_date=payload.startDate,
            end_date=payload.endDate,
        )
        self.db.add(ad)
        self.db.commit()
        self.db.refresh(ad)
        return self._ad_to_dto(ad)

    def update_ad(self, ad_id: str, payload: AdUpsertRequest) -> AdResponse:
        ad = self._get_ad_or_404(ad_id)
        if payload.endDate < payload.startDate:
            raise HTTPException(status_code=400, detail="End date must be after the start date")
        ad.title = payload.title.strip()
        ad.type = payload.type
        ad.media_url = payload.mediaUrl.strip()
        ad.link_url = (payload.linkUrl or "").strip() or None
        ad.is_active = payload.active
        ad.start_date = payload.startDate
        ad.end_date = payload.endDate
        ad.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(ad)
        return self._ad_to_dto(ad)

    def toggle_ad(self, ad_id: str) -> AdResponse:
        ad = self._get_ad_or_404(ad_id)
        ad.is_active = not bool(ad.is_active)
        ad.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(ad)
        return self._ad_to_dto(ad)

    def delete_ad(self, ad_id: str) -> None:
        ad = self._get_ad_or_404(ad_id)
        self.db.delete(ad)
        self.db.commit()

    def _get_ad_or_404(self, ad_id: str) -> PlatformAd:
        ad = self.db.query(PlatformAd).filter(PlatformAd.id == ad_id).first()
        if not ad:
            raise HTTPException(status_code=404, detail="Ad not found")
        return ad

    def _ad_to_dto(self, ad: PlatformAd) -> AdResponse:
        return AdResponse(
            id=ad.id,
            title=ad.title,
            type=ad.type if ad.type in ("image", "video") else "image",  # type: ignore[arg-type]
            mediaUrl=ad.media_url,
            linkUrl=ad.link_url,
            active=bool(ad.is_active),
            startDate=ad.start_date.isoformat(),
            endDate=ad.end_date.isoformat(),
            createdOn=_fmt_date(ad.created_at),
        )

    def list_admins(self, current: User) -> List[AdminUserResponse]:
        env_ids = _admin_id_set()
        env_emails = _admin_email_set()
        rows = (
            self.db.query(User)
            .filter(
                or_(
                    User.platform_role.in_(list(ROLE_TO_LABEL.keys())),
                    User.id.in_(list(env_ids) or ["__none__"]),
                    func.lower(User.email).in_(list(env_emails) or ["__none__"]),
                )
            )
            .all()
        )
        seen = set()
        out = []
        for user in rows:
            if user.id in seen:
                continue
            seen.add(user.id)
            role = platform_role_of(user) or "admin"
            env_locked = user.id in env_ids or (user.email or "").lower() in env_emails
            out.append(
                AdminUserResponse(
                    id=user.id,
                    fullName=user.fullname,
                    email=user.email,
                    role=ROLE_TO_LABEL.get(role, "Admin"),
                    status="active" if user.enabled else "invited",
                    invitedOn=_fmt_date(user.created_at),
                    lastActive=_fmt_date(user.updated_at) if user.updated_at else None,
                    isCurrentUser=user.id == current.id,
                    envLocked=env_locked,
                )
            )
        return out

    def _send_invite_email(self, user: User) -> None:
        role = platform_role_of(user) or "admin"
        role_label = ROLE_TO_LABEL.get(role, "Admin")
        portal = (os.getenv("ADMIN_PORTAL_URL") or "https://admin.useautobus.com").rstrip("/")
        greeting = (
            f"Hi {user.fullname},\n\n"
            f"You've been invited to the Autobus admin portal as {role_label}.\n\n"
        )
        if user.enabled:
            body = (
                greeting
                + f"Sign in at {portal} with {user.email} and your existing Autobus PIN.\n"
            )
        else:
            default_pin = "".join(secrets.choice(string.digits) for _ in range(4))
            user.hashed_password = self.auth.hash_password(default_pin)
            self.db.commit()
            body = (
                greeting
                + f"Sign in at {portal} with:\n"
                f"Email: {user.email}\n"
                f"Default PIN: {default_pin}\n\n"
                f"You can change this PIN anytime from {portal}/forgot-password.\n"
            )
        result = OTPService(self.db).send_email(
            user.email,
            subject="You're invited to Autobus Admin",
            body=body,
        )
        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result.message or "Could not send the invite email. Check email settings and try Resend invite.",
            )

    def invite_admin(self, payload: AdminInviteRequest, current: User) -> AdminUserResponse:
        role = LABEL_TO_ROLE[payload.role]
        if role == "super_admin" and platform_role_of(current) != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a super admin can invite another super admin",
            )
        email = str(payload.email).strip().lower()
        existing = self.db.query(User).filter(func.lower(User.email) == email).first()
        if existing and platform_role_of(existing):
            raise HTTPException(status_code=400, detail="An admin with this email already exists")
        created_new = False
        if existing:
            existing.platform_role = role
            self.db.commit()
            self.db.refresh(existing)
            user = existing
        else:
            fullname = payload.fullName.strip()
            collision = self.db.query(User).filter(func.lower(User.fullname) == fullname.lower()).first()
            if collision:
                fullname = f"{fullname} {secrets.token_hex(2)}"
            temp_password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
            user = User(
                id=self.auth.generate_user_id(),
                fullname=fullname,
                email=email,
                hashed_password=self.auth.hash_password(temp_password),
                platform_role=role,
                enabled=False,
                onboarding_completed=True,
                company="Autobus Admin",
                created_at=datetime.now(timezone.utc),
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)
            created_new = True
        self._send_invite_email(user)
        return AdminUserResponse(
            id=user.id,
            fullName=user.fullname,
            email=user.email,
            role=payload.role,
            status="invited" if created_new or not user.enabled else "active",
            invitedOn="Today",
            isCurrentUser=False,
            envLocked=False,
        )

    def resend_invite(self, admin_id: str) -> None:
        user = self.db.query(User).filter(User.id == admin_id).first()
        if not user or not platform_role_of(user):
            raise HTTPException(status_code=404, detail="Admin not found")
        self._send_invite_email(user)

    def update_admin_role(self, admin_id: str, role_label: AdminRoleLabel, current: User) -> AdminUserResponse:
        user = self.db.query(User).filter(User.id == admin_id).first()
        if not user or not platform_role_of(user):
            raise HTTPException(status_code=404, detail="Admin not found")
        new_role = LABEL_TO_ROLE[role_label]
        if platform_role_of(user) == "super_admin" and new_role != "super_admin":
            if self._super_admin_count() <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last super admin")
        user.platform_role = new_role
        self.db.commit()
        return [a for a in self.list_admins(current) if a.id == admin_id][0]

    def remove_admin(self, admin_id: str, current: User) -> None:
        if admin_id == current.id:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin access")
        user = self.db.query(User).filter(User.id == admin_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Admin not found")
        env_locked = user.id in _admin_id_set() or (user.email or "").lower() in _admin_email_set()
        if env_locked:
            raise HTTPException(status_code=400, detail="This admin is locked by server configuration")
        target_role = platform_role_of(user)
        current_role = platform_role_of(current)
        if current_role != "super_admin" and target_role == "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a super admin can remove a super admin",
            )
        if target_role == "super_admin" and self._super_admin_count() <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last super admin")
        user.platform_role = None
        self.db.commit()

    def _super_admin_count(self) -> int:
        count = 0
        for user in self.db.query(User).all():
            if platform_role_of(user) == "super_admin":
                count += 1
        return count
