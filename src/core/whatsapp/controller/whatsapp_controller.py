"""WhatsApp Cloud API connect routes (Meta Embedded Signup / Business App onboard)."""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
from typing import List, Optional
from another_fastapi_jwt_auth import AuthJWT
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.chatwoot.controller.chatwoot_controller import resolve_internal_user_id
from core.socialmedia.service.zernio_client import ZernioAPIError
from core.socialmedia.service.zernio_service import (
    connect_authorization,
    disconnect_account as disconnect_zernio_account,
    list_accounts as list_zernio_accounts,
    sync_accounts_from_zernio as sync_zernio_accounts,
)
from core.user.model.User import User
from core.whatsapp.model.WhatsAppAccount import WhatsAppAccount
from core.whatsapp.service.meta_embedded_signup_service import (
    MetaWhatsAppOAuthState,
    MetaWhatsAppService,
)
from utilities.dbconfig import get_db

logger = logging.getLogger(__name__)

whatsapp_routes = APIRouter()


def validate_token(authjwt: AuthJWT = Depends()) -> str:
    try:
        authjwt.jwt_required()
        return authjwt.get_jwt_subject()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


class WhatsAppAccountResponse(BaseModel):
    id: str
    waba_id: str
    phone_number_id: str
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    is_active: bool
    webhook_subscribed: bool
    phone_registered: bool

    class Config:
        from_attributes = True


class WhatsAppConnectResponse(BaseModel):
    authorization_url: str
    provider: str = "META_WHATSAPP"
    state: str
    redirect_uri: str
    message: str = (
        "Open authorization_url in a browser to link WhatsApp. "
        "Autobus launches Meta Embedded Signup via the Facebook JS SDK on your domain."
    )


class WhatsAppCompleteRequest(BaseModel):
    code: str = Field(..., min_length=8)
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    business_id: Optional[str] = None
    state: Optional[str] = None


def _frontend_base() -> str:
    return (os.getenv("BASE_FRONTEND_URL") or "https://useautobus.com").rstrip("/")


