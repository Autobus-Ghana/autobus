"""Mapping tests for the Zernio inbox and posting switch."""

from __future__ import annotations

import unittest

from core.socialmedia.service.zernio_mapping import (
    account_connected_parts,
    build_create_post_body,
    connect_scopes,
    creator_info_for_ui,
    inbox_message_parts,
    publish_status_for_ui,
    remote_account_active,
    should_retry_zernio,
    verify_webhook_signature,
    zernio_id,
    ZERNIO_PLATFORMS,
)


class ZernioMappingTests(unittest.TestCase):
    def test_post_retries_only_with_idempotency_key(self):
        self.assertTrue(
            should_retry_zernio(
                method="POST",
                status_code=None,
                has_idempotency_key=True,
                attempt=0,
            )
        )
        self.assertFalse(
            should_retry_zernio(
                method="POST",
                status_code=502,
                has_idempotency_key=False,
                attempt=0,
            )
        )
        self.assertTrue(
            should_retry_zernio(
                method="GET",
                status_code=503,
                has_idempotency_key=False,
                attempt=1,
            )
        )
        self.assertFalse(
            should_retry_zernio(
                method="GET",
                status_code=400,
                has_idempotency_key=False,
                attempt=0,
            )
        )

    def test_zernio_id_reads_nested_profile(self):
        self.assertEqual(zernio_id("6ae174e91584588f2c803827"), "6ae174e91584588f2c803827")
        self.assertEqual(
            zernio_id({"_id": "6ae174e91584588f2c803827", "name": "Shop", "externalId": "user"}),
            "6ae174e91584588f2c803827",
        )
        self.assertEqual(zernio_id(None), "")
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
        self.assertEqual(
            info["privacy_level_options"][:2],
            ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        )
        self.assertEqual(
            set(info["privacy_level_options"]),
            {
                "PUBLIC_TO_EVERYONE",
                "SELF_ONLY",
                "MUTUAL_FOLLOW_FRIENDS",
                "FOLLOWER_OF_CREATOR",
            },
        )
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

    def test_whatsapp_inbox_event_maps_to_wa_channel(self):
        parts = inbox_message_parts(
            {
                "event": "message.received",
                "profiles": [{"id": "profile", "externalId": "user_1"}],
                "message": {
                    "conversationId": "conv_wa",
                    "platform": "whatsapp",
                    "direction": "incoming",
                    "text": {"body": "Hi, do you deliver?"},
                    "sender": {"id": "233241234567", "name": "Kofi"},
                },
                "account": {"accountId": "acct_wa", "platform": "whatsapp"},
            }
        )
        self.assertEqual(parts["channel"], "wa")
        self.assertEqual(parts["sender_id"], "233241234567")
        self.assertEqual(parts["text"], "Hi, do you deliver?")
        self.assertIn("whatsapp", ZERNIO_PLATFORMS)
        self.assertEqual(connect_scopes("whatsapp"), "")

    def test_publish_status(self):
        status = publish_status_for_ui(
            {"post": {"status": "published", "platforms": [{"platform": "tiktok", "status": "published"}]}}
        )
        self.assertTrue(status["complete"])
        self.assertFalse(status["failed"])

    def test_youtube_connect_scopes_omit_messaging(self):
        scopes = connect_scopes("youtube").split(",")
        self.assertIn("posting", scopes)
        self.assertNotIn("messaging", scopes)
        self.assertEqual(connect_scopes("bluesky"), "")

    def test_youtube_account_event_reads_nested_profile(self):
        parts = account_connected_parts(
            {
                "account": {
                    "_id": "acct_yt",
                    "platform": "youtube",
                    "username": "shoptv",
                    "displayName": "Shop TV",
                    "profileId": {
                        "_id": "profile_1",
                        "externalId": "user_1",
                    },
                }
            }
        )
        self.assertEqual(parts["account_id"], "acct_yt")
        self.assertEqual(parts["platform"], "youtube")
        self.assertEqual(parts["profile_id"], "profile_1")
        self.assertEqual(parts["external_id"], "user_1")
        self.assertEqual(parts["display_name"], "Shop TV")

    def test_disabled_side_effect_account_is_not_postable(self):
        self.assertTrue(remote_account_active({"isActive": True, "platform": "youtube"}))
        self.assertFalse(remote_account_active({"isActive": True, "enabled": False}))

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
