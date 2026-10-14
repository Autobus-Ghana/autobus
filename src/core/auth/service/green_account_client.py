"""Publish and authenticate people against the shared Green account service."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

PRODUCT = "autobus"


def _settings() -> Optional[tuple[str, str]]:
    base = (os.getenv("GREEN_ACCOUNT_URL") or "").strip().rstrip("/")
    key = (os.getenv("GREEN_ACCOUNT_API_KEY") or "").strip()
    if not base or not key:
        return None
    return base, key


def _headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Content-Type": "application/json"}


def email_exists(email: str) -> Optional[bool]:
    """Whether this email is already a Green account.

    None means the directory could not be checked, so signup can continue.
    """
    settings = _settings()
    if settings is None or "@" not in (email or ""):
        return None
    base, key = settings
    try:
        response = requests.post(
            f"{base}/v1/accounts/lookup",
            json={"email": email.strip()},
            headers=_headers(key),
            timeout=5,
        )
    except requests.RequestException as exc:
        logger.warning("Green account lookup failed: %s", exc)
        return None
    if response.status_code != 200:
        logger.warning("Green account lookup failed: %s", response.status_code)
        return None
    return bool(response.json().get("exists"))


def authenticate(email: str, password: str) -> Optional[dict[str, Any]]:
    """Return account details when this email and password match a Green account."""
    settings = _settings()
    if settings is None or "@" not in (email or ""):
        return None
    base, key = settings
    try:
        response = requests.post(
            f"{base}/v1/accounts/authenticate",
            json={"email": email.strip(), "password": password},
            headers=_headers(key),
            timeout=5,
        )
    except requests.RequestException as exc:
        logger.warning("Green account authenticate failed: %s", exc)
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    details = _details(body, preferred_email=email)
    if not details.get("email"):
        return None
    return details


def publish_user(user: Any) -> None:
    """Link this Autobus user to their Green account. Failures stay on this app."""
    settings = _settings()
    email = (getattr(user, "email", None) or "").strip()
    password_hash = (getattr(user, "hashed_password", None) or "").strip()
    user_id = (getattr(user, "id", None) or "").strip()
    if settings is None or not email or not user_id or not password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        return
    base, key = settings
    identifiers = [{"type": "email", "value": email, "verified": bool(getattr(user, "enabled", False))}]
    phone = (getattr(user, "phone", None) or "").strip()
    if phone:
        identifiers.append({"type": "phone", "value": phone, "verified": False})
    ghana_card = (getattr(user, "ghana_card", None) or "").strip()
    if ghana_card:
        identifiers.append({"type": "ghana_card", "value": ghana_card, "verified": False})
    payload = {
        "product": PRODUCT,
        "local_user_id": user_id,
        "name": (getattr(user, "fullname", None) or email).strip(),
        "password_hash": password_hash,
        "identifiers": identifiers,
    }
    try:
        response = requests.post(
            f"{base}/v1/accounts/provision",
            json=payload,
            headers=_headers(key),
            timeout=5,
        )
        if response.status_code >= 400:
            logger.warning(
                "Green account provision failed for %s: %s %s",
                user_id,
                response.status_code,
                response.text[:300],
            )
    except requests.RequestException as exc:
        logger.warning("Green account provision failed for %s: %s", user_id, exc)


def _details(body: dict[str, Any], preferred_email: str = "") -> dict[str, Any]:
    """One Green account can hold several emails; keep the one the person signed in with."""
    preferred = (preferred_email or "").strip().lower()
    emails: list[str] = []
    phone = ""
    ghana_card = ""
    for item in body.get("identifiers") or []:
        kind = (item.get("type") or "").strip().lower()
        value = (item.get("value") or "").strip()
        if kind == "email" and value:
            emails.append(value)
        elif kind == "phone" and value and not phone:
            phone = value
        elif kind == "ghana_card" and value and not ghana_card:
            ghana_card = value
    email = next((e for e in emails if e.lower() == preferred), emails[0] if emails else "")
    return {
        "green_account_id": body.get("green_account_id"),
        "display_name": body.get("display_name"),
        "email": email,
        "phone": phone,
        "ghana_card": ghana_card,
    }
