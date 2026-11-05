from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, fields
import logging
import uuid

from utilities.dbconfig import SessionLocal

from core.nlu.model.Conversation import DailyConversation

logger = logging.getLogger(__name__)


@dataclass
class ConversationState:
    user_id: str
    conversation_id: str = ""
    conversation_lifecycle: str = "active"  # active | awaiting_followup_help | completed
    # After a RAG/FAQ answer: next user turn is checked for contentment before closing.
    awaiting_satisfaction: bool = False
    session_db_id: Optional[int] = None
    current_intent: str = ""
    collected_slots: Dict = None
    waiting_for_pin: bool = False
    pending_action: Dict = None
    conversation_history: List[Dict] = None
    conversation_date: date = None
    waiting_for_payment_confirmation: bool = False
    pending_payment_dto: Dict = None
    last_successful_transaction: Dict = None
    waiting_for_expense_date_selection: bool = False
    pending_expense_dates: List[Dict] = None
    intervention_active: bool = False
    intervention_id: Optional[int] = None
    intervention_trigger: Optional[str] = None
    intervention_reason: Optional[str] = None
    intervention_created_at: Optional[str] = None
    # Agent replies waiting to be shown to the customer (webchat poll / next inbound).
    pending_customer_messages: List[str] = None
    # Owner-facing identity (channel unique IDs stay on user_id for routing).
    customer_username: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_display_name: Optional[str] = None

    def __post_init__(self):
        if not self.conversation_id:
            self.conversation_id = str(uuid.uuid4())
        if self.collected_slots is None:
            self.collected_slots = {}
        if self.conversation_history is None:
            self.conversation_history = []
        if self.conversation_date is None:
            self.conversation_date = date.today()
        if self.pending_payment_dto is None:
            self.pending_payment_dto = {}
        if self.last_successful_transaction is None:
            self.last_successful_transaction = {}
        if self.pending_expense_dates is None:
            self.pending_expense_dates = []
        if self.pending_customer_messages is None:
            self.pending_customer_messages = []

    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "conversation_lifecycle": self.conversation_lifecycle,
            "awaiting_satisfaction": self.awaiting_satisfaction,
            "session_db_id": self.session_db_id,
            "current_intent": self.current_intent,
            "collected_slots": self.collected_slots,
            "waiting_for_pin": self.waiting_for_pin,
            "pending_action": self.pending_action,
            "conversation_history": self.conversation_history,
            "conversation_date": self.conversation_date.isoformat(),
            "waiting_for_payment_confirmation": self.waiting_for_payment_confirmation,
            "pending_payment_dto": self.pending_payment_dto,
            "last_successful_transaction": self.last_successful_transaction,
            "waiting_for_expense_date_selection": self.waiting_for_expense_date_selection,
            "pending_expense_dates": self.pending_expense_dates,
            "intervention_active": self.intervention_active,
            "intervention_id": self.intervention_id,
            "intervention_trigger": self.intervention_trigger,
            "intervention_reason": self.intervention_reason,
            "intervention_created_at": self.intervention_created_at,
            "pending_customer_messages": self.pending_customer_messages or [],
            "customer_username": self.customer_username,
            "customer_phone": self.customer_phone,
            "customer_display_name": self.customer_display_name,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ConversationState":
        data = dict(data)
        if "conversation_date" in data and isinstance(data["conversation_date"], str):
            data["conversation_date"] = date.fromisoformat(data["conversation_date"])
        field_names = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in field_names}
        return cls(**filtered)


