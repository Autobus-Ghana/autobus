import unittest

from core.intelligence.service.owner_agent_destinations import (
    match_destinations,
    media_looks_like_video,
    postiz_settings_for,
    title_from_caption,
)


def _dest(dest_id, provider, channel, label=None, account_id=None):
    row = {
        "id": dest_id,
        "provider": provider,
        "channel": channel,
        "label": label or provider,
    }
    if account_id:
        row["account_id"] = account_id
    return row


class OwnerAgentCampaignTest(unittest.TestCase):
    def setUp(self):
        self.dests = [
            _dest("autobus-ig-ig1", "instagram", "autobus_instagram", "Instagram (@shop)", "ig1"),
            _dest("yt1", "youtube", "postiz", "YouTube (Shop TV)"),
            _dest("tt1", "tiktok", "postiz", "TikTok (shop)"),
        ]

    def test_image_skips_youtube_and_tiktok_when_posting_to_all(self):
        dests = self.dests + [_dest("fb1", "facebook", "zernio", "Facebook")]
        matched = match_destinations(dests, has_video=False)
        self.assertEqual([d["id"] for d in matched], ["fb1"])

    def test_video_posts_to_all_linked(self):
        matched = match_destinations(self.dests, has_video=True)
        self.assertEqual([d["id"] for d in matched], ["yt1", "tt1"])

    def test_platforms_filter(self):
        matched = match_destinations(self.dests, platforms=["tiktok", "youtube"], has_video=True)
        self.assertEqual([d["id"] for d in matched], ["yt1", "tt1"])

    def test_account_ids_accept_autobus_ig_prefix(self):
        matched = match_destinations(self.dests, account_ids=["autobus-ig-ig1", "tt1"])
        self.assertEqual([d["id"] for d in matched], ["autobus-ig-ig1", "tt1"])

    def test_instagram_only_drops_postiz(self):
        matched = match_destinations(self.dests, instagram_only=True, has_video=True)
        self.assertEqual([d["id"] for d in matched], ["autobus-ig-ig1"])

    def test_media_looks_like_video(self):
        self.assertTrue(media_looks_like_video(["https://cdn/clip.mp4"]))
        self.assertFalse(media_looks_like_video(["https://cdn/poster.jpg"]))

    def test_title_and_tiktok_settings(self):
        self.assertEqual(title_from_caption("Hello\nWorld"), "Hello")
        settings = postiz_settings_for("tiktok", "Hello")
        self.assertEqual(settings["__type"], "tiktok")
        self.assertEqual(settings["privacy_level"], "SELF_ONLY")


if __name__ == "__main__":
    unittest.main()
