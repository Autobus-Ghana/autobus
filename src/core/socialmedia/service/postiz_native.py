"""TikTok and YouTube connect and publish through their own APIs.

Postiz holds the TikTok and Google app credentials and returns the provider
OAuth URL. Publishing those two platforms goes to Postiz, which calls TikTok
and YouTube. Instagram, Facebook, and WhatsApp stay on Zernio.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from core.socialmedia.model.PostizOrganization import PostizOrganization
from core.socialmedia.service.postiz_api_service import (
    PostizAPIError,
    PostizClient,
    apply_tiktok_oauth_scopes,
    require_direct_provider_url,
    derive_postiz_password,
    normalize_postiz_integrations_list,
)
from core.socialmedia.service.postiz_org_service import PostizOrgService
from core.socialmedia.service.tiktok_direct_post import extract_publish_ids
from core.user.model.User import User
from utilities.crypto import encrypt_secret

logger = logging.getLogger(__name__)

# Digital marketing accounts that use the provider API instead of Zernio.
NATIVE_POST_PLATFORMS = frozenset({"tiktok", "youtube"})


def postiz_base_url() -> str:
    return os.getenv("POSTIZ_BASE_URL", "").strip()


def postiz_username_for_user(user: User) -> str:
    """Stable identifier for Postiz LOCAL password derivation."""
    for attr in ("fullname", "uname", "username"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    email = getattr(user, "email", None)
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0].strip() or user.id
    return str(user.id)


def login_credentials(user: User) -> Tuple[str, str]:
    email = (getattr(user, "email", None) or "").strip()
    password = derive_postiz_password(username=postiz_username_for_user(user))
    return email, password


def resolve_postiz_api_key(user_id: str, db: Session) -> Optional[str]:
    """User Postiz key, then the shared env key."""
    user_scoped_key = PostizOrgService(db).get_public_api_key_for_user(user_id)
    if user_scoped_key:
        return user_scoped_key
    return (
        os.getenv("POSTIZ_PUBLIC_API_KEY", "").strip()
        or os.getenv("POSTIZ_GLOBAL_PUBLIC_API_KEY", "").strip()
        or None
    )


async def ensure_postiz_api_key(user_id: str, db: Session) -> Optional[str]:
    existing_key = resolve_postiz_api_key(user_id, db)
    if existing_key:
        return existing_key

    base_url = postiz_base_url()
    if not base_url:
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None

    company_name = (user.company or postiz_username_for_user(user) or "Autobus Client").strip()
    password = derive_postiz_password(username=postiz_username_for_user(user))
    client = PostizClient(base_url=base_url)
    postiz_org_id, postiz_api_key = await client.provision_org_and_get_public_api_key(
        email=user.email,
        company=company_name,
        password=password,
    )
    mapping = PostizOrganization(
        id=f"po_{str(uuid.uuid4())[:12]}",
        user_id=user.id,
        postiz_org_id=postiz_org_id,
        postiz_public_api_key_encrypted=encrypt_secret(postiz_api_key) or postiz_api_key,
    )
    db.add(mapping)
    db.commit()
    return postiz_api_key


def list_native_integrations_checked(
    user_id: str, db: Session
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Connected TikTok and YouTube accounts, plus an error when Postiz could not be read."""
    base_url = postiz_base_url()
    api_key = resolve_postiz_api_key(user_id, db)
    if not base_url or not api_key:
        return [], None
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(
                f"{base_url.rstrip('/')}/api/public/v1/integrations",
                headers={"Authorization": api_key},
            )
        if res.status_code >= 400:
            logger.warning("[SOCIAL] Postiz list integrations failed (%s)", res.status_code)
            return [], "Could not verify TikTok and YouTube accounts. Try again."
        payload = res.json() if res.text.strip() else []
    except Exception as exc:
        logger.warning("[SOCIAL] Postiz list integrations failed: %s", exc)
        return [], "Could not verify TikTok and YouTube accounts. Try again."

    rows: List[Dict[str, Any]] = []
    for row in normalize_postiz_integrations_list(payload):
        ident = str(row.get("identifier") or row.get("providerIdentifier") or "").strip().lower()
        if ident not in NATIVE_POST_PLATFORMS:
            continue
        if row.get("disabled") is True:
            continue
        integration_id = str(row.get("id") or "").strip()
        if not integration_id:
            continue
        name = row.get("name") if isinstance(row.get("name"), str) else ""
        profile = row.get("profile") if isinstance(row.get("profile"), str) else None
        rows.append(
            {
                "id": integration_id,
                "name": name,
                "identifier": ident,
                "disabled": False,
                "profile": profile,
                "provider": "postiz",
            }
        )
    return rows, None