class ConversationManager:
    def __init__(self):
        self.db = SessionLocal()
        self.memory_cache: Dict[str, ConversationState] = {}

    def sync_intervention_from_db(self, user_id: str) -> bool:
        """Copy an owner takeover from the database onto the in-memory session.

        The inbox API and the message worker do not share memory. Without this
        refresh, a cached session keeps ``intervention_active`` false and the
        assistant still replies after the owner flags the chat.

        A session the agent already marked completed is not a takeover. Holding
        that row in cache would append the customer's next message to the old
        history and put Mark as complete back on Live Chat.
        """
        state = self.get_conversation_state(user_id)
        session_id = state.session_db_id
        if not session_id:
            return bool(state.intervention_active)

        row = self._fetch_session_row(session_id)
        if not row:
            return bool(state.intervention_active)

        raw = row.conversation_state or {}
        if self._payload_is_completed(raw):
            self._forget_cached_session(user_id)
            fresh = self.get_conversation_state(user_id)
            return bool(fresh.intervention_active)

        if raw.get("intervention_active"):
            state.intervention_active = True
            state.intervention_id = raw.get("intervention_id")
            state.intervention_trigger = raw.get("intervention_trigger")
            state.intervention_reason = raw.get("intervention_reason")
            state.intervention_created_at = raw.get("intervention_created_at")
        return bool(state.intervention_active)

    def get_conversation_state(self, user_id: str) -> ConversationState:
        """Return the user's current (non-completed) conversation session, or start a new one."""
        cached = self.memory_cache.get(user_id)
        if cached is not None and not self._cached_session_is_closed(cached):
            return cached
        carried_identity = None
        if cached is not None:
            logger.info(
                "[CONVERSATION] Completed session %s for %s is closed; starting a new chat",
                cached.session_db_id,
                user_id,
            )
            carried_identity = (
                cached.customer_username,
                cached.customer_phone,
                cached.customer_display_name,
            )
            self._forget_cached_session(user_id)

        loaded = self._load_active_session_from_db(user_id)
        if loaded:
            self.memory_cache[user_id] = loaded
            return loaded

        state = ConversationState(user_id=user_id, conversation_date=date.today())
        if carried_identity:
            state.customer_username, state.customer_phone, state.customer_display_name = carried_identity
        self.memory_cache[user_id] = state
        return state

    def _forget_cached_session(self, user_id: str) -> None:
        self.memory_cache.pop(user_id, None)

    @staticmethod
    def _payload_is_completed(raw: Optional[Dict]) -> bool:
        return str((raw or {}).get("conversation_lifecycle") or "").lower() == "completed"

    def _fetch_session_row(self, session_id: int):
        """Reload one session from the database, ignoring this process's stale copy.

        The inbox request that marks a chat complete uses a different DB session
        than the message worker. Without a refresh, the worker still sees the
        pre-completion JSON and treats the closed chat as live.
        """
        self.db.expire_all()
        return (
            self.db.query(DailyConversation)
            .filter(DailyConversation.id == int(session_id))
            .first()
        )

    def _cached_session_is_closed(self, state: ConversationState) -> bool:
        if self._payload_is_completed({"conversation_lifecycle": state.conversation_lifecycle}):
            return True
        if not state.session_db_id:
            return False
        row = self._fetch_session_row(state.session_db_id)
        if not row:
            return False
        return self._payload_is_completed(row.conversation_state)

    def _load_active_session_from_db(self, user_id: str) -> Optional[ConversationState]:
        self.db.expire_all()
        rows = (
            self.db.query(DailyConversation)
            .filter(DailyConversation.user_id == user_id)
            .order_by(DailyConversation.updated_at.desc())
            .limit(40)
            .all()
        )
        for row in rows:
            raw = row.conversation_state or {}
            if self._payload_is_completed(raw):
                continue
            merged = dict(raw)
            if not merged.get("conversation_id"):
                merged["conversation_id"] = f"migrated-{row.id}"
            merged["session_db_id"] = row.id
            st = ConversationState.from_dict(merged)
            st.session_db_id = row.id
            return st
        return None

    def persist(self, user_id: str) -> None:
        """Write the cached conversation state without appending a message."""
        state = self.get_conversation_state(user_id)
        self._save_conversation_state(state)

    def update_conversation_history(self, user_id: str, role: str, content: str):
        state = self.get_conversation_state(user_id)
        state.conversation_history.append(
            {"role": role, "content": content, "timestamp": datetime.utcnow().isoformat()}
        )
        if len(state.conversation_history) > 20:
            state.conversation_history = state.conversation_history[-20:]
        if role == "human":
            pending = list(state.pending_customer_messages or [])
            pending.append(content)
            state.pending_customer_messages = pending
        self._save_conversation_state(state)

    def queue_pending_customer_message(self, user_id: str, content: str) -> None:
        """Queue an agent reply for delivery to the customer channel (webchat poll)."""
        state = self.get_conversation_state(user_id)
        pending = list(state.pending_customer_messages or [])
        pending.append(content)
        state.pending_customer_messages = pending
        self._save_conversation_state(state)

    def flush_pending_customer_messages(self, user_id: str) -> List[str]:
        """Return and clear queued agent replies for this conversation session.

        Always reloads from DB so replies written by the interventions API (another
        request / DB session) are visible even if this process has a stale cache.
        """
        self.db.expire_all()
        loaded = self._load_active_session_from_db(user_id)
        if loaded:
            self.memory_cache[user_id] = loaded
            state = loaded
        else:
            state = self.get_conversation_state(user_id)

        pending = [m for m in (state.pending_customer_messages or []) if str(m).strip()]
        if pending:
            state.pending_customer_messages = []
            self._save_conversation_state(state)
        return pending

    def _insert_row(self, state: ConversationState, payload: Dict):
        row = DailyConversation(
            user_id=state.user_id,
            conversation_date=state.conversation_date or date.today(),
            conversation_state=payload,
        )
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        state.session_db_id = row.id

    def _save_conversation_state(self, state: ConversationState):
        payload = state.to_dict()
        if state.session_db_id and not self._payload_is_completed(payload):
            closed = self._fetch_session_row(state.session_db_id)
            if closed and self._payload_is_completed(closed.conversation_state):
                logger.info(
                    "[CONVERSATION] Refusing to reopen completed session %s for %s",
                    state.session_db_id,
                    state.user_id,
                )
                state.session_db_id = None
                if self.memory_cache.get(state.user_id) is state:
                    self._forget_cached_session(state.user_id)
                return

        if state.session_db_id:
            row = (
                self.db.query(DailyConversation)
                .filter(DailyConversation.id == state.session_db_id)
                .first()
            )
            if row:
                from sqlalchemy.orm.attributes import flag_modified

                row.conversation_state = payload
                flag_modified(row, "conversation_state")
                row.conversation_date = state.conversation_date or date.today()
                row.updated_at = datetime.utcnow()
            else:
                self._insert_row(state, payload)
        else:
            self._insert_row(state, payload)

        try:
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            raise e

        self.memory_cache[state.user_id] = state

    def remember_customer_identity(
        self,
        user_id: str,
        *,
        username: Optional[str] = None,
        phone: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        """Persist username / phone on the active session so owners can recognize the customer."""
        from core.conversationmanager.service.customer_identity import (
            looks_like_phone,
            normalize_username,
        )

        state = self.get_conversation_state(user_id)
        changed = False

        cleaned_username = normalize_username(username)
        if cleaned_username and state.customer_username != cleaned_username:
            state.customer_username = cleaned_username
            changed = True

        cleaned_phone = (phone or "").strip()
        if cleaned_phone and looks_like_phone(cleaned_phone) and state.customer_phone != cleaned_phone:
            state.customer_phone = cleaned_phone
            changed = True

        cleaned_name = (display_name or "").strip()
        if cleaned_name and state.customer_display_name != cleaned_name:
            state.customer_display_name = cleaned_name
            changed = True

        if changed:
            self._save_conversation_state(state)

    def finalize_completed_session(self, user_id: str):
        """Mark the in-memory session completed, persist, and drop cache so the next turn starts fresh.

        Completing also leaves intervention mode so the thread is no longer live; a later
        customer message loads a new session and AI can reply again.
        """
        state = self.memory_cache.get(user_id) or self._load_active_session_from_db(user_id)
        if not state:
            return
        intervention_id = state.intervention_id
        state.conversation_lifecycle = "completed"
        state.awaiting_satisfaction = False
        state.intervention_active = False
        self._save_conversation_state(state)
        self.memory_cache.pop(user_id, None)
        self._close_open_intervention_row(intervention_id)

    def _close_open_intervention_row(self, intervention_id: Optional[int]) -> None:
        if intervention_id is None:
            return
        try:
            from core.interventions.model.Intervention import Intervention

            row = (
                self.db.query(Intervention)
                .filter(Intervention.id == int(intervention_id))
                .first()
            )
            if row and row.status == "open":
                row.status = "closed"
                row.closed_at = datetime.utcnow()
                self.db.commit()
        except Exception:
            try:
                self.db.rollback()
            except Exception:
                pass

    def reset_conversation_state(self, user_id: str):
        """End the current session (same as abandoning / PIN error hard reset)."""
        self.finalize_completed_session(user_id)

    def clear_collected_slots(self, user_id: str):
        state = self.get_conversation_state(user_id)
        state.collected_slots = {}
        self._save_conversation_state(state)

    def set_pending_action(self, user_id: str, intent: str, slots: Dict):
        state = self.get_conversation_state(user_id)
        state.waiting_for_pin = True
        state.pending_action = {
            "intent": intent,
            "slots": slots,
            "timestamp": datetime.now().isoformat(),
        }
        self._save_conversation_state(state)

    def get_previous_conversations(self, user_id: str, days_back: int = 7) -> List[ConversationState]:
        start_date = date.today() - timedelta(days=days_back)
        rows = (
            self.db.query(DailyConversation)
            .filter(
                DailyConversation.user_id == user_id,
                DailyConversation.conversation_date >= start_date,
            )
            .order_by(DailyConversation.updated_at.desc())
            .all()
        )
        return [ConversationState.from_dict(conv.conversation_state) for conv in rows]

    def cleanup_old_conversations(self, days_to_keep: int = 30):
        cutoff_date = date.today() - timedelta(days=days_to_keep)
        self.db.query(DailyConversation).filter(
            DailyConversation.conversation_date < cutoff_date
        ).delete()
        try:
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            raise e
