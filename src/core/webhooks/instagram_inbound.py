"""Parse Instagram DM payloads. No app imports, so webhook tests can use it."""

from typing import Any


def ig_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return str(message or "").strip()
    if message.get("is_echo"):
        return ""
    text = message.get("text")
    if isinstance(text, dict):
        text = text.get("body") or text.get("text")
    caption = str(text or "").strip()
    if caption:
        return caption
    for attachment in ig_attachments(message):
        payload = attachment.get("payload") if isinstance(attachment.get("payload"), dict) else {}
        titled = str(payload.get("title") or payload.get("text") or "").strip()
        if titled:
            return titled
    return ""


def ig_attachments(message: dict) -> list:
    attachments = message.get("attachments") or []
    if isinstance(attachments, dict):
        return [attachments]
    if isinstance(attachments, list):
        return [item for item in attachments if isinstance(item, dict)]
    return []


def ig_image_url(message: Any) -> str:
    """Image URL from an Instagram DM, including a photo sent with a caption."""
    if not isinstance(message, dict):
        return ""
    image_types = {"image", "animated_image", "story_mention", "share", "ig_post", "ig_reel"}
    for attachment in ig_attachments(message):
        if str(attachment.get("type") or "").lower() not in image_types:
            continue
        payload = attachment.get("payload") if isinstance(attachment.get("payload"), dict) else {}
        url = str(payload.get("url") or attachment.get("url") or "").strip()
        if url:
            return url
    reply_to = message.get("reply_to") if isinstance(message.get("reply_to"), dict) else {}
    story = reply_to.get("story") if isinstance(reply_to.get("story"), dict) else {}
    return str(story.get("url") or "").strip()
