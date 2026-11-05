"""Self-service Autobus account deletion (Apple 5.1.1(v) / Play account deletion)."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, List, Optional, Type

from another_fastapi_jwt_auth import AuthJWT
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.auth.dependencies import resolve_session_manager
from core.auth.dto.response.account_deletion import (
    AccountDeletionPreview,
    AccountDeletionResult,
    DeletionBusiness,
    DeletionConnection,
    DeletionSubscription,
)
from core.auth.service.authservice import AuthService
from core.chatwoot.model.ChatwootAccount import ChatwootAccount
from core.customers.model.customer import Customer
from core.instagram.model.InstagramAccount import InstagramAccount
from core.orders.model.order import Order
from core.product.model.product import Product
from core.socialmedia.model.PostizOrganization import PostizOrganization
from core.socialmedia.model.SocialAccount import SocialAccount
from core.subscription.model.user_subscription import SubscriptionStatus, UserSubscription
from core.subscription.service.subscription_service import SubscriptionService
from core.user.model.User import User, UserStatus
from core.whatsapp.model.WhatsAppAccount import WhatsAppAccount

logger = logging.getLogger(__name__)

META_NOTE = (
    "WhatsApp and Instagram accounts stay on Meta. Autobus only removes the connection."
)
SUBSCRIPTION_NOTE = (
    "Deleting Autobus does not cancel an App Store or Google Play subscription. "
    "Cancel it in your device subscription settings if you have one."
)
BILLING_NOTE = (
    "Billing and tax records may be kept as required by law. They are not used to restore your account."
)


def is_deleted_user(user: User) -> bool:
    raw = getattr(user, "status", None)
    if raw == UserStatus.DELETED:
        return True
    return str(raw or "").upper() == UserStatus.DELETED.value


def tombstone_user(user: User, *, now: Optional[datetime] = None) -> None:
    """Wipe PII and free unique email/username so the person can register again."""
    stamp = now or datetime.now(timezone.utc)
    tombstone_id = user.id
    user.email = f"deleted.{tombstone_id}@deleted.invalid"
    user.fullname = f"deleted.{tombstone_id}"
    user.phone = None
    user.hashed_password = AuthService.hash_password_static(secrets.token_urlsafe(32))
    user.ghana_card = None
    user.profile_picture_url = None
    user.nationality = None
    user.date_of_birth = None
    user.gender = None
    user.address = None
    user.location = None
    user.company = None
    user.current_branch = None
    user.staff_id = None
    user.occupation = None
    user.organization_workplace = None
    user.skills = None
    user.experiences = None
    user.facebook_url = None
    user.whatsapp_number = None
    user.linkedin_url = None
    user.twitter_url = None
    user.instagram_url = None
    user.agents = {}
    user.onboarding_profile = None
    user.managed_by_user_id = None
    user.enabled = False
    user.status = UserStatus.DELETED
    user.updated_at = stamp


def deletion_notes(*, has_paid_subscription: bool) -> List[str]:
    notes = [META_NOTE, BILLING_NOTE]
    if has_paid_subscription:
        notes.insert(1, SUBSCRIPTION_NOTE)
    return notes


class AccountDeletionService:
    def __init__(self, db: Session):
        self.db = db
        self.auth_service = AuthService(db)

    def _login_users(self, current_user: User, authjwt: AuthJWT) -> List[User]:
        manager = resolve_session_manager(authjwt, self.db, current_user)
        children = (
            self.db.query(User)
            .filter(User.managed_by_user_id == manager.id)
            .order_by(User.created_at.asc())
            .all()
        )
        return [manager, *children]

    def _verify_login_password(self, manager: User, password: str) -> None:
        if not password or not self.auth_service.verify_password(
            password, manager.hashed_password
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Incorrect PIN. Try again.",
            )
        if is_deleted_user(manager):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="This account has already been deleted.",
            )

    def preview(self, current_user: User, authjwt: AuthJWT) -> AccountDeletionPreview:
        users = self._login_users(current_user, authjwt)
        manager = users[0]
        connections: List[DeletionConnection] = []
        customer_count = 0
        product_count = 0
        order_count = 0
        paid_sub: Optional[UserSubscription] = None

        for user in users:
            connections.extend(self._local_connections(user))
            customer_count += (
                self.db.query(Customer).filter(Customer.user_id == user.id).count()
            )
            product_count += (
                self.db.query(Product).filter(Product.user_id == user.id).count()
            )
            order_count += self.db.query(Order).filter(Order.user_id == user.id).count()
            sub = SubscriptionService(self.db).get_user_active_subscription(user.id)
            if sub:
                provider = (sub.payment_provider or "").strip().lower()
                if provider in ("paystack", "apple_iap"):
                    paid_sub = sub

        plan_name = None
        provider = None
        if paid_sub:
            provider = paid_sub.payment_provider
            plan = getattr(paid_sub, "plan", None)
            plan_name = getattr(plan, "name", None)

        return AccountDeletionPreview(
            login_email=manager.email,
            businesses=[
                DeletionBusiness(
                    id=user.id,
                    email=user.email,
                    fullname=user.fullname,
                    company=user.company,
                    is_manager=user.id == manager.id,
                    is_active=user.id == current_user.id,
                )
                for user in users
            ],
            connections=connections,
            subscription=DeletionSubscription(
                active=paid_sub is not None,
                provider=provider,
                plan_name=plan_name,
            ),
            data_summary={
                "customers": customer_count,
                "products": product_count,
                "orders": order_count,
            },
            notes=deletion_notes(has_paid_subscription=paid_sub is not None),
        )

    def _local_connections(self, user: User) -> List[DeletionConnection]:
        items: List[DeletionConnection] = []
        for row in (
            self.db.query(WhatsAppAccount)
            .filter(WhatsAppAccount.user_id == user.id, WhatsAppAccount.is_active.is_(True))
            .all()
        ):
            items.append(
                DeletionConnection(
                    kind="whatsapp",
                    label="WhatsApp",
                    detail=row.display_phone_number or row.verified_name or row.phone_number_id,
                )
            )
        for row in (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.user_id == user.id, InstagramAccount.is_active.is_(True))
            .all()
        ):
            items.append(
                DeletionConnection(
                    kind="instagram",
                    label="Instagram",
                    detail=row.username or row.name or row.ig_user_id,
                )
            )
        for row in (
            self.db.query(SocialAccount).filter(SocialAccount.user_id == user.id).all()
        ):
            items.append(
                DeletionConnection(
                    kind="social",
                    label=str(row.platform or "Social").replace("_", " ").title(),
                    detail=row.account_name,
                )
            )
        return items

    async def delete_account(
        self,
        current_user: User,
        authjwt: AuthJWT,
        password: str,
    ) -> AccountDeletionResult:
        users = self._login_users(current_user, authjwt)
        manager = users[0]
        self._verify_login_password(manager, password)

        emails = [u.email for u in users if u.email]
        deleted_ids = [u.id for u in users]

        for user in users:
            await self._best_effort_remote_unlink(user)

        for user in reversed(users):
            self._wipe_workspace(user)

        try:
            self.db.commit()
        except Exception:
            logger.exception("Account deletion commit failed; retrying tombstone-only")
            self.db.rollback()
            for user_id in deleted_ids:
                leftover = self.db.query(User).filter(User.id == user_id).first()
                if leftover and not is_deleted_user(leftover):
                    tombstone_user(leftover)
            self.db.commit()

        for email in emails:
            try:
                self.auth_service.session_driver.remove_tokens(email)
            except Exception as exc:
                logger.warning("Could not revoke sessions for %s: %s", email, exc)

        return AccountDeletionResult(deleted_user_ids=deleted_ids)

    async def _best_effort_remote_unlink(self, user: User) -> None:
        await self._unlink_instagram(user.id)

    async def _unlink_instagram(self, user_id: str) -> None:
        rows = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.user_id == user_id)
            .all()
        )
        if not rows:
            return
        try:
            from core.instagram.service.instagram_oauth_service import InstagramOAuthService

            svc = InstagramOAuthService()
            for row in rows:
                try:
                    token = svc.decrypt_token(row.access_token_encrypted)
                    svc.unsubscribe_webhooks(token, row.ig_user_id)
                except Exception as exc:
                    logger.warning("[IG] unsubscribe on account delete failed: %s", exc)
        except Exception as exc:
            logger.warning("[IG] oauth service unavailable during delete: %s", exc)

    def _wipe_workspace(self, user: User) -> None:
        user_id = user.id
        self._delete_rows(WhatsAppAccount, WhatsAppAccount.user_id, user_id)
        self._delete_rows(InstagramAccount, InstagramAccount.user_id, user_id)
        self._delete_rows(SocialAccount, SocialAccount.user_id, user_id)
        self._delete_rows(ChatwootAccount, ChatwootAccount.user_id, user_id)
        self._delete_rows(PostizOrganization, PostizOrganization.user_id, user_id)
        self._delete_optional_models(user_id)
        self._delete_rows(Customer, Customer.user_id, user_id)
        self._delete_rows(Order, Order.user_id, user_id)
        self._delete_products(user_id)
        self._cancel_subscriptions(user_id)
        tombstone_user(user)

    def _delete_optional_models(self, user_id: str) -> None:
        optional: List[tuple] = []
        try:
            from core.socialmedia.model.DigitalMarketingPostAsset import (
                DigitalMarketingPostAsset,
            )

            optional.append((DigitalMarketingPostAsset, DigitalMarketingPostAsset.user_id))
        except Exception:
            pass
        try:
            from core.sms_sender_id.model.SmsSenderIdRegistration import (
                SmsSenderIdRegistration,
            )

            optional.append((SmsSenderIdRegistration, SmsSenderIdRegistration.user_id))
        except Exception:
            pass
        try:
            from core.credits.model.user_credit_balance import UserCreditBalance

            optional.append((UserCreditBalance, UserCreditBalance.user_id))
        except Exception:
            pass
        try:
            from core.credits.model.credit_usage_log import CreditUsageLog

            optional.append((CreditUsageLog, CreditUsageLog.user_id))
        except Exception:
            pass
        try:
            from core.notification.model.Notification import Notification

            optional.append((Notification, Notification.user_id))
        except Exception:
            pass
        try:
            from core.histories.model.history import History

            optional.append((History, History.user_id))
        except Exception:
            pass
        try:
            from core.receipts.model.Receipt import Receipt

            optional.append((Receipt, Receipt.user_id))
        except Exception:
            pass
        try:
            from core.auth.model.password_reset_token import PasswordResetToken

            optional.append((PasswordResetToken, PasswordResetToken.user_id))
        except Exception:
            pass
        try:
            from core.auth.model.refreshtoken import RefreshToken

            optional.append((RefreshToken, RefreshToken.user_id))
        except Exception:
            pass
        try:
            from core.conversationmanager.model.Conversation import DailyConversation

            optional.append((DailyConversation, DailyConversation.user_id))
        except Exception:
            pass
        try:
            from core.interventions.model.Intervention import Intervention

            optional.append((Intervention, Intervention.user_id))
        except Exception:
            pass

        for model, column in optional:
            self._delete_rows(model, column, user_id)

    def _delete_products(self, user_id: str) -> None:
        try:
            with self.db.begin_nested():
                products = self.db.query(Product).filter(Product.user_id == user_id).all()
                for product in products:
                    self.db.delete(product)
        except Exception as exc:
            logger.warning("Could not delete products for user %s: %s", user_id, exc)

    def _delete_rows(self, model: Type[Any], column: Any, user_id: str) -> None:
        try:
            with self.db.begin_nested():
                self.db.query(model).filter(column == user_id).delete(
                    synchronize_session=False
                )
        except Exception as exc:
            logger.warning(
                "Could not delete %s for user %s: %s",
                getattr(model, "__name__", model),
                user_id,
                exc,
            )

    def _cancel_subscriptions(self, user_id: str) -> None:
        try:
            now = datetime.now(timezone.utc)
            rows = (
                self.db.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_id,
                    UserSubscription.status == SubscriptionStatus.ACTIVE,
                )
                .all()
            )
            for row in rows:
                row.status = SubscriptionStatus.CANCELLED
                row.cancelled_at = now
                row.updated_at = now
        except Exception as exc:
            logger.warning("Could not cancel subscriptions for %s: %s", user_id, exc)

