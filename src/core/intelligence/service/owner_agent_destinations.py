"""Pure helpers for agent social destinations (no DB / LLM imports)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

AUTOBUS_IG_PREFIX = "autobus-ig-"
SKIP_POSTIZ_IDENTIFIERS = frozenset({"whatsapp"})
MARKETING_POST_IDENTIFIERS = frozenset(
    {"youtube", "tiktok", "facebook", "instagram"}
)
VIDEO_ONLY_PROVIDERS = frozenset({"youtube", "tiktok"})
PLATFORM_ALIASES = {
    "instagram": {"instagram", "instagram-standalone"},
    "ig": {"instagram", "instagram-standalone"},
    "youtube": {"youtube"},
    "yt": {"youtube"},
    "tiktok": {"tiktok"},
    "facebook": {"facebook"},
    "snapchat": {"snapchat"},
    "x": {"x", "twitter"},
    "twitter": {"x", "twitter"},
}


def string_list(raw: Any) -> List[str]:
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value:
            out.append(value)
    return out


def media_looks_like_video(urls: List[str]) -> bool:
    for url in urls:
        path = str(url or "").split("?", 1)[0].lower()
        if any(path.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
            return True
        if ".mp4" in path:
            return True
    return False


def provider_label(identifier: str) -> str:
    ident = (identifier or "").strip().lower()
    return {
        "instagram": "Instagram",
        "instagram-standalone": "Instagram",
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "x": "X",
        "twitter": "X",
        "facebook": "Facebook",
        "snapchat": "Snapchat",
    }.get(ident, ident.replace("-", " ").title() or "Social")


def match_destinations(
    destinations: List[Dict[str, Any]],
    *,
    account_ids: Optional[List[str]] = None,
    platforms: Optional[List[str]] = None,
    instagram_only: bool = False,
    has_video: bool = True,
) -> List[Dict[str, Any]]:
    rows = list(destinations or [])
    if instagram_only:
        rows = [d for d in rows if d.get("channel") == "autobus_instagram"]
    elif not account_ids and not platforms:
        rows = [
            d
            for d in rows
            if d.get("channel") != "autobus_instagram"
            and str(d.get("provider") or "").lower() not in {"instagram", "instagram-standalone"}
        ]
    wanted_ids = {normalize_destination_id(value) for value in (account_ids or []) if value}
    wanted_platforms = set()
    for raw in platforms or []:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        wanted_platforms |= PLATFORM_ALIASES.get(key, {key})
    if wanted_ids:
        matched = [
            d
            for d in rows
            if normalize_destination_id(d.get("id")) in wanted_ids
            or normalize_destination_id(d.get("account_id")) in wanted_ids
        ]
    elif wanted_platforms:
        matched = [d for d in rows if str(d.get("provider") or "").lower() in wanted_platforms]
    else:
        matched = rows
        if not has_video:
            matched = [
                d
                for d in matched
                if str(d.get("provider") or "").lower() not in VIDEO_ONLY_PROVIDERS
            ]
    return matched


def postiz_date_iso(when: Optional[datetime] = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    stamp = stamp.replace("+00:00", "Z")
    if "." in stamp:
        head, rest = stamp.split(".", 1)
        millis = rest.replace("Z", "")[:3]
        return f"{head}.{millis}Z"
    return stamp if stamp.endswith("Z") else f"{stamp}Z"


def title_from_caption(caption: str) -> str:
    for line in (caption or "").splitlines():
        text = line.strip()
        if text:
            return text[:100]
    return "Marketing post"


def postiz_settings_for(identifier: str, title_fallback: str) -> Dict[str, Any]:
    ident = (identifier or "").strip().lower()
    title = (title_fallback or "Marketing post").strip() or "Marketing post"
    if ident in {"instagram", "instagram-standalone"}:
        return {
            "__type": ident,
            "post_type": "post",
            "is_trial_reel": False,
            "collaborators": [],
        }
    if ident in {"x", "twitter"}:
        return {"__type": "x", "who_can_reply_post": "everyone", "community": ""}
    if ident == "youtube":
        return {
            "__type": "youtube",
            "title": title[:100],
            "type": "public",
            "selfDeclaredMadeForKids": "no",
            "tags": [],
        }
    if ident == "tiktok":
        return {
            "__type": "tiktok",
            "title": title[:90],
            "privacy_level": "SELF_ONLY",
            "duet": True,
            "stitch": True,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": False,
            "content_posting_method": "DIRECT_POST",
        }
    return {"__type": ident}


def build_postiz_payload(
    destinations: List[Dict[str, Any]],
    *,
    caption: str,
    media_urls: List[str],
) -> Dict[str, Any]:
    body = (caption or "").strip() or " "
    title = title_from_caption(body)
    image_blocks = [{"id": f"media_{i}", "path": url} for i, url in enumerate(media_urls)]
    posts: List[Dict[str, Any]] = []
    for dest in destinations:
        ident = str(dest.get("provider") or "").strip().lower()
        posts.append(
            {
                "integration": {"id": dest["id"]},
                "value": [{"content": body, "image": image_blocks}],
                "settings": postiz_settings_for(ident, title),
            }
        )
    return {
        "type": "now",
        "date": postiz_date_iso(),
        "shortLink": False,
        "tags": [],
        "posts": posts,
    }


def normalize_destination_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(AUTOBUS_IG_PREFIX):
        return text[len(AUTOBUS_IG_PREFIX) :].strip() or text
    return text
