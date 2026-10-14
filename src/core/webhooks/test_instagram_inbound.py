import unittest

from core.webhooks.instagram_inbound import ig_image_url, ig_message_text


class InstagramCaptionedPhotoTests(unittest.TestCase):
    def test_caption_and_image_stay_together(self):
        message = {
            "mid": "m1",
            "text": "is this available?",
            "attachments": [
                {
                    "type": "image",
                    "payload": {"url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1"},
                }
            ],
        }
        self.assertEqual(ig_message_text(message), "is this available?")
        self.assertIn("lookaside.fbsbx.com", ig_image_url(message))

    def test_share_attachment_with_caption_is_still_a_photo(self):
        message = {
            "text": "do you have this",
            "attachments": [
                {
                    "type": "share",
                    "payload": {"url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=2"},
                }
            ],
        }
        self.assertEqual(ig_message_text(message), "do you have this")
        self.assertTrue(ig_image_url(message))

    def test_story_reply_uses_the_story_image(self):
        message = {
            "text": "is this in stock",
            "reply_to": {"story": {"url": "https://lookaside.fbsbx.com/story.jpg", "id": "s1"}},
        }
        self.assertEqual(ig_message_text(message), "is this in stock")
        self.assertEqual(ig_image_url(message), "https://lookaside.fbsbx.com/story.jpg")


if __name__ == "__main__":
    unittest.main()
