import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.conversationmanager.dto.conversation_response_dto import (
    ConversationDetailDTO,
    ConversationSummaryDTO,
)
from core.conversationmanager.service.customer_identity import (
    SOCIAL_CHANNEL_LABELS,
    customer_channel_key,
    is_instagram_conversation,
    looks_like_phone,
    parse_conversation_user_id,
    social_channel,
)
from core.interventions.model.Intervention import Intervention
from core.orders.model.order import Order
from core.nlu.model.Conversation import DailyConversation
from core.user.model.User import User

logger = logging.getLogger(__name__)


class CustomerDeliveryError(Exception):
    """WhatsApp/Instagram send failed for an agent reply."""


class ConversationListService:
    def __init__(self, db: Session):
        self.db = db

    def _normalize_phone_like(self, value: str) -> str:
        cleaned = "".join(ch for ch in (value or "") if ch.isdigit())
        if cleaned.startswith("233") and len(cleaned) > 3:
            cleaned = "0" + cleaned[3:]
        elif cleaned and not cleaned.startswith("0") and len(cleaned) == 9:
            cleaned = "0" + cleaned
        return cleaned

    def _resolve_user_db_id(self, user_identifier: str) -> Optional[str]:
        if not user_identifier:
            return None

        user = self.db.query(User).filter(User.id == user_identifier).first()
        if user:
            return user.id

        user = self.db.query(User).filter(User.email == user_identifier).first()
        if user:
            return user.id

        normalized_phone = self._normalize_phone_like(user_identifier)
        phone_candidates = {user_identifier}
        if normalized_phone:
            phone_candidates.add(normalized_phone)

        user = self.db.query(User).filter(User.phone.in_(list(phone_candidates))).first()
        return user.id if user else None

    def _conversation_user_ids(self, user_identifier: str) -> List[str]:
        """Identifiers that may appear as daily_conversations.user_id (id or phone)."""
        candidates = {user_identifier}
        resolved_id = self._resolve_user_db_id(user_identifier)
        if resolved_id:
            candidates.add(resolved_id)
            user = self.db.query(User).filter(User.id == resolved_id).first()
            if user and user.phone:
                candidates.add(user.phone)
                normalized = self._normalize_phone_like(user.phone)
                if normalized:
                    candidates.add(normalized)
        return [c for c in candidates if c]

    def _conversation_row_access_filter(self, user_identifier: str):
        """Match rows owned by phone/id variants or scoped ``<merchant_id>:<customer>`` sessions."""
        user_ids = self._conversation_user_ids(user_identifier)
        if not user_ids:
            return None
        resolved = self._resolve_user_db_id(user_identifier)
        parts = [DailyConversation.user_id.in_(user_ids)]
        if resolved:
            parts.append(DailyConversation.user_id.like(f"{resolved}:%"))
        return or_(*parts) if len(parts) > 1 else parts[0]

    def list_grouped_conversations_for_user(
        self,
        user_identifier: str,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[ConversationSummaryDTO], List[ConversationSummaryDTO]]:
        access = self._conversation_row_access_filter(user_identifier)
        if access is None:
            return [], []

        completed_rows = self._query_completed(access, skip=skip, limit=limit).all()
        intervention_rows = self._query_intervention_active(
            access, skip=skip, limit=limit
        ).all()

        user_keys = {row.user_id for row in completed_rows} | {
            row.user_id for row in intervention_rows
        }
        user_names, user_phones = self._load_user_display_fields(user_keys)
        saved_names = self._load_saved_customer_names(user_keys)

        completed = [
            self._to_summary(row, user_names, user_phones, saved_names)
            for row in completed_rows
        ]
        intervention_active = [
            self._to_summary(row, user_names, user_phones, saved_names)
            for row in intervention_rows
        ]
        return completed, intervention_active

    def _query_completed(self, access_filter, skip: int, limit: int):
        """Sessions not in human intervention (history / all-chats list).

        Previously this required conversation_lifecycle == completed, which hid
        sessions that left intervention while still bot-active.
        """
        intervention_flag = DailyConversation.conversation_state[
            "intervention_active"
        ].as_boolean()
        return (
            self.db.query(DailyConversation)
            .filter(
                access_filter,
                func.coalesce(intervention_flag, False).is_(False),
            )
            .order_by(DailyConversation.updated_at.desc())
            .offset(skip)
            .limit(limit)
        )

    def _query_intervention_active(self, access_filter, skip: int, limit: int):
        return (
            self.db.query(DailyConversation)
            .filter(
                access_filter,
                DailyConversation.conversation_state["intervention_active"].as_boolean().is_(True),
            )
            .order_by(DailyConversation.updated_at.desc())
            .offset(skip)
            .limit(limit)
        )

    def _load_user_display_fields(
        self, user_ids: set
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Resolve customer fullnames and canonical phone numbers by conversation user_id keys."""
        if not user_ids:
            return {}, {}
        lookup_keys: set = set()
        for uid in user_ids:
            if not uid:
                continue
            lookup_keys.add(uid)
            s = str(uid)
            if ":" in s:
                left, _, right = s.partition(":")
                lookup_keys.add(left.strip())
                lookup_keys.add(right.strip())
            normalized = self._normalize_phone_like(s)
            if normalized:
                lookup_keys.add(normalized)
        users = (
            self.db.query(User)
            .filter(
                or_(User.phone.in_(list(lookup_keys)), User.id.in_(list(user_ids)))
            )
            .all()
        )
        names: Dict[str, str] = {}
        phones: Dict[str, str] = {}
        for user in users:
            names[user.id] = user.fullname
            if user.phone:
                canonical = (user.phone or "").strip()
                if canonical:
                    names[user.phone] = user.fullname
                    phones[user.id] = canonical
                    phones[canonical] = canonical
                    normalized = self._normalize_phone_like(canonical)
                    if normalized and normalized != canonical:
                        phones[normalized] = canonical
        for uid in user_ids:
            if not uid or uid in phones:
                continue
            nu = self._normalize_phone_like(str(uid))
            if nu and nu in phones:
                phones[str(uid)] = phones[nu]
        return names, phones

    def _load_user_fullnames(self, user_ids: set) -> Dict[str, str]:
        names, _ = self._load_user_display_fields(user_ids)
        return names

    def _load_saved_customer_names(self, conversation_user_ids: set) -> Dict[str, str]:
        """Map normalized phone -> saved customer name for the conversation merchants."""
        if not conversation_user_ids:
            return {}
        from core.customers.model.customer import Customer

        merchant_ids = set()
        phones: set = set()
        for uid in conversation_user_ids:
            merchant_id, _, customer_key = parse_conversation_user_id(uid)
            if merchant_id:
                merchant_ids.add(merchant_id)
            if looks_like_phone(customer_key):
                phones.add(customer_key)
                normalized = self._normalize_phone_like(customer_key)
                if normalized:
                    phones.add(normalized)
        if not merchant_ids or not phones:
            return {}

        rows = (
            self.db.query(Customer)
            .filter(
                Customer.user_id.in_(list(merchant_ids)),
                Customer.is_active.is_(True),
                Customer.customer_number.in_(list(phones)),
            )
            .all()
        )
        names: Dict[str, str] = {}
        for row in rows:
            number = (row.customer_number or "").strip()
            name = (row.name or "").strip()
            if not number or not name:
                continue
            names[number] = name
            normalized = self._normalize_phone_like(number)
            if normalized:
                names[normalized] = name
        return names

    def _identity_from_row(
        self,
        row: DailyConversation,
        user_names: Dict[str, str],
        user_phones: Optional[Dict[str, str]] = None,
        saved_names: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (username, phone, display_name) for owner-facing chat labels."""
        state = row.conversation_state or {}
        username = (state.get("customer_username") or "").strip() or None
        stored_phone = (state.get("customer_phone") or "").strip() or None
        display_name = (state.get("customer_display_name") or "").strip() or None

        channel_key = customer_channel_key(row.user_id)
        resolved_phone = stored_phone if stored_phone and looks_like_phone(stored_phone) else None
        if not resolved_phone:
            resolved_phone = self._resolve_customer_phone(row.user_id, user_phones or {})

        if not display_name:
            display_name = (
                user_names.get(channel_key)
                or user_names.get(row.user_id)
                or None
            )
        if resolved_phone and saved_names:
            saved = saved_names.get(resolved_phone) or saved_names.get(
                self._normalize_phone_like(resolved_phone)
            )
            if saved:
                display_name = display_name or saved

        if not username and not resolved_phone and not display_name:
            channel = social_channel(row.user_id)
            if channel:
                display_name = SOCIAL_CHANNEL_LABELS.get(channel, "Message")
            elif looks_like_phone(channel_key):
                resolved_phone = channel_key

        return username, resolved_phone, display_name

    def _to_summary(
        self,
        row: DailyConversation,
        user_names: Dict[str, str],
        user_phones: Dict[str, str],
        saved_names: Optional[Dict[str, str]] = None,
    ) -> ConversationSummaryDTO:
        state = row.conversation_state or {}
        history = state.get("conversation_history") or []
        last_message = self._last_customer_message(history)
        username, phone, display_name = self._identity_from_row(
            row, user_names, user_phones, saved_names
        )
        channel_key = customer_channel_key(row.user_id)

        return ConversationSummaryDTO(
            id=row.id,
            conversation_id=state.get("conversation_id"),
            user_id=row.user_id,
            user_fullname=display_name
            or user_names.get(channel_key)
            or user_names.get(row.user_id),
            conversation_date=row.conversation_date,
            conversation_lifecycle=state.get("conversation_lifecycle", "active"),
            intervention_active=bool(state.get("intervention_active", False)),
            intervention_id=state.get("intervention_id"),
            intervention_reason=state.get("intervention_reason"),
            current_intent=state.get("current_intent") or None,
            last_message=last_message,
            customer_phone=phone,
            customer_username=username,
            customer_display_name=display_name,
            message_count=len(history),
            created_at=row.created_at or datetime.utcnow(),
            updated_at=row.updated_at or datetime.utcnow(),
        )

    @staticmethod
    def _customer_channel_id(conversation_user_id: Optional[str]) -> str:
        return customer_channel_key(conversation_user_id)

    def _resolve_customer_phone(
        self, conversation_user_id: Optional[str], user_phones: Dict[str, str]
    ) -> Optional[str]:
        if social_channel(conversation_user_id):
            return None
        uid = customer_channel_key(conversation_user_id)
        if not uid:
            return None
        candidates = {uid}
        normalized = self._normalize_phone_like(uid)
        if normalized:
            candidates.add(normalized)
        for key in candidates:
            if key in user_phones:
                return user_phones[key]
        if looks_like_phone(uid):
            return uid
        return None

    @staticmethod
    def _last_customer_message(history: list) -> Optional[str]:
        """Latest message from the customer (role user), not bot or human agent."""
        if not history:
            return None
        for entry in reversed(history):
            role = (entry.get("role") or "").strip().lower()
            if role not in ("user", "customer"):
                continue
            content = entry.get("content")
            if content is None and entry.get("text") is not None:
                content = entry.get("text")
            if content is None:
                continue
            text = str(content)
            return text[:500] if len(text) > 500 else text
        return None

    def _session_owned_by_user(self, session_id: int, user_identifier: str) -> Optional[DailyConversation]:
        access = self._conversation_row_access_filter(user_identifier)
        if access is None:
            return None
        return (
            self.db.query(DailyConversation)
            .filter(
                DailyConversation.id == int(session_id),
                access,
            )
            .first()
        )

    def get_session_detail(
        self, user_identifier: str, session_id: int
    ) -> Optional[ConversationDetailDTO]:
        row = self._session_owned_by_user(session_id, user_identifier)
        if not row:
            return None
        self._backfill_instagram_identity(row)
        user_names = self._load_user_fullnames({row.user_id})
        saved_names = self._load_saved_customer_names({row.user_id})
        return self._to_detail(row, user_names, saved_names)

    def get_conversation_for_order(
        self, merchant_user_id: str, order_id: str
    ) -> Optional[ConversationDetailDTO]:
        """Load the most recent conversation for the order's customer."""
        try:
            order_uuid = uuid.UUID(str(order_id))
        except ValueError:
            return None
        order = self.db.query(Order).filter(Order.order_id == order_uuid).first()
        if not order:
            return None
        if order.user_id and str(order.user_id) != str(merchant_user_id):
            return None

        customer_key = (order.customer_phone or order.customer_id or "").strip()
        if not customer_key:
            return None

        candidates = {customer_key}
        normalized = self._normalize_phone_like(customer_key)
        if normalized:
            candidates.add(normalized)

        row = (
            self.db.query(DailyConversation)
            .filter(DailyConversation.user_id.in_(list(candidates)))
            .order_by(DailyConversation.updated_at.desc())
            .first()
        )
        if not row:
            return None
        user_names = self._load_user_fullnames({row.user_id})
        saved_names = self._load_saved_customer_names({row.user_id})
        return self._to_detail(row, user_names, saved_names)

    def _to_detail(
        self,
        row: DailyConversation,
        user_names: Dict[str, str],
        saved_names: Optional[Dict[str, str]] = None,
    ) -> ConversationDetailDTO:
        state = row.conversation_state or {}
        history = state.get("conversation_history") or []
        username, phone, display_name = self._identity_from_row(
            row, user_names, saved_names=saved_names
        )
        channel_key = customer_channel_key(row.user_id)
        return ConversationDetailDTO(
            id=row.id,
            conversation_id=state.get("conversation_id"),
            user_id=row.user_id,
            user_fullname=display_name
            or user_names.get(channel_key)
            or user_names.get(row.user_id),
            conversation_date=row.conversation_date,
            conversation_lifecycle=state.get("conversation_lifecycle", "active"),
            intervention_active=bool(state.get("intervention_active", False)),
            intervention_id=state.get("intervention_id"),
            intervention_reason=state.get("intervention_reason"),
            current_intent=state.get("current_intent") or None,
            conversation_history=history,
            collected_slots=state.get("collected_slots"),
            customer_phone=phone,
            customer_username=username,
            customer_display_name=display_name,
            created_at=row.created_at or datetime.utcnow(),
            updated_at=row.updated_at or datetime.utcnow(),
        )

    def _backfill_instagram_identity(self, row: DailyConversation) -> None:
        """Resolve and persist an Instagram username when an older session only has an IGSID."""
        if not is_instagram_conversation(row.user_id):
            return
        state = dict(row.conversation_state or {})
        if (state.get("customer_username") or "").strip():
            return
        merchant_id, _, igsid = parse_conversation_user_id(row.user_id)
        if not merchant_id or not igsid:
            return
        try:
            from core.instagram.model.InstagramAccount import InstagramAccount
            from core.instagram.service.instagram_oauth_service import InstagramOAuthService

            account = (
                self.db.query(InstagramAccount)
                .filter(
                    InstagramAccount.user_id == merchant_id,
                    InstagramAccount.is_active.is_(True),
                    InstagramAccount.messaging_enabled.is_(True),
                )
                .first()
            )
            if not account:
                return
            svc = InstagramOAuthService()
            token = svc.decrypt_token(account.access_token_encrypted)
            profile = svc.fetch_igsid_profile(token, igsid)
            username = str(profile.get("username") or "").strip()
            display_name = str(profile.get("name") or "").strip()
            if not username and not display_name:
                return
            if username:
                state["customer_username"] = username
            if display_name and not (state.get("customer_display_name") or "").strip():
                state["customer_display_name"] = display_name
            from sqlalchemy.orm.attributes import flag_modified

            row.conversation_state = state
            flag_modified(row, "conversation_state")
            self.db.commit()
            self.db.refresh(row)
        except Exception as exc:
            logger.warning(
                "[CONVERSATION] Instagram identity backfill failed for %s: %s",
                row.user_id,
                exc,
            )
            try:
                self.db.rollback()
            except Exception:
                pass

    def _deliver_agent_reply_to_customer_channel(
        self, conversation_user_id: str, message: str
    ) -> bool:
        """
        Push an agent reply to WhatsApp or Instagram when the session is on that channel.

        Returns True if a live channel send succeeded (do not also queue for poll).
        Returns False if there is no outbound channel (e.g. public webchat — queue instead).
        Raises CustomerDeliveryError if a live channel send was required and failed.
        """
        text = (message or "").strip()
        uid = (conversation_user_id or "").strip()
        if not text or ":" not in uid:
            return False

        merchant_id, _, channel = uid.partition(":")
        merchant_id = merchant_id.strip()
        channel = (channel or "").strip()
        if not merchant_id or not channel:
            return False

        social = social_channel(uid)
        if social:
            if self._send_zernio_agent_reply(uid, text):
                return True
            if social == "ig":
                igsid = channel[3:].strip()
                if not self._send_instagram_agent_reply(merchant_id, igsid, text):
                    raise CustomerDeliveryError("Failed to send Instagram reply to the customer.")
                return True
            raise CustomerDeliveryError("Failed to send the reply to the customer.")

        from core.webhooks.service.whatsapp_service import WhatsAppService

        digits = "".join(ch for ch in channel if ch.isdigit())
        if len(digits) < 9:
            return False

        account = WhatsAppService.account_for_merchant(merchant_id, self.db)
        if not account:
            return False

        recipient = "".join(ch for ch in channel if ch.isdigit()) or channel
        svc = WhatsAppService.for_phone_id(account.phone_number_id, self.db)
        sent = svc.send_message(
            phone_id=account.phone_number_id,
            recipient_phone=recipient,
            message_text=text,
        )
        if not sent:
            raise CustomerDeliveryError("Failed to send WhatsApp reply to the customer.")
        logger.info(
            "[CONVERSATION] Delivered agent WhatsApp reply merchant=%s customer=%s",
            merchant_id,
            recipient,
        )
        return True

    def _send_instagram_agent_reply(
        self, merchant_id: str, igsid: str, message: str
    ) -> bool:
        from core.instagram.model.InstagramAccount import InstagramAccount
        from core.instagram.service.instagram_oauth_service import InstagramOAuthService

        account = (
            self.db.query(InstagramAccount)
            .filter(
                InstagramAccount.user_id == merchant_id,
                InstagramAccount.is_active.is_(True),
                InstagramAccount.messaging_enabled.is_(True),
            )
            .first()
        )
        if not account:
            return False
        svc = InstagramOAuthService()
        token = svc.decrypt_token(account.access_token_encrypted)
        return svc.send_text(token, igsid, message)

    def _send_zernio_agent_reply(self, autobus_key: str, message: str) -> bool:
        from core.socialmedia.service.zernio_client import ZernioAPIError
        from core.socialmedia.service.zernio_service import send_thread_reply

        try:
            return send_thread_reply(self.db, autobus_key, message)
        except ZernioAPIError as exc:
            logger.warning("[CONVERSATION] Zernio reply failed for %s: %s", autobus_key, exc)
            return False

    def append_human_message_to_session(
        self, user_identifier: str, session_id: int, message: str
    ) -> Optional[ConversationDetailDTO]:
        """Append an agent/human message to a session owned by the authenticated user."""
        row = self._session_owned_by_user(session_id, user_identifier)
        if not row:
            return None

        state = dict(row.conversation_state or {})
        if not state.get("intervention_active"):
            return None

        channel_sent = self._deliver_agent_reply_to_customer_channel(row.user_id, message)

        history = list(state.get("conversation_history") or [])
        history.append(
            {
                "role": "human",
                "content": message,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        if len(history) > 20:
            history = history[-20:]
        state["conversation_history"] = history

        # Webchat poll only — WhatsApp/Instagram already received the live send.
        pending = list(state.get("pending_customer_messages") or [])
        if not channel_sent:
            pending.append(message)
        state["pending_customer_messages"] = pending

        row.conversation_state = state
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "conversation_state")
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)

        # Keep NLU ConversationManager cache in sync when the same process handles webhooks.
        try:
            from core.nlu.service.conversation_manager import ConversationManager

            cm = ConversationManager()
            cached = cm.memory_cache.get(row.user_id)
            if cached and cached.session_db_id == row.id:
                cached.conversation_history = list(history)
                cached.pending_customer_messages = list(pending)
        except Exception:
            pass

        user_names = self._load_user_fullnames({row.user_id})
        return self._to_detail(row, user_names)

    def activate_intervention_for_session(
        self, user_identifier: str, session_id: int
    ) -> Optional[ConversationDetailDTO]:
        """Owner takes over a customer session so the assistant stays silent.

        Raises ValueError when the session is already completed. A later customer
        message starts a new assistant chat; this thread is not reopened.
        """
        row = self._session_owned_by_user(session_id, user_identifier)
        if not row:
            return None

        from sqlalchemy.orm.attributes import flag_modified

        from core.interventions.service.intervention_service import InterventionService

        state = dict(row.conversation_state or {})
        if str(state.get("conversation_lifecycle") or "").lower() == "completed":
            raise ValueError("completed")

        if not state.get("intervention_active"):
            reason = "Owner is intervening in this conversation"
            intervention = InterventionService(self.db).create_intervention(
                user_id=row.user_id,
                trigger="owner_takeover",
                reason=reason,
                conversation_date=row.conversation_date,
                metadata={"source": "inbox", "session_id": int(row.id)},
            )
            state = dict(row.conversation_state or {})
            state["intervention_active"] = True
            state["intervention_id"] = int(intervention.id)
            state["intervention_trigger"] = "owner_takeover"
            state["intervention_reason"] = reason
            state["intervention_created_at"] = (
                intervention.created_at.isoformat()
                if intervention.created_at
                else datetime.utcnow().isoformat()
            )
            row.conversation_state = state
            flag_modified(row, "conversation_state")
            row.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(row)
            self._mark_nlu_intervening(row.user_id, int(row.id), state)

        user_names = self._load_user_fullnames({row.user_id})
        return self._to_detail(row, user_names)

    def _mark_nlu_intervening(
        self, conversation_user_id: str, session_id: int, state: dict
    ) -> None:
        """Flip the in-process assistant cache so the next turn does not reply."""
        try:
            from core.nlu.controller.nlucontroller import nlu_system

            cached = nlu_system.conversation_manager.memory_cache.get(conversation_user_id)
            if (
                cached
                and cached.session_db_id is not None
                and int(cached.session_db_id) == int(session_id)
            ):
                cached.intervention_active = True
                cached.intervention_id = state.get("intervention_id")
                cached.intervention_trigger = state.get("intervention_trigger")
                cached.intervention_reason = state.get("intervention_reason")
                cached.intervention_created_at = state.get("intervention_created_at")
                return
            nlu_system.conversation_manager.memory_cache.pop(conversation_user_id, None)
        except Exception:
            logger.debug(
                "[CONVERSATION] NLU cache not updated for intervention on %s",
                conversation_user_id,
                exc_info=True,
            )

    def deactivate_intervention_for_session(
        self, user_identifier: str, session_id: int
    ) -> Optional[ConversationDetailDTO]:
        """Deprecated alias: completing the session is the only way to leave intervention."""
        return self.complete_conversation_session(user_identifier, session_id)

    def complete_conversation_session(
        self, user_identifier: str, session_id: int
    ) -> Optional[ConversationDetailDTO]:
        """Mark a session completed and close any open intervention.

        The team agent stays in control for the life of an intervention; completing
        the thread is what allows a later customer message to start a new AI session.
        """
        row = self._session_owned_by_user(session_id, user_identifier)
        if not row:
            return None

        from sqlalchemy.orm.attributes import flag_modified

        state = dict(row.conversation_state or {})
        intervention_id = state.get("intervention_id")
        if intervention_id is not None:
            intervention = (
                self.db.query(Intervention)
                .filter(Intervention.id == int(intervention_id))
                .first()
            )
            if intervention and intervention.status == "open":
                intervention.status = "closed"
                intervention.closed_at = datetime.utcnow()

        state["intervention_active"] = False
        state["awaiting_satisfaction"] = False
        state["conversation_lifecycle"] = "completed"
        row.conversation_state = state
        flag_modified(row, "conversation_state")
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)

        self._invalidate_nlu_session_cache(row.user_id)

        user_names = self._load_user_fullnames({row.user_id})
        return self._to_detail(row, user_names)

    @staticmethod
    def _invalidate_nlu_session_cache(conversation_user_id: str) -> None:
        try:
            from core.nlu.controller.nlucontroller import nlu_system

            nlu_system.conversation_manager.memory_cache.pop(conversation_user_id, None)
        except Exception:
            pass
