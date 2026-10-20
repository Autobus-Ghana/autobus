"""Unit tests for Instagram media fit / encode / Postiz payload rewrite."""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

from core.socialmedia.service.instagram_media_prepare import (
    FEED_MAX_RATIO,
    FEED_MIN_RATIO,
    InstagramMediaPrepareError,
    InstagramMediaPrepareService,
    STORY_RATIO,
    encode_jpeg,
    fit_image_for_instagram,
    image_bytes_to_instagram_jpeg,
    is_prepared_public_url,
    parse_http_byte_range,
    select_instagram_media_urls,
)


def _png_bytes(width: int, height: int, color=(10, 80, 200, 255)) -> bytes:
    im = Image.new("RGBA", (width, height), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class FakeStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def upload_file(self, file_obj, file_name, content_type=None, **kwargs):
        file_obj.seek(0)
        self.files[file_name] = file_obj.read()
        return file_name


class FitImageTests(unittest.TestCase):
    def test_feed_crops_too_tall_to_4_by_5(self):
        im = Image.new("RGB", (400, 2000), "red")
        out = fit_image_for_instagram(im, crop="feed")
        ratio = out.size[0] / out.size[1]
        self.assertGreaterEqual(ratio, FEED_MIN_RATIO - 0.01)
        self.assertLessEqual(ratio, FEED_MAX_RATIO + 0.01)
        self.assertLessEqual(out.size[0], 1080)

    def test_feed_crops_too_wide_to_1_91(self):
        im = Image.new("RGB", (4000, 400), "red")
        out = fit_image_for_instagram(im, crop="feed")
        ratio = out.size[0] / out.size[1]
        self.assertGreaterEqual(ratio, FEED_MIN_RATIO - 0.01)
        self.assertLessEqual(ratio, FEED_MAX_RATIO + 0.01)

    def test_feed_keeps_square(self):
        im = Image.new("RGB", (1024, 1024), "red")
        out = fit_image_for_instagram(im, crop="feed")
        self.assertEqual(out.size[0], out.size[1])
        self.assertLessEqual(out.size[0], 1080)

    def test_story_is_9_by_16(self):
        im = Image.new("RGB", (1200, 800), "red")
        out = fit_image_for_instagram(im, crop="story")
        ratio = out.size[0] / out.size[1]
        self.assertAlmostEqual(ratio, STORY_RATIO, places=2)


class EncodeTests(unittest.TestCase):
    def test_png_with_alpha_becomes_small_jpeg(self):
        jpeg = image_bytes_to_instagram_jpeg(_png_bytes(1600, 2400), crop="feed")
        self.assertLess(len(jpeg), 7 * 1024 * 1024)
        im = Image.open(io.BytesIO(jpeg))
        self.assertEqual(im.format, "JPEG")
        self.assertEqual(im.mode, "RGB")
        ratio = im.size[0] / im.size[1]
        self.assertGreaterEqual(ratio, FEED_MIN_RATIO - 0.02)

    def test_encode_jpeg_respects_max_bytes(self):
        im = Image.new("RGB", (1080, 1080), (12, 34, 56))
        data = encode_jpeg(im, max_bytes=80_000)
        self.assertLessEqual(len(data), 80_000)


class SelectMediaTests(unittest.TestCase):
    def test_mixed_feed_keeps_images(self):
        urls = [
            "https://cdn.example/a.jpg",
            "https://cdn.example/b.mp4",
            "https://cdn.example/c.png",
        ]
        selected = select_instagram_media_urls(urls, is_story=False)
        self.assertEqual(selected, ["https://cdn.example/a.jpg", "https://cdn.example/c.png"])

    def test_story_takes_first_video(self):
        urls = ["https://cdn.example/a.jpg", "https://cdn.example/b.mp4"]
        selected = select_instagram_media_urls(urls, is_story=True)
        self.assertEqual(selected, ["https://cdn.example/b.mp4"])

    def test_single_video_without_images(self):
        urls = ["https://cdn.example/clip.mp4", "https://cdn.example/other.mov"]
        selected = select_instagram_media_urls(urls, is_story=False)
        self.assertEqual(selected, ["https://cdn.example/clip.mp4"])


class PublicUrlTests(unittest.TestCase):
    def test_prepared_url_without_query(self):
        self.assertTrue(
            is_prepared_public_url(
                "https://api.useautobus.com/api/v1/social/media/public/abc.jpg"
            )
        )
        self.assertFalse(
            is_prepared_public_url(
                "https://usc1.contabostorage.com/x?X-Amz-Algorithm=AWS4&X-Amz-Signature=1"
            )
        )


class PostizPayloadTests(unittest.TestCase):
    def test_instagram_paths_rewritten_to_public_jpg(self):
        png = _png_bytes(900, 900)
        storage = FakeStorage()
        service = InstagramMediaPrepareService(storage=storage)
        payload = {
            "posts": [
                {
                    "integration": {"id": "ig-1"},
                    "settings": {"__type": "instagram", "post_type": "post"},
                    "value": [
                        {
                            "content": "hello",
                            "image": [
                                {"id": "media_0", "path": "https://files.example/raw.png"}
                            ],
                        }
                    ],
                }
            ]
        }
        with patch(
            "core.socialmedia.service.instagram_media_prepare._download",
            return_value=(png, "image/png"),
        ):
            with patch.dict(os.environ, {"AUTOBUS_PUBLIC_API_URL": "https://api.useautobus.com"}):
                out = service.prepare_postiz_payload(payload)

        path = out["posts"][0]["value"][0]["image"][0]["path"]
        self.assertTrue(path.startswith("https://api.useautobus.com/api/v1/social/media/public/"))
        self.assertTrue(path.endswith(".jpg"))
        self.assertNotIn("&", path)
        self.assertTrue(storage.files)
        self.assertTrue(next(iter(storage.files.values()))[:3] == b"\xff\xd8\xff")

    def test_tiktok_photo_is_rejected(self):
        png = _png_bytes(800, 800)
        service = InstagramMediaPrepareService(storage=FakeStorage())
        payload = {
            "posts": [
                {
                    "integration": {"id": "tt-1"},
                    "settings": {"__type": "tiktok"},
                    "value": [
                        {
                            "content": "hello",
                            "image": [{"id": "media_0", "path": "https://files.example/photo.png"}],
                        }
                    ],
                }
            ]
        }
        with patch(
            "core.socialmedia.service.instagram_media_prepare._download",
            return_value=(png, "image/png"),
        ):
            with self.assertRaises(InstagramMediaPrepareError) as caught:
                service.prepare_postiz_payload(payload)
        self.assertIn("video", str(caught.exception).lower())

    def test_tiktok_mov_is_rehosted_as_mp4(self):
        mov = b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 32
        storage = FakeStorage()
        service = InstagramMediaPrepareService(storage=storage)
        payload = {
            "posts": [
                {
                    "integration": {"id": "tt-1"},
                    "settings": {"__type": "tiktok"},
                    "value": [
                        {
                            "content": "hello",
                            "image": [{"id": "media_0", "path": "https://files.example/clip.MOV?X-Amz-Signature=1"}],
                        }
                    ],
                }
            ]
        }
        with patch(
            "core.socialmedia.service.instagram_media_prepare._download",
            return_value=(mov, "video/quicktime"),
        ):
            with patch.dict(os.environ, {"AUTOBUS_PUBLIC_API_URL": "https://api.useautobus.com"}):
                out = service.prepare_postiz_payload(payload)
        path = out["posts"][0]["value"][0]["image"][0]["path"]
        self.assertTrue(path.endswith(".mp4"))
        self.assertNotIn("?", path)

    def test_local_blob_url_is_rejected(self):
        service = InstagramMediaPrepareService(storage=FakeStorage())
        payload = {
            "posts": [
                {
                    "settings": {"__type": "instagram"},
                    "value": [
                        {
                            "content": "hello",
                            "image": [{"id": "media_0", "path": "blob:http://localhost:5173/abc"}],
                        }
                    ],
                }
            ]
        }
        with self.assertRaises(InstagramMediaPrepareError) as caught:
            service.prepare_postiz_payload(payload)
        self.assertIn("device", str(caught.exception).lower())


class ParseHttpByteRangeTest(unittest.TestCase):
    def test_inclusive_range(self):
        self.assertEqual(parse_http_byte_range("bytes=0-999", 5000), (0, 999))

    def test_open_ended_range(self):
        self.assertEqual(parse_http_byte_range("bytes=100-", 250), (100, 249))

    def test_suffix_range(self):
        self.assertEqual(parse_http_byte_range("bytes=-50", 200), (150, 199))

    def test_clamps_to_file_end(self):
        self.assertEqual(parse_http_byte_range("bytes=0-9999", 100), (0, 99))

    def test_missing_or_invalid(self):
        self.assertIsNone(parse_http_byte_range("", 100))
        self.assertIsNone(parse_http_byte_range("bytes=20-10", 100))
        self.assertIsNone(parse_http_byte_range("bytes=100-200", 100))


if __name__ == "__main__":
    unittest.main()
