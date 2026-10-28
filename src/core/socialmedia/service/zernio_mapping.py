"""Pure helpers for Zernio connect, posts, creator info, and inbox webhooks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# Platforms Zernio can publish to. API values match GET /v1/connect/{platform}.
ZERNIO_PLATFORMS = (
    "twitter",
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube",
    "pinterest",
    "reddit",
    "bluesky",
    "threads",
    "googlebusiness",
    "snapchat",
    "telegram",
    "discord",
    "slack",
)

# Direct-message inboxes. Comment-only platforms stay on the posting list.
ZERNIO_INBOX_PLATFORMS = (
    "instagram",
    "facebook",
    "tiktok",
    "twitter",
    "reddit",
    "bluesky",
    "telegram",
    "slack",
)

# Autobus conversation channel segment for each inbox platform.
CHANNEL_BY_PLATFORM = {
    "instagram": "ig",
    "facebook": "fb",
    "tiktok": "tt",
    "twitter": "tw",
    "reddit": "rd",
    "bluesky": "bs",
    "telegram": "tg",
    "slack": "sl",
}
PLATFORM_BY_CHANNEL = {value: key for key, value in CHANNEL_BY_PLATFORM.items()}

CONNECT_SCOPES = "posting,messaging,comments,analytics"

# YouTube, Threads, LinkedIn, Pinterest, and Google Business have no messaging
# grant. Asking for it makes Zernio reject the connect URL, so the channel never
# lands in the posting list.
_CONNECT_SCOPE_AREAS = {
    "youtube": ("posting", "comments", "analytics"),
    "threads": ("posting", "comments", "analytics"),
    "linkedin": ("posting", "comments", "analytics"),
    "pinterest": ("posting", "analytics"),
    "googlebusiness": ("posting", "comments", "analytics"),
}
# These dialogs cannot be reduced. Sending scopes returns 400.
_SCOPES_UNSUPPORTED = frozenset({"bluesky", "telegram", "discord", "snapchat", "whatsapp"})


def connect_scopes(platform: str) -> str:
    slug = (platform or "").strip().lower()
    if slug in _SCOPES_UNSUPPORTED:
        return ""
    areas = _CONNECT_SCOPE_AREAS.get(slug)
    if areas:
        return ",".join(areas)
    return CONNECT_SCOPES


def remote_account_active(item: Dict[str, Any]) -> bool:
    """Zernio hides posting targets when enabled is false, even if isActive is true."""
    if item.get("isActive") is False:
        return False
    if item.get("enabled") is False:
        return False
    return True


INBOX_WEBHOOK_EVENTS = (
    "message.received",
    "account.connected",
    "account.disconnected",
    "post.published",
    "post.failed",
)


def media_type_for_url(url: str) -> str:
    path = urlparse(url or "").path.lower()
    if path.endswith((".mp4", ".mov", ".m4v", ".webm", ".mkv")):
        return "video"
    if path.endswith(".gif"):
        return "gif"
    return "image"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


RETRYABLE_ZERNIO_STATUS = {408, 429, 502, 503, 504}


def should_retry_zernio(
    *,
    method: str,
    status_code: Optional[int],
    has_idempotency_key: bool,
    attempt: int,
    max_attempts: int = 3,
) -> bool:
    """Retry transport failures and gateway errors. Posts retry only with an idempotency key."""
    if attempt + 1 >= max_attempts:
        return False
    if method.upper() not in {"GET", "HEAD"} and not has_idempotency_key:
        return False
    if status_code is None:
        return True
    return status_code in RETRYABLE_ZERNIO_STATUS


def zernio_id(value: Any) -> str:
    """Zernio sometimes returns an id as a string and sometimes as ``{_id, name}``."""
    if isinstance(value, dict):
        return _first_str(value.get("_id"), value.get("id"), value.get("accountId"))
    return _first_str(value)


def post_content(post: Dict[str, Any]) -> str:
    for block in post.get("value") or []:
        if isinstance(block, dict):
            text = block.get("content")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def post_media_urls(post: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for block in post.get("value") or []:
        if not isinstance(block, dict):
            continue
        images = block.get("image") or []
        if isinstance(images, dict):
            images = [images]
        if not isinstance(images, list):
            continue
        for item in images:
            if isinstance(item, dict):
                path = _first_str(item.get("path"), item.get("url"))
                if path:
                    urls.append(path)
    return urls


def tiktok_settings_from_postiz(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Map the existing TikTok composer settings onto Zernio tiktokSettings."""
    privacy = _first_str(settings.get("privacy_level")) or "SELF_ONLY"
    disclose = bool(settings.get("brand_content_toggle") or settings.get("brand_organic_toggle"))
    body: Dict[str, Any] = {
        "privacy_level": privacy,
        "allow_comment": bool(settings.get("comment")),
        "allow_duet": bool(settings.get("duet")),
        "allow_stitch": bool(settings.get("stitch")),
        "content_preview_confirmed": True,
        "express_consent_given": True,
        "video_made_with_ai": bool(settings.get("video_made_with_ai")),
    }
    if settings.get("brand_organic_toggle"):
        body["is_brand_organic_post"] = True
    if settings.get("brand_content_toggle"):
        body["is_branded_content"] = True
    if disclose and not body.get("is_brand_organic_post") and not body.get("is_branded_content"):
        body["is_brand_organic_post"] = True
    return body


