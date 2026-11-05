"""A completed intervention must not come back as the next live chat."""

from __future__ import annotations

import unittest
from unittest import mock

from core.nlu.service.conversation_manager import ConversationManager, ConversationState


class _Row:
    def __init__(self, row_id, user_id, state):
        self.id = row_id
        self.user_id = user_id
        self.conversation_state = state
        self.conversation_date = None
        self.updated_at = None


def _manager(rows):
    cm = ConversationManager.__new__(ConversationManager)
    cm.memory_cache = {}
    cm.db = mock.Mock()

    def query(_model):
        found = mock.Mock()
        found.filter.return_value = found
        found.order_by.return_value = found
        found.limit.return_value = found
        found.first.return_value = rows[0] if rows else None
        found.all.return_value = list(rows)
        return found

    cm.db.query.side_effect = query
    return cm


class CompletedSessionRestartTests(unittest.TestCase):
    def _closed_chat(self):
        payload = {
            "conversation_id": "old-chat",
            "conversation_lifecycle": "completed",
            "intervention_active": False,
            "intervention_id": 9,
            "conversation_history": [
                {"role": "user", "content": "previous question"},
                {"role": "human", "content": "agent reply"},
            ],
            "customer_phone": "233241234567",
            "customer_display_name": "Ama",
        }
        row = _Row(42, "merchant:233241234567", payload)
        cached = ConversationState(
            user_id="merchant:233241234567",
            conversation_id="old-chat",
            conversation_lifecycle="active",
            intervention_active=True,
            intervention_id=9,
            session_db_id=42,
            conversation_history=list(payload["conversation_history"]),
            customer_phone="233241234567",
            customer_display_name="Ama",
        )
        return row, cached

    def test_next_message_starts_a_new_session_instead_of_reopening_live_chat(self):
        row, cached = self._closed_chat()
        cm = _manager([row])
        cm.memory_cache[cached.user_id] = cached
        inserted = []

        def refresh(new_row):
            new_row.id = 100

        cm.db.refresh.side_effect = refresh
        cm.db.add.side_effect = inserted.append

        self.assertFalse(cm.sync_intervention_from_db(cached.user_id))
        cm.update_conversation_history(cached.user_id, "user", "I have a new question")

        self.assertEqual(len(inserted), 1)
        saved = inserted[0].conversation_state
        self.assertEqual(
            [item["content"] for item in saved["conversation_history"]],
            ["I have a new question"],
        )
        self.assertFalse(saved["intervention_active"])
        self.assertNotEqual(saved["conversation_lifecycle"], "completed")
        self.assertNotEqual(saved["conversation_id"], "old-chat")
        self.assertEqual(saved["customer_phone"], "233241234567")
        self.assertEqual(row.conversation_state["conversation_lifecycle"], "completed")
        self.assertEqual(len(row.conversation_state["conversation_history"]), 2)
        cm.db.commit.assert_called()

    def test_stale_cache_cannot_overwrite_a_completed_session(self):
        row, cached = self._closed_chat()
        cm = _manager([row])
        cm.memory_cache[cached.user_id] = cached

        cm._save_conversation_state(cached)

        self.assertEqual(row.conversation_state["conversation_lifecycle"], "completed")
        self.assertFalse(row.conversation_state["intervention_active"])
        cm.db.commit.assert_not_called()
        self.assertIsNone(cached.session_db_id)

    def test_open_intervention_is_still_the_same_session(self):
        payload = {
            "conversation_id": "live-chat",
            "conversation_lifecycle": "active",
            "intervention_active": True,
            "intervention_id": 3,
            "conversation_history": [{"role": "user", "content": "still here"}],
        }
        row = _Row(7, "merchant:233241234567", payload)
        cached = ConversationState.from_dict(
            {
                **payload,
                "user_id": "merchant:233241234567",
                "session_db_id": 7,
            }
        )
        cm = _manager([row])
        cm.memory_cache[cached.user_id] = cached

        state = cm.get_conversation_state(cached.user_id)

        self.assertIs(state, cached)
        self.assertTrue(cm.sync_intervention_from_db(cached.user_id))
        self.assertEqual(state.conversation_id, "live-chat")
        self.assertTrue(state.intervention_active)


if __name__ == "__main__":
    unittest.main()
