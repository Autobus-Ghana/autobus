"""Unit tests for TikTok Direct Post creator_info normalization."""

from __future__ import annotations

import unittest

from core.socialmedia.service.tiktok_direct_post import (
    CONSENT_BRANDED,
    CONSENT_MUSIC,
    RECONNECT_TIKTOK_MESSAGE,
    _posts_for_integration,
    _usable_creator_info,
    extract_publish_ids,
    is_tiktok_auth_failure,
    normalize_creator_info,
    normalize_publish_status,
    tiktok_consent_text,
    user_facing_publish_error,
)


class NormalizeCreatorInfoTest(unittest.TestCase):
    def test_tiktok_ok_payload(self):
        info = normalize_creator_info(
            {
                "data": {
                    "creator_nickname": "Shop TV",
                    "creator_username": "shoptv",
                    "creator_avatar_url": "https://example.com/a.png",
                    "privacy_level_options": [
                        "SELF_ONLY",
                        "MUTUAL_FOLLOW_FRIENDS",
                        "FOLLOWER_OF_CREATOR",
                    ],
                    "comment_disabled": False,
                    "duet_disabled": True,
                    "stitch_disabled": False,
                    "max_video_post_duration_sec": 180,
                },
                "error": {"code": "ok", "message": ""},
            }
        )
        self.assertTrue(info["can_post"])
        self.assertEqual(info["creator_nickname"], "Shop TV")
        self.assertEqual(info["privacy_level_options"][0], "SELF_ONLY")
        self.assertTrue(info["duet_disabled"])
        self.assertEqual(info["max_video_post_duration_sec"], 180)
        self.assertEqual(info["privacy_level_labels"]["SELF_ONLY"], "Only me")

    def test_too_many_posts_blocks_publish(self):
        info = normalize_creator_info(
            {
                "data": {},
                "error": {
                    "code": "spam_risk_too_many_posts",
                    "message": "cap",
                },
            }
        )
        self.assertFalse(info["can_post"])
        self.assertIn("Try again later", info["cannot_post_reason"])

    def test_can_post_false(self):
        info = normalize_creator_info({"can_post": False, "nickname": "A"})
        self.assertFalse(info["can_post"])
        self.assertEqual(info["creator_nickname"], "A")

    def test_fallback_name(self):
        info = normalize_creator_info({}, fallback_name="Linked TikTok")
        self.assertEqual(info["creator_nickname"], "Linked TikTok")

    def test_usable_payload_fills_missing_handle(self):
        info = _usable_creator_info(
            {
                "data": {
                    "creator_nickname": "Shop TV",
                    "privacy_level_options": ["SELF_ONLY"],
                },
                "error": {"code": "ok"},
            },
            fallback_username="shoptv",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["creator_username"], "shoptv")

    def test_empty_payload_is_not_usable(self):
        self.assertIsNone(_usable_creator_info({}))

    def test_invalid_access_token_is_not_creator_info(self):
        payload = {
            "error": {
                "code": "access_token_invalid",
                "message": "The access token is invalid or not found in the request.",
                "log_id": "abc",
            }
        }
        self.assertTrue(is_tiktok_auth_failure(payload))
        self.assertIsNone(_usable_creator_info(payload, fallback_name="Shop"))
        info = normalize_creator_info(payload)
        self.assertFalse(info["can_post"])
        self.assertEqual(info["cannot_post_reason"], RECONNECT_TIKTOK_MESSAGE)


class ConsentAndStatusTest(unittest.TestCase):
    def test_consent_switches_for_branded(self):
        self.assertEqual(tiktok_consent_text(branded_content=False), CONSENT_MUSIC)
        self.assertEqual(tiktok_consent_text(branded_content=True), CONSENT_BRANDED)

    def test_unaudited_error_is_explained(self):
        text = user_facing_publish_error(
            "ApplicationFailure: App not approved for public posting, contact support"
        )
        self.assertIn("Private", text)
        self.assertIn("audit", text.lower())

    def test_publish_complete(self):
        status = normalize_publish_status(
            {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["1"]}}
        )
        self.assertTrue(status["complete"])
        self.assertFalse(status["processing"])
        self.assertEqual(status["public_post_ids"], ["1"])

    def test_extract_publish_id(self):
        ids = extract_publish_ids(
            {"posts": [{"settings": {}, "publish_id": "v_pub_url~v2.123"}]}
        )
        self.assertEqual(ids, ["v_pub_url~v2.123"])

    def test_postiz_error_uses_error_message(self):
        status = normalize_publish_status(
            {
                "state": "ERROR",
                "error": {
                    "message": "The media storage did not return the requested byte range, please try again"
                },
                "integration": {"id": "tt", "identifier": "tiktok"},
            }
        )
        self.assertTrue(status["failed"])
        self.assertIn("byte range", status["message"])

    def test_newest_postiz_post_is_first(self):
        matched = _posts_for_integration(
            [
                {
                    "id": "old",
                    "state": "ERROR",
                    "publishDate": "2026-09-12T00:00:00.000Z",
                    "integration": {"id": "tt", "identifier": "tiktok"},
                },
                {
                    "id": "new",
                    "state": "QUEUE",
                    "publishDate": "2026-09-13T07:00:00.000Z",
                    "integration": {"id": "tt", "identifier": "tiktok"},
                },
            ],
            "tt",
        )
        self.assertEqual(matched[0]["id"], "new")
        self.assertEqual(matched[0]["state"], "QUEUE")


if __name__ == "__main__":
    unittest.main()
