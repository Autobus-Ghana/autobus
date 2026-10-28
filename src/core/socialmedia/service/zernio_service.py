"""Per-customer Zernio profiles, account connect, posting, and inbox replies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from core.socialmedia.model.ZernioAccount import ZernioAccount
from core.socialmedia.model.ZernioInboxThread import ZernioInboxThread
from core.socialmedia.model.ZernioProfile import ZernioProfile
from core.socialmedia.model.ZernioWebhookEvent import ZernioWebhookEvent
from core.socialmedia.service.zernio_client import ZernioAPIError, ZernioClient
from core.socialmedia.service.zernio_mapping import (
    INBOX_WEBHOOK_EVENTS,
    ZERNIO_MESSAGING_ONLY_PLATFORMS,
    ZERNIO_PLATFORMS,
    account_connected_parts,
    build_create_post_body,
    connect_scopes,
    remote_account_active,
    zernio_id,
    creator_info_for_ui,
    direct_instagram_post_body,
    inbox_message_parts,
    integration_row,
    post_id_from_create,
    publish_status_for_ui,
    verify_webhook_signature,
)
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.user.model.User import User

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 30 * 60


def zernio_enabled() -> bool:
    return bool((os.getenv("ZERNIO_API_KEY") or settings.ZERNIO_API_KEY or "").strip())


def _client() -> ZernioClient:
    return ZernioClient(
        api_key=(os.getenv("ZERNIO_API_KEY") or settings.ZERNIO_API_KEY or "").strip(),
        base_url=(os.getenv("ZERNIO_API_BASE") or settings.ZERNIO_API_BASE or "").strip(),
    )


def _public_api_base() -> str:
    return (
        os.getenv("AUTOBUS_PUBLIC_API_URL")
        or getattr(settings, "AUTOBUS_PUBLIC_API_URL", "")
        or "https://api.useautobus.com"
    ).strip().rstrip("/")


def _sign_state(user_id: str, platform: str, return_to: str) -> str:
    payload = {
        "u": user_id,
        "p": platform,
        "r": "app" if (return_to or "").strip().lower() == "app" else "web",
        "e": int(time.time()) + _STATE_TTL_SECONDS,
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    secret = (settings.SECRET_KEY or os.getenv("SECRET_KEY") or "").encode("utf-8")
    sig = hmac.new(secret, raw.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def read_state(state: str) -> Optional[Dict[str, str]]:
    token = (state or "").strip()
    if "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    secret = (settings.SECRET_KEY or os.getenv("SECRET_KEY") or "").encode("utf-8")
    expected = hmac.new(secret, raw.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    padding = "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + padding))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("e") or 0) < int(time.time()):
        return None
    user_id = str(payload.get("u") or "").strip()
    platform = str(payload.get("p") or "").strip().lower()
    if not user_id or platform not in ZERNIO_PLATFORMS:
        return None
    return {
        "user_id": user_id,
        "platform": platform,
        "return_to": str(payload.get("r") or "web"),
    }


def _profile_name(user: User) -> str:
    company = (getattr(user, "company", None) or "").strip()
    fullname = (getattr(user, "fullname", None) or "").strip()
    email = (getattr(user, "email", None) or "").strip()
    label = company or fullname or email or "Autobus"
    return f"{label} {user.id}"[:120]


def ensure_profile(db: Session, user: User) -> ZernioProfile:
    existing = db.query(ZernioProfile).filter(ZernioProfile.user_id == user.id).first()
    if existing:
        return existing

    client = _client()
    external_id = str(user.id)
    name = _profile_name(user)
    created: Dict[str, Any] = {}
    try:
        created = client.create_profile(name=name, external_id=external_id)
    except ZernioAPIError as exc:
        if exc.status_code != 409:
            raise
        details = exc.body.get("details") if isinstance(exc.body.get("details"), dict) else {}
        existing_id = str(details.get("existingProfileId") or "").strip()
        if existing_id:
            created = {"profile": {"_id": existing_id, "externalId": external_id}}
        else:
            found = client.find_profile(external_id)
            if found:
                created = {"profile": found}
            else:
                raise ZernioAPIError(
                    "A Zernio profile already exists for this customer, but it could not be loaded.",
                    status_code=409,
                ) from exc

    profile = created.get("profile") if isinstance(created.get("profile"), dict) else created
    profile_id = str(profile.get("_id") or profile.get("id") or "").strip()
    if not profile_id:
        found = client.find_profile(external_id)
        profile_id = str((found or {}).get("_id") or (found or {}).get("id") or "").strip()
    if not profile_id:
        raise ZernioAPIError("Zernio did not return a profile id", status_code=502)

    row = ZernioProfile(
        id=f"zp_{uuid.uuid4().hex[:12]}",
        user_id=user.id,
        zernio_profile_id=profile_id,
        external_id=external_id,
        name=name,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.query(ZernioProfile).filter(ZernioProfile.user_id == user.id).first()
        if row:
            return row
        raise
    db.refresh(row)
    return row


def connect_authorization(
    db: Session,
    user: User,
    platform: str,
    *,
    return_to: str = "web",
) -> Dict[str, Any]:
    slug = (platform or "").strip().lower().replace("_", "-")
    if slug == "instagram-standalone":
        slug = "instagram"
    if slug not in ZERNIO_PLATFORMS:
        raise ZernioAPIError(f"{platform} is not connected through Zernio", status_code=400)
    if not zernio_enabled():
        raise ZernioAPIError(
            "ZERNIO_API_KEY is not configured. Instagram, Facebook, TikTok, and WhatsApp connect through Zernio.",
            status_code=400,
        )

    profile = ensure_profile(db, user)
    state = _sign_state(user.id, slug, return_to)
    redirect = f"{_public_api_base()}/api/v1/social/zernio/callback?state={quote(state, safe='')}"
    auth_url = _client().connect_url(
        slug,
        profile_id=profile.zernio_profile_id,
        redirect_url=redirect,
        scopes=connect_scopes(slug),
    )
    label = slug.replace("-", " ").title()
    return {
        "authorization_url": auth_url,
        "platform": slug.upper(),
        "provider": "ZERNIO",
        "direct_oauth": True,
        "state": state,
        "redirect_uri": redirect,
        "message": (
            f"Open authorization_url to link {label}. "
            "Inbox messages and posting go through Zernio."
        ),
    }


def _upsert_account(
    db: Session,
    *,
    user_id: str,
    profile_id: str,
    account_id: str,
    platform: str,
    username: str = "",
    display_name: str = "",
    platform_user_id: str = "",
    active: bool = True,
) -> ZernioAccount:
    row = (
        db.query(ZernioAccount)
        .filter(ZernioAccount.zernio_account_id == account_id)
        .first()
    )
    if row and row.user_id != user_id and row.is_active:
        raise ZernioAPIError(
            "This social account is already linked to another Autobus customer.",
            status_code=409,
        )
    if row is None:
        row = ZernioAccount(
            id=f"za_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            zernio_profile_id=profile_id,
            zernio_account_id=account_id,
            platform=platform,
        )
        db.add(row)
    row.user_id = user_id
    row.zernio_profile_id = profile_id or row.zernio_profile_id
    row.platform = platform or row.platform
    row.username = username or row.username
    row.display_name = display_name or row.display_name
    row.platform_user_id = platform_user_id or row.platform_user_id
    row.is_active = active
    row.messaging_enabled = active
    row.publishing_enabled = active
    db.commit()
    db.refresh(row)
    return row


def sync_accounts_from_zernio(db: Session, user_id: str) -> List[ZernioAccount]:
    profile = db.query(ZernioProfile).filter(ZernioProfile.user_id == user_id).first()
    if not profile or not zernio_enabled():
        return (
            db.query(ZernioAccount)
            .filter(ZernioAccount.user_id == user_id, ZernioAccount.is_active.is_(True))
            .all()
        )
    remote = _client().list_accounts(profile.zernio_profile_id)
    seen = set()
    for item in remote:
        if not isinstance(item, dict):
            continue
        account_id = (
            zernio_id(item.get("_id"))
            or zernio_id(item.get("id"))
            or zernio_id(item.get("accountId"))
        )
        platform = str(item.get("platform") or "").strip().lower()
        if not account_id or platform not in ZERNIO_PLATFORMS:
            continue
        seen.add(account_id)
        try:
            _upsert_account(
                db,
                user_id=user_id,
                profile_id=zernio_id(item.get("profileId")) or profile.zernio_profile_id,
                account_id=account_id,
                platform=platform,
                username=str(item.get("username") or "").strip(),
                display_name=str(item.get("displayName") or item.get("name") or item.get("username") or "").strip(),
                platform_user_id=str(item.get("platformUserId") or "").strip(),
                active=remote_account_active(item),
            )
        except ZernioAPIError as exc:
            logger.warning("[ZERNIO] skipped account %s: %s", account_id, exc)
    return (
        db.query(ZernioAccount)
        .filter(ZernioAccount.user_id == user_id, ZernioAccount.is_active.is_(True))
        .all()
    )


def record_callback_account(
    db: Session,
    *,
    user_id: str,
    platform: str,
    account_id: str,
    username: str = "",
    profile_id: str = "",
) -> ZernioAccount:
    profile = db.query(ZernioProfile).filter(ZernioProfile.user_id == user_id).first()
    resolved_profile = profile_id or (profile.zernio_profile_id if profile else "")
    if not resolved_profile:
        raise ZernioAPIError("No Zernio profile for this customer", status_code=400)
    row = _upsert_account(
        db,
        user_id=user_id,
        profile_id=resolved_profile,
        account_id=account_id,
        platform=platform,
        username=username,
        display_name=username,
    )
    try:
        sync_accounts_from_zernio(db, user_id)
    except Exception as exc:
        logger.warning("[ZERNIO] account refresh after connect failed: %s", exc)
        db.rollback()
    refreshed = (
        db.query(ZernioAccount)
        .filter(ZernioAccount.zernio_account_id == account_id)
        .first()
    )
    return refreshed or row


def list_accounts(db: Session, user_id: str, platform: str = "") -> List[ZernioAccount]:
    query = db.query(ZernioAccount).filter(
        ZernioAccount.user_id == user_id,
        ZernioAccount.is_active.is_(True),
    )
    slug = (platform or "").strip().lower()
    if slug:
        query = query.filter(ZernioAccount.platform == slug)
    return query.order_by(ZernioAccount.connected_at.asc()).all()


def find_account(db: Session, user_id: str, local_or_remote_id: str) -> Optional[ZernioAccount]:
    key = (local_or_remote_id or "").strip()
    if not key:
        return None
    return (
        db.query(ZernioAccount)
        .filter(
            ZernioAccount.user_id == user_id,
            ZernioAccount.is_active.is_(True),
            (ZernioAccount.id == key) | (ZernioAccount.zernio_account_id == key),
        )
        .first()
    )


def posting_integrations(db: Session, user_id: str) -> List[Dict[str, Any]]:
    """Connected Zernio accounts for the marketing account list.

    Instagram is listed from /instagram/accounts so it is not shown twice.
    Refresh from Zernio first so a channel that connected without a callback
    (YouTube often does) still appears when the composer loads.
    """
    try:
        sync_accounts_from_zernio(db, user_id)
    except Exception as exc:
        logger.warning("[ZERNIO] account sync before listing failed: %s", exc)
        db.rollback()
    rows = []
    for account in list_accounts(db, user_id):
        if account.platform == "instagram" or account.platform in ZERNIO_MESSAGING_ONLY_PLATFORMS:
            continue
        rows.append(integration_row(account))
    return rows


def disconnect_account(db: Session, user_id: str, local_or_remote_id: str) -> bool:
    row = find_account(db, user_id, local_or_remote_id)
    if row is None:
        inactive = (
            db.query(ZernioAccount)
            .filter(
                ZernioAccount.user_id == user_id,
                (ZernioAccount.id == local_or_remote_id)
                | (ZernioAccount.zernio_account_id == local_or_remote_id),
            )
            .first()
        )
        return inactive is not None
    if zernio_enabled():
        try:
            _client().delete_account(row.zernio_account_id)
        except ZernioAPIError as exc:
            logger.warning("[ZERNIO] disconnect %s failed: %s", row.zernio_account_id, exc)
    row.is_active = False
    row.messaging_enabled = False
    row.publishing_enabled = False
    db.commit()
    return True


def publish_targets(
    db: Session,
    user_id: str,
    targets: List[Tuple[ZernioAccount, Dict[str, Any]]],
    *,
    publish_now: bool,
    scheduled_for: Optional[str],
) -> Dict[str, Any]:
    if not targets:
        return {}
    body = build_create_post_body(
        ((account.platform, account.zernio_account_id, post) for account, post in targets),
        publish_now=publish_now,
        scheduled_for=scheduled_for,
    )
    result = _client().create_post(body, idempotency_key=str(uuid.uuid4()))
    post_id = post_id_from_create(result)
    if post_id:
        for account, _post in targets:
            account.last_post_id = post_id
        db.commit()
    return result


def publish_instagram(
    db: Session,
    account: ZernioAccount,
    *,
    caption: str,
    media_urls: List[str],
) -> Dict[str, Any]:
    from core.socialmedia.service.instagram_media_prepare import (
        InstagramMediaPrepareError,
        InstagramMediaPrepareService,
    )

    try:
        prepared = InstagramMediaPrepareService().prepare_urls(media_urls, crop="feed")
    except InstagramMediaPrepareError as exc:
        raise ZernioAPIError(str(exc), status_code=400) from exc
    body = direct_instagram_post_body(
        account_id=account.zernio_account_id,
        caption=caption,
        media_urls=prepared,
    )
    result = _client().create_post(body, idempotency_key=str(uuid.uuid4()))
    post_id = post_id_from_create(result)
    if post_id:
        account.last_post_id = post_id
        db.commit()
    return {"post_id": post_id, "result": result}


def tiktok_creator_info(account: ZernioAccount) -> Dict[str, Any]:
    payload = _client().tiktok_creator_info(account.zernio_account_id, media_type="video")
    return creator_info_for_ui(
        payload,
        fallback_name=account.display_name or "",
        fallback_username=account.username or "",
    )


def tiktok_publish_status(account: ZernioAccount, publish_id: str = "") -> Dict[str, Any]:
    post_id = (publish_id or account.last_post_id or "").strip()
    if not post_id:
        return {
            "status": "PROCESSING",
            "processing": True,
            "complete": False,
            "failed": False,
            "message": "TikTok is still processing this post.",
            "fail_reason": "",
        }
    return publish_status_for_ui(_client().get_post(post_id))


def split_postiz_posts(
    db: Session,
    user_id: str,
    payload: Dict[str, Any],
) -> Tuple[List[Tuple[ZernioAccount, Dict[str, Any]]], List[Dict[str, Any]]]:
    zernio_posts: List[Tuple[ZernioAccount, Dict[str, Any]]] = []
    other: List[Dict[str, Any]] = []
    for post in payload.get("posts") or []:
        if not isinstance(post, dict):
            continue
        integration = post.get("integration") if isinstance(post.get("integration"), dict) else {}
        account = find_account(db, user_id, str(integration.get("id") or ""))
        if account and account.platform in ZERNIO_PLATFORMS:
            zernio_posts.append((account, post))
        else:
            other.append(post)
    return zernio_posts, other


def remember_thread(
    db: Session,
    *,
    user_id: str,
    autobus_key: str,
    conversation_id: str,
    account_id: str,
    platform: str,
    participant_id: str,
    username: str = "",
) -> ZernioInboxThread:
    row = (
        db.query(ZernioInboxThread)
        .filter(ZernioInboxThread.autobus_key == autobus_key)
        .first()
    )
    if row is None:
        row = ZernioInboxThread(
            id=f"zt_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            autobus_key=autobus_key,
            zernio_conversation_id=conversation_id,
            zernio_account_id=account_id,
            platform=platform,
            participant_id=participant_id,
        )
        db.add(row)
    row.zernio_conversation_id = conversation_id or row.zernio_conversation_id
    row.zernio_account_id = account_id or row.zernio_account_id
    row.platform = platform or row.platform
    row.participant_id = participant_id or row.participant_id
    row.participant_username = username or row.participant_username
    db.commit()
    db.refresh(row)
    return row


def send_thread_reply(db: Session, autobus_key: str, text: str, image_url: str = "") -> bool:
    row = (
        db.query(ZernioInboxThread)
        .filter(ZernioInboxThread.autobus_key == autobus_key)
        .first()
    )
    if row is None or not zernio_enabled():
        return False
    client = _client()
    sent = True
    body = (text or "").strip()
    if body:
        client.send_inbox_message(
            row.zernio_conversation_id,
            account_id=row.zernio_account_id,
            message=body,
            idempotency_key=str(uuid.uuid4()),
        )
    if (image_url or "").strip():
        client.send_inbox_message(
            row.zernio_conversation_id,
            account_id=row.zernio_account_id,
            attachment_url=image_url.strip(),
            attachment_type="image",
            idempotency_key=str(uuid.uuid4()),
        )
        if not body:
            sent = True
    return sent


def claim_webhook_event(db: Session, event_id: str, event_name: str) -> bool:
    """Return True when this delivery should be processed."""
    key = (event_id or "").strip()
    if not key:
        return True
    db.add(ZernioWebhookEvent(id=key[:80], event=(event_name or "unknown")[:64]))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def apply_account_event(db: Session, payload: Dict[str, Any], *, connected: bool) -> None:
    parts = account_connected_parts(payload)
    if not parts["account_id"]:
        logger.info("[ZERNIO] account event missing account id")
        return
    if not parts["external_id"] and parts["profile_id"]:
        profile = (
            db.query(ZernioProfile)
            .filter(ZernioProfile.zernio_profile_id == parts["profile_id"])
            .first()
        )
        if profile:
            parts["external_id"] = profile.user_id
    if not parts["external_id"]:
        logger.info("[ZERNIO] account event missing customer id")
        return
    if not connected:
        row = (
            db.query(ZernioAccount)
            .filter(ZernioAccount.zernio_account_id == parts["account_id"])
            .first()
        )
        if row:
            row.is_active = False
            row.messaging_enabled = False
            row.publishing_enabled = False
            db.commit()
        return
    if parts["platform"] not in ZERNIO_PLATFORMS:
        return
    _upsert_account(
        db,
        user_id=parts["external_id"],
        profile_id=parts["profile_id"],
        account_id=parts["account_id"],
        platform=parts["platform"],
        username=parts["username"],
        display_name=parts["display_name"],
        platform_user_id=parts["platform_user_id"],
    )


def handle_inbox_message(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    from core.nlu.nlu import get_nlu_system
    from core.webhooks.controller.webhookscontroller import _nlu_reply_text, _remember_customer_identity

    parts = inbox_message_parts(payload)
    if not parts["incoming"]:
        return {"status": "ok", "message": "Ignored outgoing message"}
    if not parts["external_id"] or not parts["channel"] or not parts["sender_id"]:
        logger.info("[ZERNIO] inbox event missing customer, channel, or sender")
        return {"status": "ok", "message": "Ignored incomplete inbox event"}
    if not parts["conversation_id"] or not parts["account_id"]:
        logger.info("[ZERNIO] inbox event missing conversation or account")
        return {"status": "ok", "message": "Ignored inbox event without a conversation"}

    text = parts["text"]
    image_url = parts["image_url"]
    if not text and image_url:
        text = "I am providing you with an image."
    if not text:
        return {"status": "ok", "message": "Ignored empty inbox message"}

    account = (
        db.query(ZernioAccount)
        .filter(
            ZernioAccount.zernio_account_id == parts["account_id"],
            ZernioAccount.is_active.is_(True),
        )
        .first()
    )
    merchant_id = account.user_id if account else parts["external_id"]
    autobus_key = f"{merchant_id}:{parts['channel']}:{parts['sender_id']}"
    remember_thread(
        db,
        user_id=merchant_id,
        autobus_key=autobus_key,
        conversation_id=parts["conversation_id"],
        account_id=parts["account_id"],
        platform=parts["platform"],
        participant_id=parts["sender_id"],
        username=parts["sender_username"],
    )

    nlu = get_nlu_system()
    paused = CreditService(db).consume_or_pause(merchant_id, CreditType.LLM.value, 1.0, "zernio_inbox")
    if paused:
        reply = paused
        reply_images = []
    else:
        reply = nlu.process_message(autobus_key, text, image_url=image_url or None)
        reply_images = nlu.take_reply_images()
    _remember_customer_identity(
        autobus_key,
        username=parts["sender_username"] or None,
        display_name=parts["sender_name"] or None,
    )
    outbound = _nlu_reply_text(reply)
    if outbound or reply_images:
        send_thread_reply(db, autobus_key, outbound, reply_images[0] if reply_images else "")
        for extra in reply_images[1:]:
            send_thread_reply(db, autobus_key, "", extra)
    return {"status": "ok", "message": "Processed inbox message"}


def webhook_signature_ok(raw_body: bytes, header: Optional[str]) -> bool:
    secret = (os.getenv("ZERNIO_WEBHOOK_SECRET") or settings.ZERNIO_WEBHOOK_SECRET or "").strip()
    if not secret:
        logger.error("[ZERNIO] ZERNIO_WEBHOOK_SECRET is not configured")
        return False
    return verify_webhook_signature(raw_body, header, secret)


async def ensure_zernio_webhook() -> None:
    if not zernio_enabled():
        return
    secret = (os.getenv("ZERNIO_WEBHOOK_SECRET") or settings.ZERNIO_WEBHOOK_SECRET or "").strip()
    if not secret:
        logger.warning("[ZERNIO] webhook not registered; set ZERNIO_WEBHOOK_SECRET")
        return
    url = f"{_public_api_base()}/api/v1/webhooks/zernio"
    client = _client()
    try:
        existing = client.list_webhooks()
    except ZernioAPIError as exc:
        logger.warning("[ZERNIO] could not list webhooks (%s). Register %s in the Zernio dashboard.", exc, url)
        return
    for hook in existing:
        if isinstance(hook, dict) and str(hook.get("url") or "").rstrip("/") == url:
            return
    try:
        client.create_webhook(
            name="Autobus",
            url=url,
            secret=secret,
            events=list(INBOX_WEBHOOK_EVENTS),
        )
        logger.info("[ZERNIO] registered webhook %s", url)
    except ZernioAPIError as exc:
        logger.warning("[ZERNIO] webhook registration failed: %s", exc)

