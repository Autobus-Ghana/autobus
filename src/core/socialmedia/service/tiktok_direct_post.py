"""TikTok Direct Post helpers: creator_info, publish status, consent copy."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from sqlalchemy import create_engine, text

from core.socialmedia.service.postiz_api_service import (
    PostizAPIError,
    PostizClient,
    normalize_postiz_integrations_list,
)

logger = logging.getLogger(__name__)

TIKTOK_CREATOR_INFO_URL = (
    "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
)
TIKTOK_PUBLISH_STATUS_URL = (
    "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
)
TIKTOK_OAUTH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
RECONNECT_TIKTOK_MESSAGE = (
    "TikTok needs to be linked again. Open Marketing, disconnect TikTok, and connect it once more."
)
_AUTH_FAILURE_MARKERS = (
    "access_token_invalid",
    "access_token_expired",
    "invalid_access_token",
    "the access token is invalid",
    "access token is invalid or not found",
)

CANNOT_POST_ERROR_CODES = frozenset(
    {
        "spam_risk_too_many_posts",
        "spam_risk_user_banned_from_posting",
        "reached_active_user_cap",
    }
)

PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Everyone",
    "MUTUAL_FOLLOW_FRIENDS": "Friends",
    "FOLLOWER_OF_CREATOR": "Followers",
    "SELF_ONLY": "Only me",
}

MUSIC_USAGE_CONFIRMATION_URL = (
    "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
)
BRANDED_CONTENT_POLICY_URL = (
    "https://www.tiktok.com/legal/page/global/bc-policy/en"
)

CONSENT_MUSIC = "By posting, you agree to TikTok's Music Usage Confirmation"
CONSENT_BRANDED = (
    "By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation"
)
PROCESSING_NOTICE = (
    "After you finish publishing, it may take a few minutes for the content "
    "to process and be visible on your TikTok profile."
)
UNAUDITED_PUBLIC_MESSAGE = (
    "TikTok has not approved this app for posting to public accounts. "
    "Until that audit is approved, set the TikTok account to Private and try again."
)


def tiktok_consent_text(*, branded_content: bool) -> str:
    return CONSENT_BRANDED if branded_content else CONSENT_MUSIC


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _error_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            nested = _first_str(
                value.get("message"),
                value.get("value"),
                value.get("error"),
                value.get("detail"),
            )
            if nested:
                return nested
            continue
        text = _first_str(value)
        if text and text.upper() not in {"ERROR", "FAILED", "OK", "SUCCESS"}:
            return text
    return ""


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _privacy_options(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def user_facing_publish_error(fail_reason: str) -> str:
    text = (fail_reason or "").strip()
    lowered = text.lower()
    if (
        "unaudited_client_can_only_post_to_private_accounts" in lowered
        or "not approved for public posting" in lowered
    ):
        return UNAUDITED_PUBLIC_MESSAGE
    return text


def is_tiktok_auth_failure(payload: Any) -> bool:
    """True when TikTok rejected the call because the access token cannot be used."""
    parts: List[str] = []
    if isinstance(payload, str):
        parts.append(payload)
    elif isinstance(payload, dict):
        error = _as_dict(payload.get("error"))
        parts.extend(
            [
                _first_str(error.get("code"), payload.get("error_code"), payload.get("code")),
                _first_str(
                    error.get("message"),
                    payload.get("message"),
                    payload.get("error_description"),
                    payload.get("cannot_post_reason"),
                ),
            ]
        )
        nested = payload.get("data")
        if isinstance(nested, dict):
            parts.append(_first_str(nested.get("error_code"), nested.get("message")))
    blob = " ".join(part for part in parts if part).lower()
    return any(marker in blob for marker in _AUTH_FAILURE_MARKERS)


def cannot_post_reason(error_code: str, error_message: str = "") -> str:
    code = (error_code or "").strip()
    if code == "spam_risk_too_many_posts":
        return "This TikTok account cannot make more posts right now. Try again later."
    if code == "spam_risk_user_banned_from_posting":
        return "This TikTok account is not allowed to post right now."
    if code == "reached_active_user_cap":
        return "TikTok posting is temporarily capped for this app. Try again later."
    if error_message.strip():
        return error_message.strip()
    return "This TikTok account cannot post right now. Try again later."


def normalize_creator_info(
    payload: Any,
    *,
    fallback_name: str = "",
    fallback_username: str = "",
    fallback_avatar: str = "",
) -> Dict[str, Any]:
    """Normalize TikTok / Postiz creator_info payloads into one UI shape."""
    root = _as_dict(payload)
    data = _as_dict(root.get("data")) if "data" in root else root
    if not data and isinstance(root.get("creator_info"), dict):
        data = _as_dict(root.get("creator_info"))

    error = _as_dict(root.get("error"))
    error_code = _first_str(error.get("code"), root.get("error_code"))
    error_message = _first_str(error.get("message"), root.get("message"))
    error_is_ok = error_code.lower() in {"", "ok", "success"}

    can_post_field = data.get("can_post", root.get("can_post"))
    can_post = True if can_post_field is None else _as_bool(can_post_field, True)
    blocked_reason = ""
    if is_tiktok_auth_failure({"error_code": error_code, "message": error_message}):
        can_post = False
        blocked_reason = RECONNECT_TIKTOK_MESSAGE
    elif error_code in CANNOT_POST_ERROR_CODES or not error_is_ok:
        can_post = False
        blocked_reason = cannot_post_reason(error_code, error_message)
    elif can_post_field is not None and not can_post:
        blocked_reason = cannot_post_reason(error_code, error_message)

    privacy = _privacy_options(
        data.get("privacy_level_options")
        or data.get("privacyLevelOptions")
        or root.get("privacy_level_options")
        or root.get("privacyLevelOptions")
    )

    max_duration = _as_int(
        data.get("max_video_post_duration_sec")
        or data.get("maxVideoPostDurationSec")
        or data.get("maxDurationSeconds")
        or root.get("max_video_post_duration_sec")
        or root.get("maxDurationSeconds")
    )

    nickname = _first_str(
        data.get("creator_nickname"),
        data.get("nickname"),
        root.get("creator_nickname"),
        root.get("nickname"),
        fallback_name,
    )
    username = _first_str(
        data.get("creator_username"),
        data.get("username"),
        root.get("creator_username"),
        root.get("username"),
        fallback_username,
    )
    avatar = _first_str(
        data.get("creator_avatar_url"),
        data.get("avatar_url"),
        data.get("picture"),
        root.get("creator_avatar_url"),
        fallback_avatar,
    )

    return {
        "creator_nickname": nickname,
        "creator_username": username,
        "creator_avatar_url": avatar,
        "privacy_level_options": privacy,
        "privacy_level_labels": {key: PRIVACY_LABELS.get(key, key) for key in privacy},
        "comment_disabled": _as_bool(
            data.get("comment_disabled", data.get("commentDisabled")), False
        ),
        "duet_disabled": _as_bool(
            data.get("duet_disabled", data.get("duetDisabled")), False
        ),
        "stitch_disabled": _as_bool(
            data.get("stitch_disabled", data.get("stitchDisabled")), False
        ),
        "max_video_post_duration_sec": max_duration,
        "can_post": can_post,
        "cannot_post_reason": blocked_reason,
        "music_usage_confirmation_url": MUSIC_USAGE_CONFIRMATION_URL,
        "branded_content_policy_url": BRANDED_CONTENT_POLICY_URL,
        "consent_music": CONSENT_MUSIC,
        "consent_branded": CONSENT_BRANDED,
        "processing_notice": PROCESSING_NOTICE,
        "error_code": error_code if not error_is_ok else "",
    }


def extract_publish_ids(payload: Any) -> List[str]:
    found: List[str] = []
    seen = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in (
                "publish_id",
                "publishId",
                "postId",
                "post_id",
                "id",
                "releaseId",
            ):
                value = node.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in seen:
                    if key in {"publish_id", "publishId"} or str(value).startswith(
                        ("v_pub", "p_pub", "photo_post")
                    ):
                        seen.add(value.strip())
                        found.append(value.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def extract_post_ids(payload: Any) -> List[str]:
    found: List[str] = []
    seen = set()

    def consider(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        found.append(text)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("id", "postId", "post_id", "releaseId"):
                consider(node.get(key))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def normalize_publish_status(payload: Any) -> Dict[str, Any]:
    root = _as_dict(payload)
    data = _as_dict(root.get("data")) if "data" in root else root
    status = _first_str(
        data.get("status"),
        root.get("status"),
        root.get("state"),
        data.get("state"),
    ).upper()
    fail_reason = _error_text(
        data.get("fail_reason"),
        data.get("error"),
        root.get("fail_reason"),
        root.get("error"),
        root.get("lastMessage"),
        root.get("message"),
    )
    public_ids = data.get("publicaly_available_post_id") or data.get(
        "publicly_available_post_id"
    )
    if not isinstance(public_ids, list):
        public_ids = []
    complete = status in {"PUBLISH_COMPLETE", "PUBLISHED"}
    failed = status in {"FAILED", "ERROR"}
    processing = status in {
        "PROCESSING_UPLOAD",
        "PROCESSING_DOWNLOAD",
        "SEND_TO_USER_INBOX",
        "QUEUE",
        "PENDING",
    } or (status and not complete and not failed)
    message = PROCESSING_NOTICE
    if complete:
        message = "Your post is on TikTok."
    elif failed:
        message = user_facing_publish_error(fail_reason) or (
            "TikTok could not publish this post."
        )
    elif status == "SEND_TO_USER_INBOX":
        message = (
            "TikTok sent a draft to the creator inbox. Open TikTok to finish the post."
        )
    return {
        "status": status or "PROCESSING_DOWNLOAD",
        "processing": processing and not complete and not failed,
        "complete": complete,
        "failed": failed,
        "message": message,
        "fail_reason": fail_reason,
        "public_post_ids": [str(x) for x in public_ids if x],
        "processing_notice": PROCESSING_NOTICE,
    }


def _postiz_engine():
    dsn = os.getenv("POSTIZ_DATABASE_URL", "").strip()
    if not dsn:
        return None
    return create_engine(dsn, pool_pre_ping=True)


def tiktok_tokens_from_postiz_db(integration_id: str) -> Dict[str, str]:
    """Read the TikTok access token and refresh token stored by Postiz."""
    iid = (integration_id or "").strip()
    engine = _postiz_engine()
    if engine is None or not iid:
        return {}
    sql = """
        SELECT token, "refreshToken"
        FROM "Integration"
        WHERE id = :id AND "providerIdentifier" = 'tiktok'
          AND ("deletedAt" IS NULL)
        LIMIT 1
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"id": iid}).mappings().first()
    except Exception as exc:
        logger.warning("[SOCIAL] Could not read TikTok tokens from Postiz DB: %s", exc)
        return {}
    if not row:
        return {}
    access = _first_str(row.get("token"))
    refresh = _first_str(row.get("refreshToken"))
    out: Dict[str, str] = {}
    if access:
        out["token"] = access
    if refresh:
        out["refresh_token"] = refresh
    return out