def list_native_integrations(user_id: str, db: Session) -> List[Dict[str, Any]]:
    """Connected TikTok and YouTube accounts from Postiz."""
    rows, _error = list_native_integrations_checked(user_id, db)
    return rows


async def build_native_connect(
    *,
    internal_user_id: str,
    db: Session,
    platform_upper: str,
    postiz_slug: str,
) -> Dict[str, Any]:
    """OAuth URL on tiktok.com or accounts.google.com, via Postiz."""
    base_url = postiz_base_url()
    if not base_url:
        raise PostizAPIError("POSTIZ_BASE_URL is not configured", status_code=400)
    try:
        api_key = await ensure_postiz_api_key(internal_user_id, db)
    except Exception as postiz_error:
        logger.warning(
            "[SOCIAL] Postiz provisioning failed for user %s: %s",
            internal_user_id,
            postiz_error,
        )
        api_key = resolve_postiz_api_key(internal_user_id, db)

    browser_postiz_url = (os.getenv("POSTIZ_PUBLIC_URL", "").strip() or base_url).rstrip("/")
    if "chatwoot" in browser_postiz_url.lower():
        raise PostizAPIError(
            "POSTIZ_PUBLIC_URL points at Chatwoot. Set it to your Postiz URL "
            "(e.g. https://postiz.useautobus.com).",
            status_code=500,
        )

    provider_label = "TikTok" if postiz_slug == "tiktok" else "YouTube"
    if not api_key:
        raise PostizAPIError(
            f"No Postiz workspace for this account yet. "
            f"Subscribe or provision Postiz, then link {provider_label}.",
            status_code=404,
        )

    postiz_client = PostizClient(base_url=base_url)
    try:
        authorization_url = await postiz_client.get_social_connect_url(api_key, postiz_slug)
    except Exception as oauth_error:
        logger.warning(
            "[SOCIAL] Postiz direct OAuth for %s failed (user %s): %s",
            postiz_slug,
            internal_user_id,
            oauth_error,
        )
        if "timed out" in str(oauth_error).lower():
            raise PostizAPIError(
                f"{provider_label} connect is busy right now. Wait a few seconds and try again.",
                status_code=503,
            ) from oauth_error
        redirect = f"https://postiz.useautobus.com/integrations/social/{postiz_slug}"
        raise PostizAPIError(
            f"Could not start {provider_label} OAuth. "
            f"Configure {postiz_slug.upper()} client credentials on Postiz "
            f"(redirect URI {redirect}) and restart Postiz. Details: {oauth_error}",
            status_code=502,
        ) from oauth_error

    authorization_url = apply_tiktok_oauth_scopes(authorization_url, slug=postiz_slug)
    authorization_url = require_direct_provider_url(authorization_url, postiz_slug)
    if postiz_slug == "tiktok":
        message = "Redirect the user to authorize TikTok with TikTok."
    else:
        message = "Redirect the user to authorize YouTube with Google."
    return {
        "authorization_url": authorization_url,
        "platform": platform_upper,
        "provider": "TIKTOK" if postiz_slug == "tiktok" else "GOOGLE",
        "direct_oauth": True,
        "postiz_ready": True,
        "message": message,
    }


