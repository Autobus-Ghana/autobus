"""Instagram Business Login connect / accounts / callback helpers."""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from another_fastapi_jwt_auth import AuthJWT
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.chatwoot.controller.chatwoot_controller import resolve_internal_user_id
from core.instagram.model.InstagramAccount import InstagramAccount
from core.instagram.service.instagram_oauth_service import (
    InstagramOAuthService,
    InstagramOAuthState,
)
from core.user.model.User import User
from core.instagram.service.instagram_publish_service import InstagramPublishService
from core.socialmedia.service.zernio_client import ZernioAPIError
from core.socialmedia.service.zernio_service import (
    connect_authorization,
    disconnect_account as disconnect_zernio_account,
    find_account as find_zernio_account,
    list_accounts as list_zernio_accounts,
    publish_instagram,
)
from utilities.dbconfig import get_db

logger = logging.getLogger(__name__)

instagram_routes = APIRouter()


def validate_token(authjwt: AuthJWT = Depends()) -> str:
    try:
        authjwt.jwt_required()
        return authjwt.get_jwt_subject()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


class InstagramAccountResponse(BaseModel):
    id: str
    ig_user_id: str
    username: Optional[str] = None
    name: Optional[str] = None
    account_type: Optional[str] = None
    profile_picture_url: Optional[str] = None
    is_active: bool
    messaging_enabled: bool
    publishing_enabled: bool
    permissions: Optional[str] = None

    class Config:
        from_attributes = True


class InstagramConnectResponse(BaseModel):
    authorization_url: str
    provider: str = "ZERNIO"
    state: str
    redirect_uri: str
    message: str = (
        "Open authorization_url to link Instagram. "
        "Inbox messaging and Digital Marketing posting go through Zernio."
    )


class InstagramPublishRequest(BaseModel):
    account_id: str = Field(..., description="Autobus Instagram account UUID")
    caption: str = Field("", description="Post caption / text")
    media_urls: List[str] = Field(
        default_factory=list,
        description="Public HTTPS image/video URLs (required for Instagram)",
    )


class InstagramPublishResponse(BaseModel):
    success: bool = True
    post_id: str
    creation_id: Optional[str] = None
    media_type: Optional[str] = None
    account_id: str
    message: str = "Published to Instagram"


def _frontend_base() -> str:
    return (os.getenv("BASE_FRONTEND_URL") or "https://useautobus.com").rstrip("/")


