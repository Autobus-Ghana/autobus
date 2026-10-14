"""In-app and SMS notifications for operational events (interventions, orders)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from config import settings
from core.notification.model.Notification import NotificationType
from core.notification.service.notification_service import NotificationService
from core.orders.model.order import Order
from core.orders.order_messages import format_out_of_stock_notice
from core.user.model.User import User

logger = logging.getLogger(__name__)


class EventNotificationService:
    def __init__(self, db: Session):
        self.db = db
        self._notifications = NotificationService(db)

    @staticmethod
    def _is_placeholder_recipient(value: str) -> bool:
        text = (value or "").strip().lower()
        if not text:
            return True
        return (
            text.startswith("change_me")
            or "example.com" in text
            or text in {"changeme", "admin", "user_id"}
        )

    def _admin_recipient_ids(self) -> List[str]:
        raw = getattr(settings, "ADMIN_NOTIFICATION_USER_IDS", "") or ""
        recipients: List[str] = []
        for uid in raw.split(","):
            uid = uid.strip()
            if not uid or self._is_placeholder_recipient(uid):
                continue
            recipients.append(uid)
        return recipients

    def _resolve_user_db_id(self, user_identifier: str) -> Optional[str]:
        if not user_identifier:
            return None

        user = self.db.query(User).filter(User.id == user_identifier).first()
        if user:
            return user.id

        user = self.db.query(User).filter(User.email == user_identifier).first()
        if user:
            return user.id

        phone_candidates = {user_identifier}
        normalized = self._normalize_phone_like(user_identifier)
        if normalized:
            phone_candidates.add(normalized)

        user = self.db.query(User).filter(User.phone.in_(list(phone_candidates))).first()
        if user:
            return user.id

        merchant_id, _ = self._split_conversation_user_id(user_identifier)
        if merchant_id and merchant_id != user_identifier:
            return self._resolve_user_db_id(merchant_id)
        return None

    @staticmethod
    def _normalize_phone_like(value: str) -> str:
        cleaned = "".join(ch for ch in (value or "") if ch.isdigit())
        if cleaned.startswith("233") and len(cleaned) > 3:
            cleaned = "0" + cleaned[3:]
        elif cleaned and not cleaned.startswith("0") and len(cleaned) == 9:
            cleaned = "0" + cleaned
        return cleaned

    @staticmethod
    def _split_conversation_user_id(user_id: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse ``{merchant_id}:{customer_channel}`` conversation keys."""
        if not user_id or ":" not in user_id:
            return None, None
        merchant_id, _, rest = user_id.partition(":")
        merchant_id = merchant_id.strip()
        rest = (rest or "").strip()
        if not merchant_id or not rest:
            return None, None
        return merchant_id, rest

    @staticmethod
    def _customer_label_for_sms(customer_channel: Optional[str]) -> str:
        if not customer_channel:
            return "A customer"
        channel = customer_channel.strip()
        if channel.lower().startswith("ig:"):
            return "An Instagram customer"
        digits = "".join(ch for ch in channel if ch.isdigit())
        if len(digits) >= 9:
            return f"Customer {channel}"
        return "A customer"

    @staticmethod
    def _user_sms_phone(user: Optional[User]) -> Optional[str]:
        if user is None:
            return None
        for candidate in (getattr(user, "phone", None), getattr(user, "whatsapp_number", None)):
            phone = (candidate or "").strip()
            if phone:
                return phone
        return None

    def _notify_user_safe(
        self,
        user_id: str,
        notification_type: NotificationType,
        data: Dict[str, Any],
        *,
        send_sms: bool = False,
        sms_phone: Optional[str] = None,
    ) -> None:
        try:
            self._notifications.create_notification(
                user_id=user_id,
                notification_type=notification_type,
                data=data,
                send_sms=send_sms,
                sms_phone=sms_phone,
            )
            logger.info(
                "[EVENT_NOTIFICATION] Notified user %s event=%s sms=%s",
                user_id,
                data.get("event"),
                bool(send_sms and sms_phone),
            )
        except Exception as exc:
            try:
                self.db.rollback()
            except Exception:
                logger.debug(
                    "[EVENT_NOTIFICATION] Rollback after notify failure for %s failed",
                    user_id,
                    exc_info=True,
                )
            logger.error(
                "[EVENT_NOTIFICATION] Failed to notify user %s (%s): %s",
                user_id,
                data.get("event"),
                exc,
                exc_info=True,
            )

    def _notify_admins(
        self,
        notification_type: NotificationType,
        data: Dict[str, Any],
        *,
        send_sms: bool = False,
        skip_user_ids: Optional[Set[str]] = None,
    ) -> None:
        recipients = self._admin_recipient_ids()
        if not recipients:
            logger.debug(
                "[EVENT_NOTIFICATION] No ADMIN_NOTIFICATION_USER_IDS configured; skipping admin notify for %s",
                data.get("event"),
            )
            return

        skip = skip_user_ids or set()
        for admin_id in recipients:
            resolved = self._resolve_user_db_id(admin_id)
            if not resolved:
                logger.warning(
                    "[EVENT_NOTIFICATION] Skipping unknown admin recipient %s for %s",
                    admin_id,
                    data.get("event"),
                )
                continue
            if admin_id in skip or resolved in skip:
                continue
            admin = self.db.query(User).filter(User.id == resolved).first()
            sms_phone = self._user_sms_phone(admin) if send_sms else None
            self._notify_user_safe(
                resolved,
                notification_type,
                data,
                send_sms=bool(send_sms and sms_phone),
                sms_phone=sms_phone,
            )

    def notify_intervention_active(
        self,
        *,
        user_id: str,
        intervention_id: int,
        trigger: str,
        reason: Optional[str] = None,
        conversation_date: Optional[str] = None,
    ) -> None:
        """Notify the business owner by SMS and in-app that a customer needs attention."""
        merchant_id, customer_channel = self._split_conversation_user_id(user_id)
        customer_label = self._customer_label_for_sms(customer_channel)
        ask = " ".join((reason or "").split())
        if len(ask) > 90:
            ask = ask[:87].rstrip() + "..."
        detail = f' They said: "{ask}".' if ask else ""
        sms_body = (
            f"AutoBus: Please attend an intervention. {customer_label} needs you."
            f"{detail} Open Live Chats."
        )
        display_reason = (reason or trigger or "Agent handover").strip()
        data = {
            "event": "intervention_active",
            "title": "Conversation needs attention",
            "content": f"A customer conversation was flagged for human support ({trigger}).",
            "message": sms_body,
            "user_id": user_id,
            "intervention_id": intervention_id,
            "trigger": trigger,
            "reason": reason,
            "display_reason": display_reason,
            "conversation_date": conversation_date,
            "merchant_id": merchant_id,
            "customer_channel": customer_channel,
        }

        notified: Set[str] = set()
        if merchant_id:
            resolved = self._resolve_user_db_id(merchant_id)
            if resolved:
                merchant = self.db.query(User).filter(User.id == resolved).first()
                sms_phone = self._user_sms_phone(merchant)
                if not sms_phone:
                    logger.warning(
                        "[EVENT_NOTIFICATION] Merchant %s has no phone for intervention SMS",
                        resolved,
                    )
                self._notify_user_safe(
                    resolved,
                    NotificationType.ALERT,
                    data,
                    send_sms=bool(sms_phone),
                    sms_phone=sms_phone,
                )
                notified.add(resolved)
            else:
                logger.warning(
                    "[EVENT_NOTIFICATION] Could not resolve merchant %s for intervention %s",
                    merchant_id,
                    intervention_id,
                )
        else:
            logger.info(
                "[EVENT_NOTIFICATION] Conversation %s is not merchant-scoped; skipping owner SMS",
                user_id,
            )

        self._notify_admins(NotificationType.ALERT, data, skip_user_ids=notified)

    def _resolve_order_owner_id(self, order: Order) -> Optional[str]:
        """Resolve the merchant/business owner who should be notified about this order."""
        if not getattr(order, "user_id", None):
            return None
        return self._resolve_user_db_id(order.user_id)

    def notify_order_created(self, order: Order) -> None:
        """Notify the business owner by SMS and in-app that a new order was placed."""
        item_name = None
        quantity = None
        if order.order_items and isinstance(order.order_items, list) and order.order_items:
            first_item = order.order_items[0] or {}
            if isinstance(first_item, dict):
                item_name = first_item.get("name")
                quantity = first_item.get("quantity")

        requires_cs = bool(
            isinstance(order.custom_metadata, dict)
            and order.custom_metadata.get("requires_cs_followup")
        )
        cs_note = " Product not listed in catalog — customer service follow-up needed." if requires_cs else ""
        customer = (order.customer_name or "a customer").strip()
        qty = quantity or order.total_quantity
        sms_body = (
            f"AutoBus: New order {order.order_number} from {customer} "
            f"({order.customer_phone or 'no phone'}). "
            f"{item_name or 'Item'} x{qty} — "
            f"{order.total_amount} {order.currency_code}. Open Orders to view."
        )

        owner_data = {
            "event": "order_created",
            "title": "New order received",
            "content": (
                f"Order {order.order_number} from {customer} "
                f"({order.customer_phone or 'no phone'}).{cs_note}"
            ),
            "message": sms_body,
            "order_id": str(order.order_id),
            "order_number": order.order_number,
            "customer_name": order.customer_name,
            "customer_phone": order.customer_phone,
            "item_name": item_name,
            "quantity": quantity,
            "total_amount": str(order.total_amount),
            "currency_code": order.currency_code,
            "order_status": order.order_status,
            "product_listed": not requires_cs,
            "requires_cs_followup": requires_cs,
        }

        notified: Set[str] = set()
        owner_id = self._resolve_order_owner_id(order)
        if owner_id:
            owner = self.db.query(User).filter(User.id == owner_id).first()
            sms_phone = self._user_sms_phone(owner)
            if not sms_phone:
                logger.warning(
                    "[EVENT_NOTIFICATION] Merchant %s has no phone for order SMS",
                    owner_id,
                )
            self._notify_user_safe(
                owner_id,
                NotificationType.TRANSACTIONAL,
                owner_data,
                send_sms=bool(sms_phone),
                sms_phone=sms_phone,
            )
            notified.add(owner_id)
        else:
            logger.warning(
                "[EVENT_NOTIFICATION] Could not resolve merchant for order %s (user_id=%s)",
                order.order_number,
                getattr(order, "user_id", None),
            )

        self._notify_admins(
            NotificationType.TRANSACTIONAL,
            owner_data,
            skip_user_ids=notified,
        )

        if order.customer_phone:
            customer_user_id = self._resolve_user_db_id(order.customer_phone)
            if customer_user_id and customer_user_id not in notified:
                customer_message = (
                    f"Order {order.order_number} confirmed. "
                    f"Total: {order.total_amount} {order.currency_code}"
                )
                if requires_cs and item_name:
                    customer_message = (
                        f"{customer_message}\n\n"
                        f"{format_out_of_stock_notice(str(item_name))}"
                    )
                customer_data = {
                    "event": "order_created",
                    "title": "Order placed",
                    "content": f"Your order {order.order_number} has been received.",
                    "message": customer_message,
                    "order_id": str(order.order_id),
                    "order_number": order.order_number,
                }
                self._notify_user_safe(
                    customer_user_id,
                    NotificationType.SUCCESS,
                    customer_data,
                )