def _upsert_account(
    db: Session,
    *,
    user_id: str,
    waba_id: str,
    phone_number_id: str,
    access_token: str,
    display_phone: Optional[str],
    verified_name: Optional[str],
    business_id: Optional[str],
    webhook_subscribed: bool,
    phone_registered: bool,
) -> WhatsAppAccount:
    svc = MetaWhatsAppService()
    existing = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.phone_number_id == phone_number_id)
        .first()
    )
    if existing and existing.user_id != user_id:
        if existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This WhatsApp number is already linked to another Autobus account.",
            )
        logger.info(
            "[WA] reclaiming unlinked phone_number_id=%s from user=%s to user=%s",
            phone_number_id,
            existing.user_id,
            user_id,
        )
        existing.user_id = user_id

    token_enc = svc.encrypt_token(access_token)
    if existing:
        existing.waba_id = waba_id
        existing.access_token_encrypted = token_enc
        existing.display_phone_number = display_phone or existing.display_phone_number
        existing.verified_name = verified_name or existing.verified_name
        existing.business_id = business_id or existing.business_id
        existing.is_active = True
        existing.webhook_subscribed = webhook_subscribed
        existing.phone_registered = phone_registered
        db.commit()
        db.refresh(existing)
        return existing

    row = WhatsAppAccount(
        id=svc.new_account_id(),
        user_id=user_id,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        display_phone_number=display_phone,
        verified_name=verified_name,
        business_id=business_id,
        access_token_encrypted=token_enc,
        is_active=True,
        webhook_subscribed=webhook_subscribed,
        phone_registered=phone_registered,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def complete_onboarding(
    db: Session,
    *,
    user_id: str,
    code: str,
    waba_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    business_id: Optional[str] = None,
) -> WhatsAppAccount:
    svc = MetaWhatsAppService()
    token_data = svc.exchange_code(code)
    access_token = token_data["access_token"]
    waba_id, phone_number_id, display, name = svc.resolve_waba_and_phone(
        access_token,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
    )
    subscribed = svc.subscribe_webhooks(access_token, waba_id)
    registered = svc.register_phone(access_token, phone_number_id)
    return _upsert_account(
        db,
        user_id=user_id,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        access_token=access_token,
        display_phone=display,
        verified_name=name,
        business_id=business_id,
        webhook_subscribed=subscribed,
        phone_registered=registered,
    )


def _result_html(
    *,
    title: str,
    heading: str,
    body_html: str,
    return_to: str = "web",
    error: bool = False,
    extra_script: str = "",
) -> str:
    to_app = MetaWhatsAppOAuthState.normalize_return_to(return_to) == "app"
    frontend = _frontend_base()
    bg = "#140b0b" if error else "#0b0f0c"
    card_bg = "#201010" if error else "#102016"
    border = "#4a1f1f" if error else "#1f3d2a"
    link = "#ff8a80" if error else "#69f0ae"
    if to_app:
        # Do not auto-navigate to autobus:// — in-app browsers (Safari View /
        # Chrome Custom Tabs) treat that custom scheme as a broken page.
        primary_href = ""
        primary_label = ""
        fallback_note = (
            "<p class=\"muted\">Tap the <strong>X</strong> at the top right "
            "to return to Autobus. You should see <strong>Linking channel</strong>, "
            "then <strong>WhatsApp connected</strong>.</p>"
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
body{{font-family:system-ui,sans-serif;background:{bg};color:#e8f5e9;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{max-width:420px;padding:28px;border:1px solid {border};border-radius:16px;background:{card_bg}}}
h1{{font-size:1.25rem;margin:0 0 8px}} p{{opacity:.85;line-height:1.45}}
a{{color:{link}}}
.muted{{opacity:.7;font-size:14px;line-height:1.45}}
.btn{{display:inline-block;margin-top:8px;padding:12px 16px;border-radius:10px;
background:#1877f2;color:#fff;text-decoration:none;font-weight:600}}
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


def _success_html(account: WhatsAppAccount, *, return_to: str = "web") -> str:
    phone = html_lib.escape(
        str(account.display_phone_number or account.phone_number_id or ""),
        quote=True,
    )
    phone_id_js = json.dumps(str(account.phone_number_id or ""))
    return _result_html(
        title="WhatsApp linked",
        heading="WhatsApp connected",
        body_html=(
            f"<p>Number <strong>{phone}</strong> is linked to Autobus.</p>"
            "<p>You can return to Autobus.</p>"
        ),
        return_to=return_to,
        error=False,
        extra_script=(
            "try{window.opener&&window.opener.postMessage("
            f"{{type:'AUTOBUS_WHATSAPP_LINKED',phone_number_id:{phone_id_js}}},'*');"
            "}catch(e){}"
        ),
    )


def _error_html(message: str, *, return_to: str = "web") -> str:
    safe = html_lib.escape(message or "Authorization failed", quote=True)
    return _result_html(
        title="WhatsApp link failed",
        heading="WhatsApp link failed",
        body_html=f"<p>{safe}</p>",
        return_to=return_to,
        error=True,
    )


def _embedded_signup_launch_html(
    *,
    app_id: str,
    config_id: str,
    state: str,
    extras_json: str,
    redirect_uri: str,
    graph_version: str = "v21.0",
) -> str:
    """Hosted Facebook JS SDK bridge — Meta's supported Embedded Signup launch path.

    Do not send the user to the Facebook OAuth dialog. That dialog requires
    redirect_uri to be listed under Valid OAuth Redirect URIs, and this app
    only whitelists the Postiz Facebook callback. Embedded Signup uses
    FB.login with config_id on the JavaScript SDK domain instead.
    """
    app_id_js = json.dumps(app_id)
    config_id_js = json.dumps(config_id)
    state_js = json.dumps(state)
    extras_js = extras_json
    redirect_js = json.dumps(redirect_uri)
    version_js = json.dumps(graph_version)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta property="fb:app_id" content="{html_lib.escape(app_id, quote=True)}"/>
  <title>Link WhatsApp · Autobus</title>
  <style>
    body{{font-family:system-ui,sans-serif;background:#0b1020;color:#e8eaf6;margin:0;
      min-height:100vh;display:flex;align-items:center;justify-content:center}}
    .card{{max-width:440px;padding:28px;border:1px solid #3d2a55;border-radius:16px;background:#161022}}
    button{{background:#1877f2;color:#fff;border:0;border-radius:10px;padding:12px 18px;
      font-size:15px;font-weight:600;cursor:pointer;width:100%}}
    button:disabled{{opacity:.6;cursor:default}}
    .muted{{opacity:.7;font-size:13px;line-height:1.45}}
    .err{{color:#ff8a80;margin-top:12px;font-size:13px}}
  </style>
</head>
<body>
  <div class="card">
    <h1 style="margin:0 0 8px;font-size:20px">Link WhatsApp</h1>
    <p class="muted">Sign in with Meta to connect your WhatsApp Business number to Autobus.</p>
    <button id="btn" type="button">Continue with Meta</button>
    <p id="status" class="muted" style="margin-top:14px">Tap Continue with Meta to open Facebook.</p>
    <p id="err" class="err" hidden></p>
  </div>
  <script>
    const APP_ID = {app_id_js};
    const CONFIG_ID = {config_id_js};
    const STATE = {state_js};
    const EXTRAS = {extras_js};
    const REDIRECT_URI = {redirect_js};
    const GRAPH_VERSION = {version_js};
    const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent || '')
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    let session = {{ waba_id: null, phone_number_id: null, business_id: null }};

    function setStatus(t) {{
      const el = document.getElementById('status');
      if (el) el.textContent = t;
    }}
    function setErr(t) {{
      const el = document.getElementById('err');
      if (!el) return;
      if (!t) {{ el.hidden = true; el.textContent = ''; return; }}
      el.hidden = false; el.textContent = t;
    }}

    function finishWithCode(code) {{
      if (!code) {{
        setErr('Meta did not return an authorization code. Try again.');
        document.getElementById('btn').disabled = false;
        setStatus('Ready when you are.');
        return;
      }}
      const u = new URL(REDIRECT_URI);
      u.searchParams.set('code', code);
      u.searchParams.set('state', STATE);
      if (session.waba_id) u.searchParams.set('waba_id', session.waba_id);
      if (session.phone_number_id) u.searchParams.set('phone_number_id', session.phone_number_id);
      if (session.business_id) u.searchParams.set('business_id', session.business_id);
      setStatus('Finishing WhatsApp link…');
      window.location.replace(u.toString());
    }}

    window.addEventListener('message', function (event) {{
      if (!event.origin || event.origin.indexOf('facebook.com') === -1) return;
      let data = event.data;
      try {{ if (typeof data === 'string') data = JSON.parse(data); }} catch (e) {{ return; }}
      if (!data || data.type !== 'WA_EMBEDDED_SIGNUP') return;
      if (data.event === 'FINISH' || data.event === 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING') {{
        const d = data.data || {{}};
        session.waba_id = d.waba_id || session.waba_id;
        session.phone_number_id = d.phone_number_id || session.phone_number_id;
        session.business_id = d.business_id || session.business_id;
      }} else if (data.event === 'CANCEL' || data.event === 'ERROR') {{
        setErr((data.data && (data.data.error_message || data.data.error_id)) || 'Signup was cancelled.');
        document.getElementById('btn').disabled = false;
        setStatus('You can try again.');
      }}
    }});

    function launchSignup() {{
      setErr('');
      document.getElementById('btn').disabled = true;
      setStatus('Opening Meta WhatsApp signup…');
      if (!window.FB) {{
        setErr('Facebook did not load. Disable content blockers for this page and try again.');
        document.getElementById('btn').disabled = false;
        setStatus('Ready when you are.');
        return;
      }}
      // Embedded Signup must use FB.login + config_id. A Facebook OAuth
      // dialog with redirect_uri is what triggers "not whitelisted in
      // Client OAuth Settings" — do not fall back to that.
      FB.login(function (response) {{
        const code = response && response.authResponse && response.authResponse.code;
        if (code) {{
          finishWithCode(code);
          return;
        }}
        setErr('Meta login did not complete. If you closed the popup, tap Continue again.');
        document.getElementById('btn').disabled = false;
        setStatus('Ready when you are.');
      }}, {{
        config_id: CONFIG_ID,
        response_type: 'code',
        override_default_response_type: true,
        extras: EXTRAS
      }});
    }}

    window.fbAsyncInit = function () {{
      FB.init({{
        appId: APP_ID,
        autoLogAppEvents: true,
        xfbml: true,
        version: GRAPH_VERSION
      }});
      setStatus('Ready — tap Continue with Meta.');
      document.getElementById('btn').disabled = false;
      // Auto-launch on Android/desktop only. iOS needs a user tap.
      if (!IS_IOS) setTimeout(launchSignup, 400);
    }};

    document.getElementById('btn').disabled = false;
    document.getElementById('btn').addEventListener('click', launchSignup);
    if (IS_IOS) {{
      setStatus('Tap Continue with Meta to open Facebook.');
    }}
  </script>
  <script async defer crossorigin="anonymous"
    src="https://connect.facebook.net/en_US/sdk.js"></script>
</body>
</html>"""


@whatsapp_routes.get("/connect", response_model=WhatsAppConnectResponse)
async def whatsapp_connect(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    return_to: str = Query(
        "web",
        description="After Meta returns to the HTTPS callback, send users to the mobile app (`app`) or website (`web`).",
    ),
    launch: str = Query(
        "sdk",
        description=(
            "sdk = Facebook JS SDK on the Meta-whitelisted callback URL. "
            "redirect is accepted for older app builds but uses the same SDK URL."
        ),
    ),
    raw_meta: bool = Query(
        False,
        description="Ignored (kept so older app builds that pass it still work).",
    ),
):
    """Return a Zernio connect URL for WhatsApp inbox messaging."""
    _ = (launch, raw_meta)
    try:
        user_id = resolve_internal_user_id(db, jwt_subject)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        payload = connect_authorization(db, user, "whatsapp", return_to=return_to)
        return WhatsAppConnectResponse(
            authorization_url=payload["authorization_url"],
            provider="ZERNIO",
            state=payload.get("state") or "",
            redirect_uri=payload.get("redirect_uri") or "",
            message=payload.get("message")
            or "Open authorization_url to link WhatsApp. Inbox messaging goes through Zernio.",
        )
    except ZernioAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code if exc.status_code < 500 else 502,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[WA] connect failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@whatsapp_routes.get("/embedded-signup/launch")
async def whatsapp_embedded_signup_launch(
    state: str = Query(..., min_length=8),
    go: bool = Query(
        False,
        description="Ignored (kept so older app builds that pass go=1 still work).",
    ),
):
    """
    Canonical Embedded Signup start is META_WHATSAPP_REDIRECT_URI (Client OAuth).
    This path 302s there so Facebook never sees /embedded-signup/launch as redirect_uri.
    """
    _ = go
    svc = MetaWhatsAppService()
    try:
        svc.require_config()
    except ValueError:
        return _whatsapp_launch_page(state)
    return RedirectResponse(url=svc.build_launch_bridge_url(state), status_code=302)


def _whatsapp_launch_page(state: str) -> HTMLResponse:
    payload = MetaWhatsAppOAuthState.peek_payload(state)
    return_to = (payload or {}).get("return_to") or "web"
    if not payload:
        return HTMLResponse(
            _error_html(
                "This WhatsApp link expired or is invalid. "
                "Go back to Autobus → Manage Channels → Link WhatsApp and try again.",
                return_to=return_to,
            ),
            status_code=400,
        )
    svc = MetaWhatsAppService()
    try:
        svc.require_config()
    except ValueError as exc:
        return HTMLResponse(_error_html(str(exc), return_to=return_to), status_code=500)
    return HTMLResponse(
        _embedded_signup_launch_html(
            app_id=svc.app_id,
            config_id=svc.config_id,
            state=state,
            extras_json=json.dumps(svc.embedded_signup_extras()),
            redirect_uri=svc.redirect_uri,
            graph_version=svc.graph_version(),
        )
    )

@whatsapp_routes.get("/accounts", response_model=List[WhatsAppAccountResponse])
async def list_whatsapp_accounts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    rows = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.user_id == user_id, WhatsAppAccount.is_active.is_(True))
        .all()
    )
    listed = [WhatsAppAccountResponse.model_validate(r) for r in rows]
    try:
        sync_zernio_accounts(db, user_id)
    except Exception as exc:
        logger.warning("[WA] Zernio account sync failed: %s", exc)
        db.rollback()
    for account in list_zernio_accounts(db, user_id, "whatsapp"):
        phone = (account.username or account.platform_user_id or "").strip()
        name = (account.display_name or "").strip()
        listed.append(
            WhatsAppAccountResponse(
                id=account.id,
                waba_id=account.zernio_account_id,
                phone_number_id=account.platform_user_id or account.zernio_account_id,
                display_phone_number=phone or None,
                verified_name=name if name and name != phone else None,
                is_active=account.is_active,
                webhook_subscribed=account.messaging_enabled,
                phone_registered=True,
            )
        )
    return listed


@whatsapp_routes.delete("/accounts/{account_id}")
async def disconnect_whatsapp_account(
    account_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    if disconnect_zernio_account(db, user_id, account_id):
        return {"status": "ok", "message": "WhatsApp account disconnected", "provider": "zernio"}
    row = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.id == account_id, WhatsAppAccount.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    db.delete(row)
    db.commit()
    return {"status": "ok", "message": "WhatsApp account disconnected"}


@whatsapp_routes.post("/embedded-signup/complete", response_model=WhatsAppAccountResponse)
async def whatsapp_embedded_signup_complete(
    body: WhatsAppCompleteRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Complete Embedded Signup when the client receives code via FB JS SDK."""
    user_id = resolve_internal_user_id(db, jwt_subject)
    if body.state:
        state_user = MetaWhatsAppOAuthState.validate(body.state)
        if not state_user or state_user != user_id:
            raise HTTPException(status_code=400, detail="Invalid or expired state")
    try:
        account = complete_onboarding(
            db,
            user_id=user_id,
            code=body.code,
            waba_id=body.waba_id,
            phone_number_id=body.phone_number_id,
            business_id=body.business_id,
        )
        return WhatsAppAccountResponse.model_validate(account)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[WA] embedded signup complete failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@whatsapp_routes.get("/callback", response_class=HTMLResponse)
async def whatsapp_meta_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    error_message: Optional[str] = Query(None),
    waba_id: Optional[str] = Query(None),
    phone_number_id: Optional[str] = Query(None),
    business_id: Optional[str] = Query(None),
):
    """
    Meta redirect target for WhatsApp Business App onboarding.

    Also mounted at /api/social/callback to match META_WHATSAPP_REDIRECT_URI.
    """
    early_return_to = "web"
    if state:
        peeked = MetaWhatsAppOAuthState.peek_payload(state)
        if peeked:
            early_return_to = peeked.get("return_to") or "web"

    if error or error_message:
        msg = error_description or error_message or error or "Authorization failed"
        if state:
            MetaWhatsAppOAuthState.validate_payload(state)
        return HTMLResponse(_error_html(msg, return_to=early_return_to), status_code=400)

    if not code:
        # Start Embedded Signup on this same URI so Facebook's redirect_uri
        # matches Client OAuth Settings (META_WHATSAPP_REDIRECT_URI).
        if state:
            return _whatsapp_launch_page(state)
        return HTMLResponse(
            _error_html("Missing authorization code from Meta.", return_to=early_return_to),
            status_code=400,
        )

    if not state:
        return HTMLResponse(
            _error_html(
                "Missing state. Start linking again from Autobus Manage Channels.",
                return_to=early_return_to,
            ),
            status_code=400,
        )

    payload = MetaWhatsAppOAuthState.validate_payload(state)
    if not payload:
        return HTMLResponse(
            _error_html(
                "Invalid or expired link session. Please try linking again.",
                return_to=early_return_to,
            ),
            status_code=400,
        )
    user_id = payload["user_id"]
    return_to = payload.get("return_to") or "web"

    q = request.query_params
    waba_id = waba_id or q.get("waba_id") or q.get("whatsapp_business_account_id")
    phone_number_id = (
        phone_number_id or q.get("phone_number_id") or q.get("phone_number_ids")
    )
    business_id = business_id or q.get("business_id") or q.get("businessId")

    try:
        account = complete_onboarding(
            db,
            user_id=user_id,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            business_id=business_id,
        )
        return HTMLResponse(_success_html(account, return_to=return_to))
    except Exception as exc:
        logger.exception("[WA] meta callback failed")
        return HTMLResponse(_error_html(str(exc), return_to=return_to), status_code=400)


# Dedicated mount for Meta's configured redirect_uri:
# https://useautobus.com/api/social/callback
meta_whatsapp_callback_routes = APIRouter()


@meta_whatsapp_callback_routes.get("/social/callback", response_class=HTMLResponse)
async def meta_social_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    error_message: Optional[str] = Query(None),
    error_reason: Optional[str] = Query(None),
    waba_id: Optional[str] = Query(None),
    phone_number_id: Optional[str] = Query(None),
    business_id: Optional[str] = Query(None),
):
    """
    Shared Meta redirect URI for WhatsApp Embedded Signup and Instagram Business Login.

    Instagram states are prefixed with ``ig.``; everything else uses the WhatsApp handler.
    """
    from core.instagram.controller.instagram_controller import (
        handle_instagram_oauth_callback,
    )
    from core.instagram.service.instagram_oauth_service import InstagramOAuthState

    if InstagramOAuthState.is_instagram_state(state):
        return await handle_instagram_oauth_callback(
            db=db,
            code=code,
            state=state,
            error=error,
            error_description=error_description,
            error_reason=error_reason or error_message,
        )

    return await whatsapp_meta_callback(
        request=request,
        db=db,
        code=code,
        state=state,
        error=error,
        error_description=error_description,
        error_message=error_message,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        business_id=business_id,
    )
