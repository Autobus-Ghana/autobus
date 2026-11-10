import json
import logging
import os
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Dict, List, Optional

import redis
from sqlalchemy.orm import Session

from config import settings
from core.customers.dto.customer_dto import (
    CustomerMessageRecipientResult,
    CustomerMessageResponse,
)
from core.customers.service.customer_service import CustomerService
from core.email.sender_email import SenderEmailNotConfigured, sender_email_from_user
from core.sms_sender_id.model.SmsSenderIdRegistration import (
    SmsSenderIdRegistration,
    SmsSenderIdStatus,
)
from core.socialmedia.service.zernio_client import ZernioAPIError, ZernioClient
from core.user.model.User import User
from utilities.auth_subject import resolve_internal_user_id

logger = logging.getLogger(__name__)

_SENT_SMS_KEY_PREFIX = "sms:sent:"
_SENT_SMS_MAX = 100


_SMS_REGION_NAMES = {
    "GH": "Ghana",
    "NG": "Nigeria",
    "KE": "Kenya",
    "ZA": "South Africa",
    "UG": "Uganda",
    "TZ": "Tanzania",
    "CI": "Côte d'Ivoire",
    "SN": "Senegal",
    "GB": "the UK",
    "US": "the US",
    "CA": "Canada",
}
_REGION_RE = re.compile(r"region\s+'([A-Za-z]{2})'", re.IGNORECASE)


def friendly_delivery_error(raw: str) -> str:
    """Turn a provider dump into a sentence a customer can act on."""
    text = (raw or "").strip()
    lower = text.lower()
    region = _REGION_RE.search(text)
    if "whitelisted destinations" in lower or region is not None:
        code = region.group(1).upper() if region is not None else ""
        place = _SMS_REGION_NAMES.get(code, "this country")
        return f"SMS to {place} numbers isn't available yet."
    if not text or "zernio" in lower or text.startswith("{") or len(text) > 160:
        return "The message couldn't be sent. Try again in a moment."
    return text


def _e164(phone: str) -> str:
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return f"+{digits}"
    if digits.startswith("233"):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+233{digits[1:]}"
    return f"+{digits}"


