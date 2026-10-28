"""Unit tests for owner-facing chat identity helpers."""

from __future__ import annotations

import unittest

from core.conversationmanager.service.customer_identity import (
    customer_channel_key,
    format_customer_label,
    is_instagram_conversation,
    looks_like_phone,
    parse_conversation_user_id,
)


class CustomerIdentityTests(unittest.TestCase):
    def test_parse_instagram_key(self):
        merchant, channel, key = parse_conversation_user_id(
            "user123:ig:17841401234567890"
        )
        self.assertEqual(merchant, "user123")
        self.assertEqual(channel, "ig")
        self.assertEqual(key, "17841401234567890")
        self.assertTrue(is_instagram_conversation("user123:ig:17841401234567890"))
        self.assertEqual(
            customer_channel_key("user123:ig:17841401234567890"),
            "17841401234567890",
        )

    def test_parse_whatsapp_phone_key(self):
        merchant, channel, key = parse_conversation_user_id("user123:233241234567")
        self.assertEqual(merchant, "user123")
        self.assertIsNone(channel)
        self.assertEqual(key, "233241234567")
        self.assertFalse(is_instagram_conversation("user123:233241234567"))

    def test_parse_zernio_whatsapp_key(self):
        merchant, channel, key = parse_conversation_user_id("user123:wa:233241234567")
        self.assertEqual(merchant, "user123")
        self.assertEqual(channel, "wa")
        self.assertEqual(key, "233241234567")
        self.assertTrue(looks_like_phone(key))

    def test_looks_like_phone_rejects_instagram_ids(self):
        self.assertTrue(looks_like_phone("233241234567"))
        self.assertTrue(looks_like_phone("0550748724"))
        self.assertFalse(looks_like_phone("ig:17841401234567890"))
        self.assertFalse(looks_like_phone("fb:17841401234567890"))
        self.assertFalse(looks_like_phone("tt:17841401234567890"))
        self.assertFalse(looks_like_phone("17841401234567890"))
        self.assertFalse(looks_like_phone("user123:ig:17841401234567890"))

    def test_format_customer_label_prefers_username_and_phone(self):
        self.assertEqual(
            format_customer_label(username="jane_doe", phone="0550748724"),
            "@jane_doe · 0550748724",
        )
        self.assertEqual(format_customer_label(username="@jane_doe"), "@jane_doe")
        self.assertEqual(format_customer_label(phone="0550748724"), "0550748724")
        self.assertEqual(format_customer_label(display_name="Jane Doe"), "Jane Doe")
        self.assertIsNone(format_customer_label(phone="ig:17841401234567890"))


if __name__ == "__main__":
    unittest.main()
