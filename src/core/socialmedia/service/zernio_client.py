"""HTTP client for the Zernio REST API."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from core.socialmedia.service.zernio_mapping import should_retry_zernio

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://zernio.com/api/v1"


class ZernioAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        code: str = "",
        body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body = body or {}


class ZernioClient:
    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = (api_key or os.getenv("ZERNIO_API_KEY") or "").strip()
        self.base_url = (
            base_url or os.getenv("ZERNIO_API_BASE") or DEFAULT_BASE
        ).strip().rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 60,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise ZernioAPIError("ZERNIO_API_KEY is not configured", status_code=400)
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        request_headers = self._headers(headers)
        has_idempotency = bool((request_headers.get("Idempotency-Key") or "").strip())
        attempt = 0
        while True:
            try:
                response = httpx.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                if should_retry_zernio(
                    method=method,
                    status_code=None,
                    has_idempotency_key=has_idempotency,
                    attempt=attempt,
                ):
                    attempt += 1
                    logger.warning("[ZERNIO] %s %s failed (%s); retrying", method, path, exc)
                    time.sleep(0.4 * attempt)
                    continue
                raise ZernioAPIError(f"Zernio request failed: {exc}", status_code=502) from exc

            try:
                body = response.json()
            except Exception:
                body = {"message": (response.text or "")[:500]}
            if not isinstance(body, dict):
                body = {"value": body}

            if response.status_code < 400:
                return body
            if should_retry_zernio(
                method=method,
                status_code=response.status_code,
                has_idempotency_key=has_idempotency,
                attempt=attempt,
            ):
                attempt += 1
                logger.warning(
                    "[ZERNIO] %s %s returned %s; retrying",
                    method,
                    path,
                    response.status_code,
                )
                time.sleep(0.4 * attempt)
                continue
            message = str(body.get("error") or body.get("message") or response.text[:400])
            code = str(body.get("code") or "")
            raise ZernioAPIError(
                message,
                status_code=response.status_code,
                code=code,
                body=body,
            )

    def create_profile(self, *, name: str, external_id: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/profiles",
            json_body={"name": name, "externalId": external_id},
        )

    def find_profile(self, external_id: str) -> Optional[Dict[str, Any]]:
        body = self.request("GET", "/profiles", params={"externalId": external_id})
        profiles = body.get("profiles") or body.get("data") or []
        if isinstance(body.get("profile"), dict):
            return body["profile"]
        if isinstance(profiles, list):
            for item in profiles:
                if isinstance(item, dict) and str(item.get("externalId") or "") == external_id:
                    return item
            if len(profiles) == 1 and isinstance(profiles[0], dict):
                return profiles[0]
        return None

    def connect_url(
        self,
        platform: str,
        *,
        profile_id: str,
        redirect_url: str,
        scopes: str = "",
    ) -> str:
        params: Dict[str, Any] = {
            "profileId": profile_id,
            "redirect_url": redirect_url,
        }
        if scopes:
            params["scopes"] = scopes
        body = self.request(
            "GET",
            f"/connect/{platform}",
            params=params,
        )
        url = str(body.get("authUrl") or body.get("authorization_url") or "").strip()
        if not url:
            raise ZernioAPIError("Zernio did not return a connect URL", status_code=502)
        return url

    def list_accounts(self, profile_id: str) -> list:
        body = self.request("GET", "/accounts", params={"profileId": profile_id})
        for key in ("accounts", "data", "items", "value"):
            accounts = body.get(key)
            if isinstance(accounts, list):
                return accounts
        return []

    def delete_account(self, account_id: str) -> Dict[str, Any]:
        return self.request("DELETE", f"/accounts/{account_id}")

    def create_post(
        self,
        body: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/posts",
            json_body=body,
            headers={"Idempotency-Key": idempotency_key},
            timeout=120,
        )

    def get_post(self, post_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/posts/{post_id}")

    def tiktok_creator_info(self, account_id: str, *, media_type: str = "video") -> Dict[str, Any]:
        return self.request(
            "GET",
            f"/accounts/{account_id}/tiktok/creator-info",
            params={"mediaType": media_type},
        )

    def send_inbox_message(
        self,
        conversation_id: str,
        *,
        account_id: str,
        message: str = "",
        attachment_url: str = "",
        attachment_type: str = "image",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"accountId": account_id}
        if message:
            body["message"] = message
        if attachment_url:
            body["attachmentUrl"] = attachment_url
            body["attachmentType"] = attachment_type
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self.request(
            "POST",
            f"/inbox/conversations/{conversation_id}/messages",
            json_body=body,
            headers=headers,
            timeout=45,
        )

    def list_webhooks(self) -> list:
        body = self.request("GET", "/webhooks/settings")
        hooks = body.get("webhooks") or body.get("data") or []
        return hooks if isinstance(hooks, list) else []

    def create_webhook(self, *, name: str, url: str, secret: str, events: list) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/webhooks/settings",
            json_body={"name": name, "url": url, "secret": secret, "events": events},
        )
