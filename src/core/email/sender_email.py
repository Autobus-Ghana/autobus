"""Normalize and read the outbound From address used by Zeptomail."""

from __future__ import annotations

import os
import re
from typing import Any, Optional

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class SenderEmailNotConfigured(ValueError):
    """Raised when a user tries to send mail without a From address."""


class SenderEmailInvalid(ValueError):
    """Raised when the submitted From address cannot be used."""


def sender_domain() -> str:
    return (os.getenv("ZEPTOMAIL_SENDER_DOMAIN") or "useautobus.com").strip().lower()


def sender_email_from_user(user: Any) -> Optional[str]:
    if user is None:
        return None
    getter = getattr(user, "get_agent", None)
    agent = getter("email_agent") if callable(getter) else None
    if not agent:
        return None
    raw = (agent.get("params") or {}).get("sender_email")
    text = str(raw or "").strip()
    return text or None


def normalize_sender_email(raw: str) -> str:
    """
    Accept a full address or `local.domain` shorthand.

    `noreply@usegreenmall.com` and `noreply.usegreenmall.com` both become
    `noreply@usegreenmall.com`. A name with no domain, such as `noreply`,
    uses the default Autobus domain.
    """
    text = (raw or "").strip()
    if not text:
        raise SenderEmailInvalid(
            "Enter a from email, for example noreply@usegreenmall.com"
        )

    if "@" not in text:
        if "." in text:
            local, _, host = text.partition(".")
            text = f"{local}@{host}"
        else:
            text = f"{text}@{sender_domain()}"

    local, _, host = text.partition("@")
    local = local.strip()
    host = host.strip().lower().rstrip(".")
    if not local or not host or "@" in local or "@" in host:
        raise SenderEmailInvalid(
            "Enter a from email, for example noreply@usegreenmall.com"
        )

    email = f"{local}@{host}"
    if not _EMAIL_RE.match(email):
        raise SenderEmailInvalid(
            "That does not look like a valid from email. Try noreply@usegreenmall.com"
        )
    return email