class CustomerMessagingService:
    """Send custom SMS or email to saved customers by customer ID."""

    def __init__(self, db: Session, redis_client=None):
        self.db = db
        self.customer_service = CustomerService(db)
        self._redis_client = redis_client

    def _approved_sender_id(self, user_id: str) -> str:
        internal_id = resolve_internal_user_id(self.db, user_id)
        rows = (
            self.db.query(SmsSenderIdRegistration)
            .filter(
                SmsSenderIdRegistration.user_id == internal_id,
                SmsSenderIdRegistration.is_active.is_(True),
            )
            .order_by(SmsSenderIdRegistration.created_at.desc())
            .all()
        )
        for row in rows:
            if row.status != SmsSenderIdStatus.APPROVED:
                continue
            sender = (row.sender_id or "").strip()
            if sender:
                return sender
        if any(row.status == SmsSenderIdStatus.PENDING for row in rows):
            raise ValueError(
                "Your SMS sender ID is waiting for approval. You can send SMS after it is approved."
            )
        raise ValueError("Register an SMS sender ID before sending to customers.")

    def send_sms(
        self,
        user_id: str,
        customer_ids: List[int],
        message: str,
    ) -> CustomerMessageResponse:
        sender_id = self._approved_sender_id(user_id)
        client = ZernioClient()
        customers_by_id = {
            c.id: c for c in self.customer_service.get_customers_by_ids(customer_ids, user_id)
        }
        results: List[CustomerMessageRecipientResult] = []

        for customer_id in customer_ids:
            customer = customers_by_id.get(customer_id)
            if not customer:
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer_id,
                        customer_name="",
                        success=False,
                        message="Customer not found or not accessible",
                    )
                )
                continue

            phone = _e164(getattr(customer, "customer_number", "") or "")
            if not phone:
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        success=False,
                        message="This customer has no phone number.",
                    )
                )
                continue

            try:
                sent = client.send_sms(
                    sender_id=sender_id,
                    to=phone,
                    text=message,
                    idempotency_key=str(uuid.uuid4()),
                )
                status_name = str(sent.get("status") or "sent").lower()
                self._record_sent_sms(
                    user_id,
                    phone=phone,
                    message=message,
                    status=status_name,
                )
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        success=True,
                        message="SMS sent successfully",
                        destination=phone,
                    )
                )
            except ZernioAPIError as exc:
                logger.error("Zernio SMS error for customer %s: %s", customer.id, exc)
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        success=False,
                        message=friendly_delivery_error(str(exc)),
                        destination=phone,
                    )
                )

        return self._build_response(results)

    def send_email(
        self,
        user_id: str,
        customer_ids: List[int],
        subject: str,
        body: str,
    ) -> CustomerMessageResponse:
        if not settings.ZEPTOMAIL_SMTP_PASSWORD:
            raise ValueError("Email service is not configured")

        user = self._resolve_user(user_id)
        from_email = sender_email_from_user(user)
        if not from_email:
            raise SenderEmailNotConfigured(
                "Add a from email in Messaging before sending. Example: noreply@usegreenmall.com"
            )

        display_name = ""
        if user is not None:
            display_name = ((user.company or user.fullname or "") or "").strip()
        from_header = formataddr((display_name, from_email)) if display_name else from_email
        reply_to = (user.email or "").strip() if user is not None else ""

        customers_by_id = {
            c.id: c for c in self.customer_service.get_customers_by_ids(customer_ids, user_id)
        }
        results: List[CustomerMessageRecipientResult] = []

        for customer_id in customer_ids:
            customer = customers_by_id.get(customer_id)
            if not customer:
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer_id,
                        customer_name="",
                        success=False,
                        message="Customer not found or not accessible",
                    )
                )
                continue

            if not customer.email:
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        success=False,
                        message="Customer has no email address. Add an email when creating or updating the customer.",
                    )
                )
                continue

            to_email = customer.email.strip()
            try:
                sent = self._send_email(
                    from_header,
                    to_email,
                    subject,
                    body,
                    reply_to=reply_to if reply_to and reply_to.lower() != from_email.lower() else None,
                )
                if sent:
                    self._record_sent_email(
                        user,
                        to_email=to_email,
                        subject=subject,
                    )
                    results.append(
                        CustomerMessageRecipientResult(
                            customer_id=customer.id,
                            customer_name=customer.name,
                            success=True,
                            message="Email sent successfully",
                            destination=to_email,
                        )
                    )
                else:
                    results.append(
                        CustomerMessageRecipientResult(
                            customer_id=customer.id,
                            customer_name=customer.name,
                            success=False,
                            message="Failed to send email",
                            destination=to_email,
                        )
                    )
            except Exception as exc:
                logger.error("Email send error for customer %s: %s", customer.id, exc)
                results.append(
                    CustomerMessageRecipientResult(
                        customer_id=customer.id,
                        customer_name=customer.name,
                        success=False,
                        message="Failed to send email. Please try again later.",
                        destination=to_email,
                    )
                )

        return self._build_response(results)

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        redis_password = os.getenv("REDIS_PASSWORD", "autobus098")
        self._redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            password=redis_password if redis_password else None,
            db=0,
            decode_responses=True,
        )
        return self._redis_client

    def _history_user_id(self, user_id: str) -> Optional[str]:
        return self.customer_service._resolve_user_db_id(user_id) or (user_id or "").strip() or None

    def _resolve_user(self, user_id: str) -> Optional[User]:
        db_id = self.customer_service._resolve_user_db_id(user_id)
        if not db_id:
            return None
        return self.db.query(User).filter(User.id == db_id).first()

    def _record_sent_email(
        self,
        user: Optional[User],
        *,
        to_email: str,
        subject: str,
    ) -> None:
        """Append outbound email metadata so Sent Emails history can show it."""
        keys: List[str] = []
        if user is not None:
            phone = (user.phone or "").strip()
            if phone:
                keys.append(f"email:sent:{phone}")
            if user.id:
                keys.append(f"email:sent:{user.id}")
        if not keys:
            return
        try:
            payload = json.dumps(
                {
                    "to": to_email,
                    "subject": subject,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            client = self._redis()
            for key in keys:
                client.lpush(key, payload)
                client.ltrim(key, 0, 99)
        except Exception as e:
            logger.warning("Could not record sent email: %s", e)

    def _record_sent_sms(
        self,
        user_id: str,
        *,
        phone: str,
        message: str,
        status: str = "Sent",
    ) -> None:
        """Append outbound SMS metadata for Sent SMS history (newest first)."""
        history_user_id = self._history_user_id(user_id)
        if not history_user_id:
            return
        try:
            payload = json.dumps(
                {
                    "phone": phone,
                    "message": message,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "status": status,
                }
            )
            key = f"{_SENT_SMS_KEY_PREFIX}{history_user_id}"
            client = self._redis()
            client.lpush(key, payload)
            client.ltrim(key, 0, _SENT_SMS_MAX - 1)
        except Exception as e:
            logger.warning("Could not record sent SMS for user %s: %s", user_id, e)

    def list_sent_sms_for_user(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent SMS sent by this user through customer messaging (Redis-backed)."""
        if limit < 1:
            limit = 1
        if limit > 50:
            limit = 50
        history_user_id = self._history_user_id(user_id)
        if not history_user_id:
            return []
        key = f"{_SENT_SMS_KEY_PREFIX}{history_user_id}"
        try:
            raw = self._redis().lrange(key, 0, limit - 1)
        except Exception as e:
            logger.error("Redis error listing sent SMS for %s: %s", user_id, e)
            return []
        out: List[Dict[str, Any]] = []
        for row in raw or []:
            try:
                parsed = json.loads(row)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    @staticmethod
    def _send_email(
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
        reply_to: Optional[str] = None,
    ) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        smtp_host = settings.ZEPTOMAIL_SMTP_HOST
        smtp_port = settings.ZEPTOMAIL_SMTP_PORT
        smtp_username = settings.ZEPTOMAIL_SMTP_USERNAME
        smtp_password = settings.ZEPTOMAIL_SMTP_PASSWORD

        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
        return True

    @staticmethod
    def _build_response(results: List[CustomerMessageRecipientResult]) -> CustomerMessageResponse:
        sent = sum(1 for r in results if r.success)
        failed = len(results) - sent
        return CustomerMessageResponse(
            total=len(results),
            sent=sent,
            failed=failed,
            results=results,
        )
