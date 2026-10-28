"""Mapping tests for the Zernio inbox and posting switch."""

from __future__ import annotations

import unittest

from core.socialmedia.service.zernio_mapping import (
    build_create_post_body,
    creator_info_for_ui,
    inbox_message_parts,
    publish_status_for_ui,
    verify_webhook_signature,
)


class ZernioMappingTests(unittest.TestCase):
    def test_post_body_maps_tiktok_and_instagram(self):
        body = build_create_post_body(
            [
                (
                    "tiktok",
                    "acct_tt",
                    {
                        "value": [{"content": "Hello", "image": [{"path": "https://cdn.example.com/clip.mp4"}]}],
                        "settings": {
                            "__type": "tiktok",
                            "title": "Short title",
                            "privacy_level": "SELF_ONLY",
                            "comment": True,
                            "duet": False,
                            "stitch": False,
                            "content_preview_confirmed": False,
                        },
                    },
                ),
                (
                    "instagram",
                    "acct_ig",
                    {
                        "value": [{"content": "Feed caption", "image": [{"path": "https://cdn.example.com/photo.jpg"}]}],
                        "settings": {"__type": "instagram", "post_type": "story", "collaborators": ["@shop"]},
                    },
                ),
            ],
            publish_now=True,
        )
        self.assertTrue(body["publishNow"])
        self.assertEqual(body["platforms"][0]["platform"], "tiktok")
        self.assertEqual(body["platforms"][0]["customContent"], "Short title")
        settings = body["platforms"][0]["platformSpecificData"]["tiktokSettings"]
        self.assertEqual(settings["privacy_level"], "SELF_ONLY")
        self.assertTrue(settings["content_preview_confirmed"])
        self.assertTrue(settings["express_consent_given"])
        self.assertTrue(settings["allow_comment"])
        self.assertEqual(body["platforms"][1]["platformSpecificData"]["contentType"], "story")
        self.assertEqual(body["platforms"][1]["platformSpecificData"]["collaborators"], ["shop"])
        self.assertEqual(body["platforms"][0]["customMedia"][0]["type"], "video")
        self.assertEqual(body["platforms"][1]["customMedia"][0]["type"], "image")

    def test_creator_info_matches_composer_shape(self):
        info = creator_info_for_ui(
            {
                "creator": {"nickname": "Shop", "username": "shop", "canPostMore": True},
                "privacyLevels": [{"value": "PUBLIC_TO_EVERYONE"}, {"value": "SELF_ONLY"}],
                "postingLimits": {
                    "maxVideoDurationSec": 600,
                    "interactionSettings": {
                        "allow_comment": {"enabled": False},
                        "allow_duet": {"enabled": True},
                        "allow_stitch": {"enabled": False},
                    },
                },
            }
        )
        self.assertEqual(info["creator_nickname"], "Shop")
        self.assertEqual(info["privacy_level_options"], ["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
        self.assertTrue(info["comment_disabled"])
        self.assertFalse(info["duet_disabled"])
        self.assertTrue(info["stitch_disabled"])
        self.assertTrue(info["can_post"])
        self.assertEqual(info["max_video_post_duration_sec"], 600)

    def test_inbox_event_uses_external_id(self):
        parts = inbox_message_parts(
            {
                "event": "message.received",
                "profiles": [{"id": "profile", "externalId": "user_1"}],
                "message": {
                    "conversationId": "conv_1",
                    "platform": "instagram",
                    "direction": "incoming",
                    "text": "Is this still available?",
                    "sender": {"id": "igsid", "username": "jane", "name": "Jane"},
                    "attachments": [{"type": "image", "url": "https://cdn.example.com/a.jpg"}],
                },
                "conversation": {"id": "conv_1"},
                "account": {"accountId": "acct_ig", "platform": "instagram"},
            }
        )
        self.assertEqual(parts["external_id"], "user_1")
        self.assertEqual(parts["channel"], "ig")
        self.assertEqual(parts["conversation_id"], "conv_1")
        self.assertEqual(parts["sender_id"], "igsid")
        self.assertEqual(parts["image_url"], "https://cdn.example.com/a.jpg")
        self.assertTrue(parts["incoming"])

    def test_publish_status(self):
        status = publish_status_for_ui(
            {"post": {"status": "published", "platforms": [{"platform": "tiktok", "status": "published"}]}}
        )
        self.assertTrue(status["complete"])
        self.assertFalse(status["failed"])

    def test_webhook_signature(self):
        body = b'{"event":"message.received"}'
        secret = "test-secret"
        import hashlib
        import hmac

        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertTrue(verify_webhook_signature(body, f"sha256={signature}", secret))
        self.assertFalse(verify_webhook_signature(body, "nope", secret))


if __name__ == "__main__":
    unittest.main()