def _upsert_account(
    db: Session,
    *,
    user_id: str,
    ig_user_id: str,
    access_token: str,
    username: Optional[str],
    name: Optional[str],
    account_type: Optional[str],
    profile_picture_url: Optional[str],
    permissions: Optional[str],
    token_expires_at: Optional[datetime],
) -> InstagramAccount:
    svc = InstagramOAuthService()
    existing = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.ig_user_id == ig_user_id)
        .first()
    )
    if existing and existing.user_id != user_id:
        if existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This Instagram account is already linked to another Autobus account.",
            )
        # Soft-unlinked leftovers still occupy uq_instagram_ig_user_id.
        logger.info(
            "[IG] reclaiming unlinked ig_user_id=%s from user=%s to user=%s",
            ig_user_id,
            existing.user_id,
            user_id,
        )
        existing.user_id = user_id

    token_enc = svc.encrypt_token(access_token)
    if existing:
        existing.username = username or existing.username
        existing.name = name or existing.name
        existing.account_type = account_type or existing.account_type
        existing.profile_picture_url = profile_picture_url or existing.profile_picture_url
        existing.permissions = permissions or existing.permissions
        existing.access_token_encrypted = token_enc
        existing.token_expires_at = token_expires_at
        existing.is_active = True
        existing.messaging_enabled = True
        existing.publishing_enabled = True
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(existing)
        return existing

    row = InstagramAccount(
        id=str(uuid.uuid4()),
        user_id=user_id,
        ig_user_id=ig_user_id,
        username=username,
        name=name,
        account_type=account_type,
        profile_picture_url=profile_picture_url,
        permissions=permissions,
        access_token_encrypted=token_enc,
        token_expires_at=token_expires_at,
        is_active=True,
        messaging_enabled=True,
        publishing_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def complete_instagram_onboarding(db: Session, *, user_id: str, code: str) -> InstagramAccount:
    svc = InstagramOAuthService()
    short = svc.exchange_code(code)
    short_token = short["access_token"]
    permissions = svc.permissions_list(short.get("permissions") or short.get("scope"))
    user_id_from_token = short.get("user_id")

    long_data = svc.exchange_long_lived(short_token)
    access_token = str(long_data.get("access_token") or "").strip()
    expires_at = svc.token_expiry(long_data.get("expires_in"))
    if not access_token or expires_at is None:
        raise RuntimeError("Instagram did not return a 60-day access token.")

    profile: dict = {}
    try:
        profile = svc.fetch_profile(
            access_token,
            ig_user_id=str(user_id_from_token or "").strip() or None,
        )
    except Exception as exc:
        logger.warning("[IG] profile fetch failed, using token user_id: %s", exc)

    # Messaging webhooks identify the business by the professional account id
    # (`user_id`), not the app-scoped `/me.id`.
    ig_user_id = str(
        profile.get("user_id") or user_id_from_token or profile.get("id") or ""
    ).strip()
    if not ig_user_id:
        raise RuntimeError("Instagram login succeeded but no user id was returned.")

    account = _upsert_account(
        db,
        user_id=user_id,
        ig_user_id=ig_user_id,
        access_token=access_token,
        username=profile.get("username"),
        name=profile.get("name"),
        account_type=profile.get("account_type"),
        profile_picture_url=profile.get("profile_picture_url"),
        permissions=",".join(permissions) if permissions else None,
        token_expires_at=expires_at,
    )
    try:
        svc.subscribe_webhooks(access_token, account.ig_user_id)
    except Exception as exc:
        logger.warning("[IG] webhook subscribe failed: %s", exc)
    return account


def _success_html(account: InstagramAccount, *, return_to: str = "web") -> str:
    label = account.username or account.name or account.ig_user_id
    safe_label = html_lib.escape(str(label or ""), quote=True)
    ig_user_id_js = json.dumps(str(account.ig_user_id or ""))
    return _result_html(
        title="Instagram linked",
        heading="Instagram linked",
        body_html=(
            f"<p>Connected <strong>@{safe_label}</strong> to Autobus for inbox "
            "messaging and Digital Marketing posting.</p>"
        ),
        return_to=return_to,
        error=False,
        extra_script=(
            "try{window.opener&&window.opener.postMessage("
            f"{{type:'AUTOBUS_INSTAGRAM_LINKED',ig_user_id:{ig_user_id_js}}},'*');"
            "}catch(e){}"
        ),
    )


def _error_html(message: str, *, return_to: str = "web") -> str:
    safe = html_lib.escape(message or "Authorization failed", quote=True)
    return _result_html(
        title="Instagram link failed",
        heading="Instagram link failed",
        body_html=f"<p>{safe}</p>",
        return_to=return_to,
        error=True,
    )


def _result_html(
    *,
    title: str,
    heading: str,
    body_html: str,
    return_to: str,
    error: bool,
    extra_script: str = "",
) -> str:
    to_app = InstagramOAuthState.normalize_return_to(return_to) == "app"
    frontend = _frontend_base()
    bg = "#140b0b" if error else "#0b1020"
    card_bg = "#201010" if error else "#161022"
    border = "#4a1f1f" if error else "#3d2a55"
    link = "#ff8a80" if error else "#f48fb1"
    if to_app:
        # Do not auto-navigate to autobus:// — in-app browsers (Safari View /
        # Chrome Custom Tabs) treat that custom scheme as a broken page.
        primary_href = ""
        primary_label = ""
        fallback_note = (
            "<p class=\"muted\">Tap the <strong>X</strong> at the top right "
            "to return to Autobus.</p>"
        )
        redirect_js = ""
    else:
        primary_href = f"{frontend}/"
        primary_label = "Back to Autobus"
        fallback_note = ""
        redirect_js = (
            f"setTimeout(function(){{window.location.replace({json.dumps(frontend + '/')});}},2500);"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{html_lib.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{font-family:system-ui,sans-serif;background:{bg};color:#e8eaf6;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{max-width:420px;padding:28px;border:1px solid {border};border-radius:16px;background:{card_bg}}}
a{{color:{link}}}
.muted{{opacity:.7;font-size:14px;line-height:1.45}}
.btn{{display:inline-block;margin-top:8px;padding:12px 16px;border-radius:10px;
background:#7c3aed;color:#fff;text-decoration:none;font-weight:600}}
</style></head>
<body><div class="card">
<h1>{html_lib.escape(heading)}</h1>
{body_html}
{fallback_note}
{f'<p><a class="btn" href="{html_lib.escape(primary_href, quote=True)}">{html_lib.escape(primary_label)}</a></p>' if primary_href else ""}
</div>
<script>{extra_script}
{redirect_js}
</script>
</body></html>"""


@instagram_routes.get("/connect", response_model=InstagramConnectResponse)
async def instagram_connect(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    return_to: str = Query(
        "web",
        description="After Instagram returns to the HTTPS callback, send users to the mobile app (`app`) or website (`web`).",
    ),
):
    """Return a Zernio connect URL for Instagram inbox and posting."""
    try:
        user_id = resolve_internal_user_id(db, jwt_subject)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        payload = connect_authorization(db, user, "instagram", return_to=return_to)
        return InstagramConnectResponse(
            authorization_url=payload["authorization_url"],
            provider="ZERNIO",
            state=payload.get("state") or "",
            redirect_uri=payload.get("redirect_uri") or "",
            message=payload.get("message")
            or "Open authorization_url to link Instagram. Inbox messaging and posting go through Zernio.",
        )
    except ZernioAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code < 500 else 502,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[IG] connect failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@instagram_routes.get("/accounts", response_model=List[InstagramAccountResponse])
async def list_instagram_accounts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    rows = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == user_id, InstagramAccount.is_active.is_(True))
        .all()
    )
    listed = [InstagramAccountResponse.model_validate(r) for r in rows]
    seen = {item.id for item in listed}
    for account in list_zernio_accounts(db, user_id, "instagram"):
        if account.id in seen:
            continue
        listed.append(
            InstagramAccountResponse(
                id=account.id,
                ig_user_id=account.platform_user_id or account.zernio_account_id,
                username=account.username,
                name=account.display_name or account.username,
                account_type="zernio",
                is_active=account.is_active,
                messaging_enabled=account.messaging_enabled,
                publishing_enabled=account.publishing_enabled,
                permissions="zernio",
            )
        )
    return listed


@instagram_routes.delete("/accounts/{account_id}")
async def disconnect_instagram_account(
    account_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    if disconnect_zernio_account(db, user_id, account_id):
        return {"status": "ok", "message": "Instagram account disconnected", "provider": "zernio"}
    row = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.id == account_id, InstagramAccount.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Instagram account not found")
    try:
        svc = InstagramOAuthService()
        token = svc.decrypt_token(row.access_token_encrypted)
        svc.unsubscribe_webhooks(token, row.ig_user_id)
    except Exception as exc:
        logger.warning("[IG] webhook unsubscribe on unlink failed: %s", exc)
    db.delete(row)
    db.commit()
    return {"status": "ok", "message": "Instagram account disconnected"}


@instagram_routes.post("/posts", response_model=InstagramPublishResponse)
async def publish_instagram_post(
    body: InstagramPublishRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Publish an image, carousel, or reel to a linked Instagram Business account."""
    user_id = resolve_internal_user_id(db, jwt_subject)
    account_id = (body.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    media = [u.strip() for u in (body.media_urls or []) if u and str(u).strip()]
    if not media:
        raise HTTPException(
            status_code=400,
            detail="Instagram requires at least one public media URL",
        )

    zernio_account = find_zernio_account(db, user_id, account_id)
    if zernio_account and zernio_account.platform == "instagram":
        try:
            result = publish_instagram(
                db,
                zernio_account,
                caption=body.caption or "",
                media_urls=media,
            )
        except ZernioAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code if exc.status_code < 500 else 502,
                detail=str(exc),
            ) from exc
        label = zernio_account.username or zernio_account.display_name or "Instagram"
        return InstagramPublishResponse(
            post_id=str(result.get("post_id") or ""),
            account_id=account_id,
            message=f"Published to Instagram (@{label})",
        )

    row = (
        db.query(InstagramAccount)
        .filter(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == user_id,
            InstagramAccount.is_active.is_(True),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Instagram account not found")

    media = [u.strip() for u in (body.media_urls or []) if u and str(u).strip()]
    if not media:
        raise HTTPException(
            status_code=400,
            detail="Instagram requires at least one public media URL",
        )

    try:
        result = InstagramPublishService().publish(
            row,
            caption=body.caption or "",
            media_urls=media,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[IG] publish failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return InstagramPublishResponse(
        post_id=str(result.get("post_id") or ""),
        creation_id=result.get("creation_id"),
        media_type=result.get("media_type"),
        account_id=account_id,
        message=f"Published to Instagram (@{row.username or row.ig_user_id})",
    )


async def handle_instagram_oauth_callback(
    *,
    db: Session,
    code: Optional[str],
    state: Optional[str],
    error: Optional[str],
    error_description: Optional[str],
    error_reason: Optional[str],
) -> HTMLResponse:
    """Shared callback body used by /api/social/callback dispatcher."""
    payload = InstagramOAuthState.consume(state)
    return_to = (payload or {}).get("return_to") or "web"

    if error or error_reason:
        msg = error_description or error_reason or error or "Authorization failed"
        return HTMLResponse(_error_html(msg, return_to=return_to), status_code=400)

    if not code:
        return HTMLResponse(
            _error_html(
                "Missing authorization code from Instagram.",
                return_to=return_to,
            ),
            status_code=400,
        )

    if not state:
        return HTMLResponse(
            _error_html(
                "Missing state. Start linking again from Autobus Manage Channels.",
                return_to=return_to,
            ),
            status_code=400,
        )

    user_id = (payload or {}).get("user_id") if payload else None
    if not user_id:
        return HTMLResponse(
            _error_html(
                "Invalid or expired Instagram link session. Please try linking again.",
                return_to=return_to,
            ),
            status_code=400,
        )

    try:
        account = complete_instagram_onboarding(db, user_id=user_id, code=code)
        return HTMLResponse(_success_html(account, return_to=return_to))
    except Exception as exc:
        logger.exception("[IG] oauth callback failed")
        return HTMLResponse(_error_html(str(exc), return_to=return_to), status_code=400)


@instagram_routes.get("/callback", response_class=HTMLResponse)
async def instagram_oauth_callback(
    db: Session = Depends(get_db),
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    error_reason: Optional[str] = Query(None),
):
    return await handle_instagram_oauth_callback(
        db=db,
        code=code,
        state=state,
        error=error,
        error_description=error_description,
        error_reason=error_reason,
    )