def store_tiktok_tokens(
    integration_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> None:
    """Write a refreshed TikTok token back so later posts use it."""
    iid = (integration_id or "").strip()
    access = (access_token or "").strip()
    refresh = (refresh_token or "").strip()
    engine = _postiz_engine()
    if engine is None or not iid or not access or not refresh:
        return
    from datetime import datetime, timedelta, timezone

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(int(expires_in or 0), 60))
    sql = """
        UPDATE "Integration"
        SET token = :token,
            "refreshToken" = :refresh,
            "tokenExpiration" = :expires_at,
            "refreshNeeded" = false
        WHERE id = :id AND "providerIdentifier" = 'tiktok'
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(sql),
                {
                    "id": iid,
                    "token": access,
                    "refresh": refresh,
                    "expires_at": expires_at,
                },
            )
    except Exception as exc:
        logger.warning("[SOCIAL] Could not store refreshed TikTok token: %s", exc)


def token_from_postiz_db(integration_id: str) -> Optional[str]:
    dsn = os.getenv("POSTIZ_DATABASE_URL", "").strip()
    iid = (integration_id or "").strip()
    if not dsn or not iid:
        return None
    queries = (
        """
        SELECT token FROM "Integration"
        WHERE id = :id AND "providerIdentifier" = 'tiktok'
          AND ("deletedAt" IS NULL)
        LIMIT 1
        """,
        """
        SELECT token FROM "Integration"
        WHERE id = :id AND "providerIdentifier" = 'tiktok'
        LIMIT 1
        """,
    )
    try:
        engine = create_engine(dsn, pool_pre_ping=True)
        with engine.connect() as conn:
            for sql in queries:
                try:
                    row = conn.execute(text(sql), {"id": iid}).first()
                except Exception:
                    continue
                if row and row[0]:
                    token = str(row[0]).strip()
                    if token:
                        return token
    except Exception as exc:
        logger.warning("[SOCIAL] Could not read TikTok token from Postiz DB: %s", exc)
    return None


def latest_postiz_post_error(integration_id: str) -> str:
    """Read the newest Postiz Post.error for this TikTok integration."""
    dsn = os.getenv("POSTIZ_DATABASE_URL", "").strip()
    iid = (integration_id or "").strip()
    if not dsn or not iid:
        return ""
    sql = """
        SELECT error FROM "Post"
        WHERE "integrationId" = :id AND ("deletedAt" IS NULL)
        ORDER BY "publishDate" DESC NULLS LAST, "createdAt" DESC
        LIMIT 1
    """
    try:
        engine = create_engine(dsn, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"id": iid}).first()
    except Exception as exc:
        logger.info("[SOCIAL] Could not read Postiz post error: %s", exc)
        return ""
    if not row or not row[0]:
        return ""
    raw = row[0]
    blob = raw if isinstance(raw, str) else _error_text(raw)
    return user_facing_publish_error(blob)


async def query_tiktok_creator_info(access_token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            TIKTOK_CREATOR_INFO_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={},
        )
    if res.status_code >= 400:
        raise PostizAPIError(
            f"TikTok creator_info failed ({res.status_code}): {res.text}",
            status_code=res.status_code,
            body=res.text or "",
        )
    return res.json() if res.text.strip() else {}


async def query_tiktok_publish_status(
    access_token: str, publish_id: str
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            TIKTOK_PUBLISH_STATUS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )
    if res.status_code >= 400:
        raise PostizAPIError(
            f"TikTok publish status failed ({res.status_code}): {res.text}",
            status_code=res.status_code,
            body=res.text or "",
        )
    return res.json() if res.text.strip() else {}


def _tiktok_app_credentials() -> Tuple[str, str]:
    client_key = (
        os.getenv("TIKTOK_CLIENT_ID", "").strip()
        or os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    )
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    return client_key, client_secret


async def refresh_tiktok_access_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """Exchange a TikTok refresh token for a new access token."""
    client_key, client_secret = _tiktok_app_credentials()
    refresh = (refresh_token or "").strip()
    if not client_key or not client_secret or not refresh:
        return None
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            TIKTOK_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
        )
    try:
        payload = res.json() if res.text.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    access = _first_str(payload.get("access_token"))
    if not access:
        logger.info(
            "[SOCIAL] TikTok token refresh failed (%s): %s",
            res.status_code,
            _first_str(payload.get("error"), payload.get("error_description"))
            or "no access token",
        )
        return None
    return payload


def _usable_creator_info(
    payload: Any,
    *,
    fallback_name: str = "",
    fallback_username: str = "",
    fallback_avatar: str = "",
) -> Optional[Dict[str, Any]]:
    if is_tiktok_auth_failure(payload):
        return None
    info = normalize_creator_info(
        payload,
        fallback_name=fallback_name,
        fallback_username=fallback_username,
        fallback_avatar=fallback_avatar,
    )
    if is_tiktok_auth_failure(
        {"error_code": info.get("error_code"), "message": info.get("cannot_post_reason")}
    ):
        return None
    if not (
        info["privacy_level_options"] or info["creator_nickname"] or info["error_code"]
    ):
        return None
    if not info["creator_nickname"]:
        info["creator_nickname"] = fallback_name
    if not info["creator_username"]:
        info["creator_username"] = fallback_username
    if not info["creator_avatar_url"]:
        info["creator_avatar_url"] = fallback_avatar
    return info


def _integration_row(
    rows: Iterable[Dict[str, Any]], integration_id: str
) -> Optional[Dict[str, Any]]:
    iid = (integration_id or "").strip()
    for row in rows:
        if str(row.get("id") or "").strip() != iid:
            continue
        ident = str(row.get("identifier") or row.get("providerIdentifier") or "").lower()
        if ident and ident != "tiktok":
            return None
        return row
    return None


async def load_tiktok_creator_info(
    *,
    client: PostizClient,
    api_key: str,
    integration_id: str,
    postiz_email: str = "",
    postiz_password: str = "",
) -> Dict[str, Any]:
    iid = (integration_id or "").strip()
    if not iid:
        raise PostizAPIError("integration id is required", status_code=400)

    raw_list = await client.list_integrations(api_key)
    rows = normalize_postiz_integrations_list(raw_list)
    integration = _integration_row(rows, iid)
    if not integration:
        raise PostizAPIError("TikTok channel not found", status_code=404)

    fallback_name = _first_str(integration.get("name"))
    fallback_username = _first_str(integration.get("profile"), integration.get("display"))
    fallback_avatar = _first_str(integration.get("picture"))

    # TikTok's official API is the source of truth. This Postiz build has no
    # public creator-info route (404) and no getCreatorInfo method (500).
    stored = tiktok_tokens_from_postiz_db(iid)
    token = stored.get("token") or token_from_postiz_db(iid)
    refresh_token = stored.get("refresh_token") or ""

    async def creator_from_access_token(access_token: str) -> Optional[Dict[str, Any]]:
        try:
            payload = await query_tiktok_creator_info(access_token)
        except PostizAPIError as exc:
            if is_tiktok_auth_failure(exc.body or str(exc)):
                return None
            logger.info("[SOCIAL] Direct TikTok creator_info failed: %s", exc)
            return None
        return _usable_creator_info(
            payload,
            fallback_name=fallback_name,
            fallback_username=fallback_username,
            fallback_avatar=fallback_avatar,
        )

    if token:
        info = await creator_from_access_token(token)
        if info:
            return info

    # Access tokens expire after a day. Refresh and retry before asking the user to reconnect.
    if refresh_token:
        refreshed = await refresh_tiktok_access_token(refresh_token)
        if refreshed:
            new_access = _first_str(refreshed.get("access_token"))
            new_refresh = _first_str(refreshed.get("refresh_token")) or refresh_token
            expires_in = _as_int(refreshed.get("expires_in")) or 86400
            store_tiktok_tokens(iid, new_access, new_refresh, expires_in)
            info = await creator_from_access_token(new_access)
            if info:
                return info

    session_jwt = ""
    if postiz_email and postiz_password:
        try:
            session_jwt = await client.login_local_session(
                postiz_email, postiz_password
            )
        except PostizAPIError as exc:
            logger.info("[SOCIAL] Postiz session for creator_info failed: %s", exc)

    if session_jwt:
        # queryCreatorInfo is injected by patch-tiktok-scopes.sh. Do not call
        # getCreatorInfo — stock Postiz TikTok has no such method.
        try:
            info = _usable_creator_info(
                await client.call_integration_function(
                    session_jwt,
                    integration_id=iid,
                    name="queryCreatorInfo",
                    data={},
                ),
                fallback_name=fallback_name,
                fallback_username=fallback_username,
                fallback_avatar=fallback_avatar,
            )
            if info:
                return info
        except PostizAPIError as exc:
            logger.info("[SOCIAL] Postiz queryCreatorInfo failed: %s", exc)

    raise PostizAPIError(RECONNECT_TIKTOK_MESSAGE, status_code=502)


async def load_tiktok_publish_status(
    *,
    client: PostizClient,
    api_key: str,
    integration_id: str,
    publish_id: str = "",
    postiz_email: str = "",
    postiz_password: str = "",
) -> Dict[str, Any]:
    iid = (integration_id or "").strip()
    pid = (publish_id or "").strip()
    if pid:
        session_jwt = ""
        if postiz_email and postiz_password:
            try:
                session_jwt = await client.login_local_session(
                    postiz_email, postiz_password
                )
            except PostizAPIError:
                session_jwt = ""
        if session_jwt and iid:
            try:
                raw = await client.call_integration_function(
                    session_jwt,
                    integration_id=iid,
                    name="fetchPublishStatus",
                    data={"publish_id": pid},
                )
                return normalize_publish_status(raw)
            except PostizAPIError as exc:
                logger.info("[SOCIAL] Postiz fetchPublishStatus failed: %s", exc)
        token = token_from_postiz_db(iid) if iid else None
        if token:
            try:
                raw = await query_tiktok_publish_status(token, pid)
                return normalize_publish_status(raw)
            except PostizAPIError as exc:
                logger.info("[SOCIAL] Direct TikTok publish status failed: %s", exc)

    start, end = _recent_post_window()
    try:
        posts = await client.list_posts(api_key, start_date=start, end_date=end)
    except PostizAPIError as exc:
        logger.info("[SOCIAL] Postiz list posts failed: %s", exc)
        return normalize_publish_status({"status": "PROCESSING_DOWNLOAD"})

    matched = _posts_for_integration(posts, iid)
    if not matched:
        return normalize_publish_status({"status": "PROCESSING_DOWNLOAD"})
    latest = matched[0]
    status = normalize_publish_status(latest)
    if status["failed"]:
        db_error = latest_postiz_post_error(iid)
        if db_error:
            status["fail_reason"] = db_error
            status["message"] = db_error
    return status


def _recent_post_window() -> Tuple[str, str]:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return start, end


def _posts_for_integration(payload: Any, integration_id: str) -> List[Dict[str, Any]]:
    iid = (integration_id or "").strip()
    rows: List[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        inner = (
            payload.get("posts")
            or payload.get("items")
            or payload.get("data")
            or payload.get("value")
            or []
        )
        rows = inner if isinstance(inner, list) else []
    else:
        rows = []

    matched: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        integration = row.get("integration")
        row_iid = ""
        if isinstance(integration, dict):
            row_iid = str(integration.get("id") or "").strip()
            ident = str(integration.get("identifier") or "").lower()
        else:
            row_iid = str(row.get("integrationId") or row.get("integration_id") or "").strip()
            ident = str(row.get("identifier") or "").lower()
        if iid and row_iid and row_iid != iid:
            continue
        if ident and ident != "tiktok" and iid:
            continue
        matched.append(row)
    matched.sort(
        key=lambda row: _first_str(
            row.get("publishDate"),
            row.get("publish_date"),
            row.get("createdAt"),
            row.get("updatedAt"),
        ),
        reverse=True,
    )
    return matched