async def delete_native_integration(user_id: str, db: Session, integration_id: str) -> bool:
    iid = (integration_id or "").strip()
    if not iid:
        return False
    if not any(str(row.get("id") or "") == iid for row in list_native_integrations(user_id, db)):
        return False
    base_url = postiz_base_url()
    api_key = resolve_postiz_api_key(user_id, db)
    if not base_url or not api_key:
        return False
    await PostizClient(base_url).delete_integration(api_key, iid)
    return True


def _post_kind(post: Dict[str, Any]) -> str:
    settings = post.get("settings") if isinstance(post.get("settings"), dict) else {}
    kind = str(settings.get("__type") or "").strip().lower()
    if kind in {"x", "twitter"}:
        return "twitter"
    if kind == "instagram-standalone":
        return "instagram"
    if kind:
        return kind
    integration = post.get("integration") if isinstance(post.get("integration"), dict) else {}
    ident = str(integration.get("identifier") or "").strip().lower()
    if ident in {"x", "twitter"}:
        return "twitter"
    return ident


def classify_marketing_posts(
    db: Session,
    user_id: str,
    payload: Dict[str, Any],
) -> Tuple[List[Tuple[Any, Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a composer payload into Zernio posts, TikTok/YouTube posts, and unknown posts."""
    from core.socialmedia.service.zernio_mapping import (
        ZERNIO_MARKETING_PLATFORMS,
        ZERNIO_PLATFORMS,
    )
    from core.socialmedia.service.zernio_service import find_account

    zernio_posts: List[Tuple[Any, Dict[str, Any]]] = []
    native_posts: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for post in payload.get("posts") or []:
        if not isinstance(post, dict):
            continue
        kind = _post_kind(post)
        if kind in NATIVE_POST_PLATFORMS:
            native_posts.append(post)
            continue
        integration = post.get("integration") if isinstance(post.get("integration"), dict) else {}
        account = find_account(db, user_id, str(integration.get("id") or ""))
        allowed = kind in ZERNIO_MARKETING_PLATFORMS or kind == "instagram"
        if (
            account
            and account.platform in ZERNIO_PLATFORMS
            and account.platform == ("twitter" if kind == "twitter" else kind)
            and allowed
        ):
            zernio_posts.append((account, post))
        else:
            unknown.append(post)
    return zernio_posts, native_posts, unknown


async def publish_marketing_posts(
    db: Session,
    user_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Publish Facebook and Instagram through Zernio, and TikTok/YouTube through Postiz."""
    from core.socialmedia.service.zernio_client import ZernioAPIError
    from core.socialmedia.service.zernio_service import publish_targets, zernio_enabled

    zernio_targets, native_posts, unknown = classify_marketing_posts(db, user_id, payload)
    if unknown:
        raise ZernioAPIError(
            "One or more selected accounts are not connected. Link them again from Marketing.",
            status_code=400,
        )
    if not zernio_targets and not native_posts:
        raise ZernioAPIError("Choose at least one account to post to.", status_code=400)

    result: Dict[str, Any] = {}
    publish_ids: List[str] = []
    if zernio_targets:
        if not zernio_enabled():
            raise ZernioAPIError(
                "ZERNIO_API_KEY is not configured. Instagram and Facebook post through Zernio.",
                status_code=400,
            )
        schedule = str(payload.get("type") or "").strip().lower() == "schedule"
        result["zernio"] = publish_targets(
            db,
            user_id,
            zernio_targets,
            publish_now=not schedule,
            scheduled_for=str(payload.get("date") or "") if schedule else None,
        )
    if native_posts:
        base_url = postiz_base_url()
        api_key = resolve_postiz_api_key(user_id, db)
        if not base_url:
            raise PostizAPIError("POSTIZ_BASE_URL is not configured", status_code=400)
        if not api_key:
            raise PostizAPIError(
                "No Postiz API key found. Link TikTok or YouTube again from Marketing.",
                status_code=404,
            )
        native_payload = {**payload, "posts": native_posts}
        created = await PostizClient(base_url).create_post(api_key, native_payload)
        result["postiz"] = created
        publish_ids = extract_publish_ids(created)
    result["tiktok_publish_ids"] = publish_ids
    return result