def instagram_specific_data(settings: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if str(settings.get("post_type") or "").strip().lower() == "story":
        data["contentType"] = "story"
    collaborators = settings.get("collaborators")
    if isinstance(collaborators, list):
        names = [str(item).strip().lstrip("@") for item in collaborators if str(item).strip()]
        if names:
            data["collaborators"] = names[:3]
    return data


def platform_entry(
    *,
    platform: str,
    account_id: str,
    settings: Dict[str, Any],
    content: str,
    media_urls: List[str],
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"platform": platform, "accountId": account_id}
    if platform == "tiktok":
        title = _first_str(settings.get("title"))
        if title:
            entry["customContent"] = title
        entry["platformSpecificData"] = {"tiktokSettings": tiktok_settings_from_postiz(settings)}
    elif platform == "instagram":
        specific = instagram_specific_data(settings)
        if specific:
            entry["platformSpecificData"] = specific
    elif platform == "youtube":
        title = _first_str(settings.get("title")) or content
        visibility = _first_str(settings.get("type"), settings.get("visibility")) or "public"
        tags = settings.get("tags") if isinstance(settings.get("tags"), list) else []
        made_for_kids = str(settings.get("selfDeclaredMadeForKids") or "").lower() == "yes"
        entry["platformSpecificData"] = {
            "title": title or "Video",
            "visibility": visibility if visibility in {"public", "unlisted", "private"} else "public",
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "madeForKids": made_for_kids,
        }
    if content:
        entry.setdefault("customContent", content)
    if media_urls:
        entry["customMedia"] = [
            {"type": media_type_for_url(url), "url": url} for url in media_urls
        ]
    return entry


def build_create_post_body(
    targets: Iterable[Tuple[str, str, Dict[str, Any]]],
    *,
    publish_now: bool,
    scheduled_for: Optional[str] = None,
) -> Dict[str, Any]:
    """
    targets: (platform, zernio_account_id, postiz_post) for one Autobus publish.
    """
    platforms: List[Dict[str, Any]] = []
    shared_content = ""
    shared_media: List[str] = []
    for platform, account_id, post in targets:
        settings = _as_dict(post.get("settings"))
        content = post_content(post)
        media = post_media_urls(post)
        if not shared_content and content:
            shared_content = content
        if not shared_media and media:
            shared_media = media
        platforms.append(
            platform_entry(
                platform=platform,
                account_id=account_id,
                settings=settings,
                content=content,
                media_urls=media,
            )
        )

    body: Dict[str, Any] = {
        "content": shared_content or " ",
        "platforms": platforms,
    }
    if shared_media:
        body["mediaItems"] = [
            {"type": media_type_for_url(url), "url": url} for url in shared_media
        ]
    if publish_now:
        body["publishNow"] = True
    elif scheduled_for:
        body["scheduledFor"] = scheduled_for
    return body


def direct_instagram_post_body(
    *,
    account_id: str,
    caption: str,
    media_urls: List[str],
) -> Dict[str, Any]:
    return {
        "content": (caption or "").strip() or " ",
        "mediaItems": [
            {"type": media_type_for_url(url), "url": url} for url in media_urls if url
        ],
        "platforms": [{"platform": "instagram", "accountId": account_id}],
        "publishNow": True,
    }


def creator_info_for_ui(payload: Dict[str, Any], *, fallback_name: str = "", fallback_username: str = "") -> Dict[str, Any]:
    """Shape Zernio TikTok creator info the way the existing composer reads it."""
    creator = _as_dict(payload.get("creator"))
    privacy_raw = payload.get("privacyLevels") or payload.get("privacy_levels") or []
    privacy: List[str] = []
    if isinstance(privacy_raw, list):
        for item in privacy_raw:
            if isinstance(item, str) and item.strip():
                privacy.append(item.strip())
            elif isinstance(item, dict):
                value = _first_str(item.get("value"), item.get("privacy_level"))
                if value:
                    privacy.append(value)
    # TikTok often returns only Public when the account is new. The composer
    # still needs Friends, Followers, and Only me.
    for level in (
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    ):
        if level not in privacy:
            privacy.append(level)

    limits = _as_dict(payload.get("postingLimits") or payload.get("posting_limits"))
    interactions = _as_dict(limits.get("interactionSettings") or limits.get("interaction_settings"))

    def disabled(name: str) -> bool:
        toggle = _as_dict(interactions.get(name))
        if not toggle:
            return False
        enabled = toggle.get("enabled")
        return enabled is False

    can_post = creator.get("canPostMore", creator.get("can_post_more", True))
    if can_post is None:
        can_post = True
    nickname = _first_str(creator.get("nickname"), creator.get("creator_nickname"), fallback_name)
    username = _first_str(creator.get("username"), creator.get("creator_username"), fallback_username, nickname)
    return {
        "creator_nickname": nickname,
        "creator_username": username,
        "creator_avatar_url": _first_str(creator.get("avatarUrl"), creator.get("avatar_url")),
        "privacy_level_options": privacy,
        "comment_disabled": disabled("allow_comment"),
        "duet_disabled": disabled("allow_duet"),
        "stitch_disabled": disabled("allow_stitch"),
        "max_video_post_duration_sec": limits.get("maxVideoDurationSec")
        or limits.get("max_video_duration_sec"),
        "can_post": bool(can_post),
        "cannot_post_reason": "" if can_post else "This TikTok account cannot post right now. Try again later.",
    }


def publish_status_for_ui(payload: Dict[str, Any]) -> Dict[str, Any]:
    post = _as_dict(payload.get("post")) or payload
    status = _first_str(post.get("status"), payload.get("status")).lower()
    platforms = post.get("platforms") if isinstance(post.get("platforms"), list) else []
    platform_status = ""
    fail_reason = _first_str(post.get("error"), payload.get("error"))
    for item in platforms:
        if not isinstance(item, dict):
            continue
        if str(item.get("platform") or "").lower() == "tiktok" or not platform_status:
            platform_status = _first_str(item.get("status")).lower() or platform_status
            fail_reason = _first_str(item.get("error"), item.get("message"), fail_reason)
    effective = platform_status or status
    complete = effective in {"published", "publish_complete", "success"}
    failed = effective in {"failed", "error", "cancelled", "canceled"}
    if complete:
        message = "Your post is on TikTok."
    elif failed:
        message = fail_reason or "TikTok could not publish this post."
    else:
        message = "TikTok is still processing this post."
    return {
        "status": (effective or "processing").upper(),
        "processing": not complete and not failed,
        "complete": complete,
        "failed": failed,
        "message": message,
        "fail_reason": fail_reason,
    }


def post_id_from_create(payload: Dict[str, Any]) -> str:
    post = _as_dict(payload.get("post"))
    return _first_str(post.get("_id"), post.get("id"), payload.get("postId"), payload.get("post_id"))


def verify_webhook_signature(raw_body: bytes, header: Optional[str], secret: str) -> bool:
    provided = (header or "").strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    if not provided or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def inbox_message_parts(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a message.received delivery into the fields Autobus needs."""
    message = _as_dict(payload.get("message"))
    conversation = _as_dict(payload.get("conversation"))
    account = _as_dict(payload.get("account"))
    sender = _as_dict(message.get("sender"))
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
    external_id = ""
    profile_id = ""
    if profiles and isinstance(profiles[0], dict):
        external_id = _first_str(profiles[0].get("externalId"), profiles[0].get("external_id"))
        profile_id = _first_str(profiles[0].get("id"), profiles[0].get("_id"))

    platform = _first_str(
        message.get("platform"),
        account.get("platform"),
        payload.get("platform"),
    ).lower()
    sender_id = _first_str(sender.get("id"), sender.get("accountId"))
    text = message.get("text")
    if isinstance(text, dict):
        text = text.get("body") or text.get("text")
    text = str(text or "").strip()

    image_url = ""
    attachments = message.get("attachments") or []
    if isinstance(attachments, dict):
        attachments = [attachments]
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            kind = _first_str(item.get("type")).lower()
            url = _first_str(item.get("url"), _as_dict(item.get("payload")).get("url"))
            if url and kind in {"", "image", "photo", "animated_image", "sticker"}:
                image_url = url
                break

    direction = _first_str(message.get("direction")).lower()
    return {
        "external_id": external_id,
        "profile_id": profile_id,
        "platform": platform,
        "channel": CHANNEL_BY_PLATFORM.get(platform, ""),
        "account_id": _first_str(account.get("accountId"), account.get("id"), account.get("_id")),
        "conversation_id": _first_str(
            message.get("conversationId"),
            message.get("conversation_id"),
            conversation.get("id"),
            conversation.get("_id"),
        ),
        "sender_id": sender_id,
        "sender_username": _first_str(sender.get("username")),
        "sender_name": _first_str(sender.get("name")),
        "text": text,
        "image_url": image_url,
        "direction": direction,
        "incoming": direction in {"", "incoming", "inbound"},
    }


def account_connected_parts(payload: Dict[str, Any]) -> Dict[str, str]:
    account = _as_dict(payload.get("account"))
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
    profile_obj = account.get("profileId") if isinstance(account.get("profileId"), dict) else {}
    if not profile_obj and isinstance(account.get("profile"), dict):
        profile_obj = account.get("profile")
    external_id = ""
    profile_id = zernio_id(account.get("profileId")) or _first_str(account.get("profile_id"))
    if profiles and isinstance(profiles[0], dict):
        external_id = _first_str(profiles[0].get("externalId"), profiles[0].get("external_id"))
        profile_id = zernio_id(profiles[0]) or profile_id
    if not external_id:
        external_id = _first_str(profile_obj.get("externalId"), profile_obj.get("external_id"))
    if not profile_id:
        profile_id = zernio_id(profile_obj)
    return {
        "external_id": external_id,
        "profile_id": profile_id,
        "account_id": (
            zernio_id(account.get("accountId"))
            or zernio_id(account.get("id"))
            or zernio_id(account.get("_id"))
        ),
        "platform": _first_str(account.get("platform")).lower(),
        "username": _first_str(account.get("username"), account.get("name")),
        "display_name": _first_str(account.get("displayName"), account.get("name"), account.get("username")),
        "platform_user_id": _first_str(account.get("platformUserId"), account.get("platform_user_id")),
    }


def integration_row(account: Any) -> Dict[str, Any]:
    username = (getattr(account, "username", None) or "").strip()
    display = (getattr(account, "display_name", None) or "").strip()
    label = f"@{username}" if username else display or account.platform.title()
    return {
        "id": account.id,
        "name": label,
        "identifier": account.platform,
        "disabled": not bool(account.is_active),
        "profile": username or None,
        "provider": "zernio",
    }
