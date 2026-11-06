"""Unit tests for Postiz OAuth URL rewrites."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from core.socialmedia.service.postiz_api_service import (
    PostizAPIError,
    apply_tiktok_oauth_scopes,
    require_direct_provider_url,
)


class ApplyTiktokOauthScopesTest(unittest.TestCase):
    def test_strips_video_list_and_video_create(self):
        url = (
            "https://www.tiktok.com/v2/auth/authorize/"
            "?client_key=abc"
            "&scope=video.list%2Cuser.info.basic%2Cvideo.publish"
            "%2Cvideo.upload%2Cuser.info.profile%2Cuser.info.stats%2Cvideo.create"
            "&response_type=code"
        )
        rewritten = apply_tiktok_oauth_scopes(url, slug="tiktok")
        query = parse_qs(urlsplit(rewritten).query)
        scopes = query["scope"][0].split(",")
        self.assertEqual(
            scopes,
            [
                "user.info.basic",
                "video.publish",
                "video.upload",
                "user.info.profile",
                "user.info.stats",
            ],
        )
        self.assertEqual(query["disable_auto_auth"], ["1"])

    def test_shows_consent_when_tiktok_would_auto_authorize(self):
        url = (
            "https://www.tiktok.com/v2/auth/authorize/"
            "?client_key=abc&scope=user.info.basic&response_type=code"
        )
        rewritten = apply_tiktok_oauth_scopes(url, slug="tiktok")
        query = parse_qs(urlsplit(rewritten).query)
        self.assertEqual(query["disable_auto_auth"], ["1"])
        self.assertEqual(query["scope"], ["user.info.basic"])

    def test_shows_consent_even_without_scope_query(self):
        url = "https://www.tiktok.com/v2/auth/authorize/?client_key=abc"
        rewritten = apply_tiktok_oauth_scopes(url, slug="tiktok")
        query = parse_qs(urlsplit(rewritten).query)
        self.assertEqual(query["disable_auto_auth"], ["1"])
        self.assertEqual(query["client_key"], ["abc"])

    def test_ignores_non_tiktok_slug(self):
        url = "https://www.tiktok.com/v2/auth/authorize/?scope=video.list,user.info.basic"
        self.assertEqual(apply_tiktok_oauth_scopes(url, slug="facebook"), url)

    def test_ignores_non_tiktok_host(self):
        url = "https://example.com/oauth?scope=video.list,user.info.basic"
        self.assertEqual(apply_tiktok_oauth_scopes(url, slug="tiktok"), url)


class RequireDirectProviderUrlTest(unittest.TestCase):
    def test_accepts_tiktok_and_google(self):
        tiktok = "https://www.tiktok.com/v2/auth/authorize/?client_key=abc"
        google = "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc"
        self.assertEqual(require_direct_provider_url(tiktok, "tiktok"), tiktok)
        self.assertEqual(require_direct_provider_url(google, "youtube"), google)

    def test_rejects_zernio_for_tiktok_and_youtube(self):
        url = "https://zernio.com/connect/youtube"
        for slug in ("tiktok", "youtube"):
            with self.assertRaises(PostizAPIError):
                require_direct_provider_url(url, slug)


if __name__ == "__main__":
    unittest.main()
