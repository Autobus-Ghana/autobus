"""Last generated campaign + linked social destinations for the owner agent."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.cloudstorage.service.storageservice import StorageFolder, StorageService
from core.instagram.model.InstagramAccount import InstagramAccount
from core.intelligence.service.business_context_assembler import owner_conversation_key
from core.nlu.service.conversation_manager import ConversationManager
from core.user.model.User import User

from core.intelligence.service.owner_agent_destinations import (
    AUTOBUS_IG_PREFIX,
    MARKETING_POST_IDENTIFIERS,
    SKIP_POSTIZ_IDENTIFIERS,
    VIDEO_ONLY_PROVIDERS,
    build_postiz_payload,
    match_destinations,
    media_looks_like_video,
    provider_label,
    string_list,
)

logger = logging.getLogger(__name__)

CAMPAIGN_KEY = "owner_agent_campaign"


def _await(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def run_async(coro):
    return _await(coro)


def load_campaign(user: User) -> Dict[str, Any]:
    conv_key = owner_conversation_key(user.id)
    state = ConversationManager().get_conversation_state(conv_key)
    pending = state.pending_action if isinstance(state.pending_action, dict) else {}
    campaign = pending.get(CAMPAIGN_KEY)
    return dict(campaign) if isinstance(campaign, dict) else {}


def save_campaign(user: User, **fields: Any) -> Dict[str, Any]:
    conv_key = owner_conversation_key(user.id)
    manager = ConversationManager()
    state = manager.get_conversation_state(conv_key)
    pending = dict(state.pending_action) if isinstance(state.pending_action, dict) else {}
    campaign = dict(pending.get(CAMPAIGN_KEY) or {})
    for key, value in fields.items():
        if value is None:
            continue
        campaign[key] = value
    pending[CAMPAIGN_KEY] = campaign
    state.pending_action = pending
    manager.persist(conv_key)
    return campaign


def fill_publish_args(
    user: User,
    args: Dict[str, Any],
    *,
    db: Optional[Session] = None,
    instagram_only: bool = False,
) -> Dict[str, Any]:
    merged = dict(args or {})
    campaign = load_campaign(user)
    urls = string_list(merged.get("media_urls"))
    if not urls:
        urls = string_list(campaign.get("media_urls"))
        if not urls:
            url = str(campaign.get("url") or "").strip()
            if url:
                urls = [url]
    if urls:
        merged["media_urls"] = urls
    caption = str(merged.get("caption") or "").strip()
    if not caption:
        caption = str(campaign.get("caption") or campaign.get("copy") or "").strip()
        if caption:
            merged["caption"] = caption
    account_id = str(merged.get("account_id") or "").strip()
    if not account_id and instagram_only:
        account_id = str(campaign.get("account_id") or "").strip()
        if account_id:
            merged["account_id"] = account_id
    if db is not None:
        dests = resolve_destinations(
            db,
            user,
            merged,
            instagram_only=instagram_only,
        )
        merged["destinations"] = dests
        merged["destination_ids"] = [d["id"] for d in dests]
        merged["destination_labels"] = [d["label"] for d in dests]
    return merged


def list_accounts(db: Session, user: User) -> List[Dict[str, Any]]:
    rows = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == user.id, InstagramAccount.is_active.is_(True))
        .all()
    )
    return [
        {
            "id": row.id,
            "username": row.username,
            "name": row.name,
            "publishing_enabled": bool(row.publishing_enabled),
        }
        for row in rows
    ]


def pick_publish_account(db: Session, user: User, account_id: Optional[str]) -> Optional[InstagramAccount]:
    query = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == user.id,
        InstagramAccount.is_active.is_(True),
    )
    wanted = (account_id or "").strip()
    if wanted:
        return query.filter(InstagramAccount.id == wanted).first()
    rows = query.all()
    for row in rows:
        if row.publishing_enabled:
            return row
    return rows[0] if rows else None


def upload_image_bytes(user_id: str, raw_b64: str, mime: str) -> str:
    blob = raw_b64.strip()
    if "," in blob:
        blob = blob.split(",", 1)[1]
    data = base64.b64decode(blob)
    mime_type = (mime or "image/png").strip() or "image/png"
    ext = "jpg"
    if "png" in mime_type:
        ext = "png"
    elif "webp" in mime_type:
        ext = "webp"
    elif "gif" in mime_type:
        ext = "gif"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{user_id}_{stamp}_{uuid.uuid4().hex[:8]}.{ext}"
    return StorageService().upload_file(
        io.BytesIO(data),
        filename,
        content_type=mime_type,
        folder=StorageFolder.generated_images,
    )


def list_social_destinations(db: Session, user: User) -> List[Dict[str, Any]]:
    dests: List[Dict[str, Any]] = []
    for ig in list_accounts(db, user):
        username = str(ig.get("username") or "").strip()
        name = str(ig.get("name") or "").strip()
        handle = f"@{username}" if username else (name or "Instagram")
        dests.append(
            {
                "id": f"{AUTOBUS_IG_PREFIX}{ig['id']}",
                "account_id": ig["id"],
                "provider": "instagram",
                "channel": "autobus_instagram",
                "label": f"Instagram ({handle})",
                "publishing_enabled": bool(ig.get("publishing_enabled")),
            }
        )
    for row in fetch_postiz_integrations(db, user):
        ident = str(row.get("identifier") or "").strip().lower()
        iid = str(
            row.get("id") or row.get("integrationId") or row.get("integration_id") or ""
        ).strip()
        if not iid or ident in SKIP_POSTIZ_IDENTIFIERS or ident not in MARKETING_POST_IDENTIFIERS:
            continue
        if row.get("disabled") is True:
            continue
        name = str(row.get("name") or row.get("profile") or ident).strip()
        label = provider_label(ident)
        dests.append(
            {
                "id": iid,
                "provider": ident,
                "channel": "postiz" if ident in {"tiktok", "youtube"} else "zernio",
                "label": f"{label} ({name})" if name and name.lower() != ident else label,
                "publishing_enabled": True,
            }
        )
    return dests


def resolve_destinations(
    db: Session,
    user: User,
    args: Dict[str, Any],
    *,
    instagram_only: bool = False,
) -> List[Dict[str, Any]]:
    ids = string_list(args.get("account_ids"))
    if not ids:
        ids = string_list(args.get("destination_ids"))
    single = str(args.get("account_id") or "").strip()
    if single:
        ids.append(single)
    platforms = string_list(args.get("platforms"))
    urls = string_list(args.get("media_urls"))
    return match_destinations(
        list_social_destinations(db, user),
        account_ids=ids,
        platforms=platforms,
        instagram_only=instagram_only,
        has_video=media_looks_like_video(urls),
    )


def fetch_postiz_integrations(db: Session, user: User) -> List[Dict[str, Any]]:
    from core.socialmedia.service.postiz_native import list_native_integrations
    from core.socialmedia.service.zernio_service import posting_integrations

    try:
        return posting_integrations(db, user.id) + list_native_integrations(user.id, db)
    except Exception:
        logger.exception("[OWNER_AGENT] Could not list connected social accounts")
        return []
