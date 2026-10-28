"""Human-readable customer identity for owner-facing chat lists.

Conversations stay keyed by channel unique IDs (IGSID, WhatsApp wa_id) so
replies can be delivered. Owners should see username / phone instead.
"""

from __future__ import annotations

from typing import Optional, Tuple

SOCIAL_CHANNELS = ("ig", "fb", "tt", "tw", "rd", "bs", "tg", "sl", "wa")

SOCIAL_CHANNEL_LABELS = {
    "ig": "Instagram",
    "fb": "Facebook",
    "tt": "TikTok",
    "tw": "X",
    "rd": "Reddit",
    "bs": "Bluesky",
    "tg": "Telegram",
    "sl": "Slack",
    "wa": "WhatsApp",
}


def parse_conversation_user_id(
    conversation_user_id: Optional[str],
) -> Tuple[Optional[str], Optional[str], str]:
    """Split ``{merchant_id}:{channel}:{key}`` or ``{merchant_id}:{phone}``.

    Returns ``(merchant_id, channel, customer_key)``.
    ``channel`` is a Zernio inbox code (``ig``, ``fb``, ``tt``, ``tw``, and the rest).
    """
    uid = (conversation_user_id or "").strip()
    if not uid:
        return None, None, ""
    if ":" not in uid:
        return None, None, uid

    merchant_id, _, rest = uid.partition(":")
    merchant_id = merchant_id.strip()
    rest = (rest or "").strip()
    if not merchant_id or not rest:
        return None, None, uid

    lowered = rest.lower()
    for channel in SOCIAL_CHANNELS:
        prefix = f"{channel}:"
        if lowered.startswith(prefix):
            return merchant_id, channel, rest[len(prefix):].strip()
    return merchant_id, None, rest


def customer_channel_key(conversation_user_id: Optional[str]) -> str:
    _, _, key = parse_conversation_user_id(conversation_user_id)
    return key


def is_instagram_conversation(conversation_user_id: Optional[str]) -> bool:
    _, channel, _ = parse_conversation_user_id(conversation_user_id)
    return channel == "ig"


def social_channel(conversation_user_id: Optional[str]) -> Optional[str]:
    _, channel, _ = parse_conversation_user_id(conversation_user_id)
    if channel in SOCIAL_CHANNELS:
        return channel
    return None


def looks_like_phone(value: Optional[str]) -> bool:
    """True for typical phone numbers; false for social-platform unique IDs."""
    raw = (value or "").strip()
    lowered = raw.lower()
    if not raw or any(lowered.startswith(f"{channel}:") for channel in SOCIAL_CHANNELS):
        return False
    if ":" in raw:
        return False
    digits = "".join(ch for ch in raw if ch.isdigit())
    # E.164 is at most 15 digits. Instagram IGSIDs are typically 16–17+.
    return 9 <= len(digits) <= 15


def normalize_username(value: Optional[str]) -> Optional[str]:
    cleaned = (value or "").strip().lstrip("@")
    return cleaned or None


def format_customer_label(
    *,
    username: Optional[str] = None,
    phone: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Optional[str]:
    """Owner-facing label: ``@username · phone`` when those values exist."""
    parts = []
    uname = normalize_username(username)
    if uname:
        parts.append(f"@{uname}")
    phone_clean = (phone or "").strip()
    if phone_clean and looks_like_phone(phone_clean):
        parts.append(phone_clean)
    if parts:
        return " · ".join(parts)
    name = (display_name or "").strip()
    return name or None
