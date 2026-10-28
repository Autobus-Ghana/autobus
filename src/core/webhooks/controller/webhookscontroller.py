from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Header
import json
import re
import hmac
import hashlib
from datetime import datetime
from typing import Any, Optional, Tuple
from core.webhooks.dto.response.simple_chat_response import SimpleChatResponse
from utilities.dbconfig import get_db
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
import logging
import os
from core.user.model.User import User
from core.nlu.nlu import get_nlu_system
from core.webhooks.service.whatsapp_service import WhatsAppService
from utilities.phone_utils import convert_to_local_ghana_format, normalize_ghana_phone_number
from core.auth.service.authservice import AuthService
from config import settings

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _nlu_reply_text(response_message: Optional[str]) -> str:
    return (response_message or "").strip()


def _send_whatsapp_nlu_reply(
    whatsapp_service: WhatsAppService,
    phone_id: str,
    phone: str,
    outbound: str,
    reply_images: Optional[list] = None,
) -> bool:
    """Send the text reply, then any catalog photos the turn queued."""
    sent = True
    if outbound:
        sent = whatsapp_service.send_message(
            phone_id=phone_id,
            recipient_phone=phone,
            message_text=outbound,
        )
    for image_url in reply_images or []:
        whatsapp_service.send_message_receipt(phone_id, phone, image_url)
    return sent


def _whatsapp_show_typing(whatsapp_service: WhatsAppService, phone_id: str, message: dict) -> None:
    """Mark the inbound WhatsApp message read and show typing while the AI runs."""
    message_id = (message or {}).get("id") if isinstance(message, dict) else None
    if not message_id:
        return
    try:
        whatsapp_service.send_typing_indicator(phone_id, str(message_id))
    except Exception as exc:
        logger.warning("WhatsApp typing indicator failed: %s", exc)


# Public routes: Meta uses verify_token / app secret; other helpers use X-Api-Key when configured.
webhooks_routes = APIRouter()


@webhooks_routes.post("/zernio")
async def zernio_webhook(request: Request, db: Session = Depends(get_db)):
    """Inbox, account, and post events from Zernio. One endpoint for every customer."""
    from core.socialmedia.service.zernio_service import (
        apply_account_event,
        claim_webhook_event,
        handle_inbox_message,
        webhook_signature_ok,
    )

    raw_body = await request.body()
    signature = request.headers.get("X-Zernio-Signature") or request.headers.get("x-zernio-signature")
    if not webhook_signature_ok(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid Zernio signature")
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(payload, dict):
        return {"ok": True}

    event = str(payload.get("event") or "")
    event_id = str(payload.get("id") or "")
    if not claim_webhook_event(db, event_id, event):
        return {"ok": True, "duplicate": True}

    if event == "account.connected":
        apply_account_event(db, payload, connected=True)
    elif event == "account.disconnected":
        apply_account_event(db, payload, connected=False)
    elif event == "message.received":
        return handle_inbox_message(db, payload)
    return {"ok": True, "event": event}


def _require_public_api_key(x_api_key: Optional[str] = Header(None, alias="X-Api-Key")) -> None:
    expected = (settings.PUBLIC_WEBHOOK_API_KEY or "").strip()
    if not expected:
        if settings.DEBUG:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PUBLIC_WEBHOOK_API_KEY is not configured",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def _meta_webhook_secrets() -> list:
    secrets = []
    for raw in (
        settings.META_APP_SECRET,
        os.getenv("META_APP_SECRET"),
        os.getenv("INSTAGRAM_APP_SECRET"),
    ):
        val = (raw or "").strip()
        if val and val not in secrets:
            secrets.append(val)
    return secrets


def _verify_meta_signature(raw_body: bytes, signature_header: Optional[str]) -> None:
    secrets = _meta_webhook_secrets()
    if not secrets:
        if settings.DEBUG:
            logger.warning("META_APP_SECRET not set; skipping Meta signature verification")
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="META_APP_SECRET is not configured",
        )
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing Meta signature")
    provided = signature_header.split("=", 1)[1].strip()
    for secret in secrets:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided):
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Meta signature")

@webhooks_routes.get("/start-dialog")
def verify_webhook(
    mode: Optional[str] = Query(None, alias="hub.mode"),
    challenge: Optional[str] = Query(None, alias="hub.challenge"),
    verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
):
    """
    Webhook verification endpoint for Meta (Facebook/WhatsApp) webhooks.
    Meta will send a GET request with hub.mode, hub.challenge, and hub.verify_token.

    Does not require application JWT authentication (Meta uses hub.verify_token).
    """
    expected_verify_token = os.getenv("VERIFY_TOKEN")

    if mode == "subscribe" and verify_token == expected_verify_token:
        logger.info("WEBHOOK VERIFIED")
        # Meta expects the raw challenge echoed back as plain text.
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(content=str(challenge or ""))
    else:
        logger.warning("Webhook verification failed")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verification failed"
        )

def _payload_str(payload: dict, *keys: str) -> str:
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _merchant_display_name(user: User) -> str:
    return (
        user.company
        or user.organization_workplace
        or user.fullname
        or user.id
        or ""
    ).strip()


def _company_match_score(user: User, needle: str) -> int:
    """Higher = better match for public company lookup."""
    n = needle.lower()
    fields = [
        (user.company, 100),
        (user.organization_workplace, 90),
        (user.fullname, 80),
    ]
    best = 0
    for raw, exact_weight in fields:
        val = (raw or "").strip().lower()
        if not val:
            continue
        if val == n:
            best = max(best, exact_weight)
        elif val.startswith(n):
            best = max(best, exact_weight - 15)
        elif n in val:
            best = max(best, exact_weight - 35)
    return best


def _find_company_matches(db: Session, name: str, *, limit: int = 8) -> list[User]:
    needle = name.strip().lower()
    if len(needle) < 2:
        return []

    like = f"%{needle}%"
    candidates = (
        db.query(User)
        .filter(
            or_(
                func.lower(User.company).like(like),
                func.lower(User.organization_workplace).like(like),
                func.lower(User.fullname).like(like),
            )
        )
        .limit(50)
        .all()
    )
    if not candidates:
        return []

    scored = [(u, _company_match_score(u, needle)) for u in candidates]
    scored = [(u, s) for u, s in scored if s > 0]
    scored.sort(key=lambda pair: (-pair[1], _merchant_display_name(pair[0]).lower()))

    # Single clear winner: exact match on company name, or lone high-confidence hit.
    if len(scored) == 1:
        return [scored[0][0]]

    top_score = scored[0][1]
    second_score = scored[1][1] if len(scored) > 1 else 0
    if top_score >= 100 and top_score - second_score >= 10:
        return [scored[0][0]]
    if top_score >= 90 and second_score < 70:
        return [scored[0][0]]

    return [u for u, _ in scored[:limit]]


def _resolve_company_number(
    db: Session,
    *,
    company_number: str = "",
    company_name: str = "",
) -> Tuple[str, Optional[str]]:
    """
    Resolve merchant ``users.id`` from an explicit id or a display name.
    Returns (company_id, error_message).
    """
    comp = (company_number or "").strip()
    if comp:
        merchant = db.query(User).filter(User.id == comp).first()
        if not merchant:
            return "", f"Unknown company_number: no merchant user with id '{comp}'."
        return comp, None

    name = (company_name or "").strip()
    if not name:
        return "", "company_number or company_name is required."

    matches = _find_company_matches(db, name)
    if not matches:
        return "", f"No business found matching '{name}'."
    if len(matches) > 1:
        labels = ", ".join(_merchant_display_name(u) for u in matches[:5])
        return "", f"Multiple businesses match '{name}': {labels}. Please pick one from the list."
    return matches[0].id, None


@webhooks_routes.get("/company-lookup")
def company_lookup(
    name: str = Query(..., min_length=2, max_length=200),
    db: Session = Depends(get_db),
    _: None = Depends(_require_public_api_key),
):
    """Marketing-site company lookup (requires X-Api-Key when configured)."""
    query = name.strip()
    matches = _find_company_matches(db, query)
    if not matches:
        return {"ok": False, "message": f"No business found matching '{query}'."}

    options = [
        {
            "company_number": u.id,
            "display_name": _merchant_display_name(u),
        }
        for u in matches
    ]

    if len(matches) == 1:
        u = matches[0]
        return {
            "ok": True,
            "requires_selection": False,
            "company_number": u.id,
            "display_name": _merchant_display_name(u),
            "matches": options,
        }

    return {
        "ok": True,
        "requires_selection": True,
        "message": f"Multiple businesses match '{query}'. Please select one.",
        "matches": options,
    }


@webhooks_routes.get("/agent-messages")
def poll_agent_messages(
    customer_number: str = Query(..., min_length=1),
    company_number: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _: None = Depends(_require_public_api_key),
):
    """
    Public chat bubble poll: return (and clear) agent intervention replies queued
    for ``{company_number}:{customer_number}``.
    """
    cust = (customer_number or "").strip()
    comp = (company_number or "").strip()
    if not cust or not comp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="customer_number and company_number are required",
        )

    merchant = db.query(User).filter(User.id == comp).first()
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown company_number",
        )

    from core.nlu.service.conversation_manager import ConversationManager

    nlu_user_id = f"{comp}:{cust}"
    cm = ConversationManager()
    messages = cm.flush_pending_customer_messages(nlu_user_id)
    return {"messages": messages, "count": len(messages)}


@webhooks_routes.post("/send-whatsapp-message")
async def send_whatsapp_message(
    request: Request,
    _: None = Depends(_require_public_api_key),
):
    """
    Marketing-site WhatsApp send helper. Requires X-Api-Key (PUBLIC_WEBHOOK_API_KEY).
    """
    payload = await request.json()
    raw_to = _payload_str(payload, "to", "customer_number", "phone", "recipient")
    if not raw_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Phone number (to / customer_number) is required.",
        )

    recipient = normalize_ghana_phone_number(raw_to)
    if not recipient or len(re.sub(r"\D", "", recipient)) < 8:
        # Fall back to digits-only for non-Ghana test numbers from Meta
        digits = re.sub(r"\D", "", raw_to)
        recipient = digits if len(digits) >= 8 else raw_to

    template_name = (
        _payload_str(payload, "template_name", "template")
        or os.getenv("WHATSAPP_DEMO_TEMPLATE_NAME")
        or "jaspers_market_order_confirmation_v1"
    )
    language_code = (
        _payload_str(payload, "language", "language_code")
        or os.getenv("WHATSAPP_DEMO_TEMPLATE_LANGUAGE")
        or "en_US"
    )

    body_params = payload.get("body_parameters") or payload.get("parameters")
    if not isinstance(body_params, list) or not body_params:
        body_params = [
            os.getenv("WHATSAPP_DEMO_PARAM_CUSTOMER", "John Doe"),
            os.getenv("WHATSAPP_DEMO_PARAM_ORDER", "123456"),
            os.getenv(
                "WHATSAPP_DEMO_PARAM_DATE",
                datetime.utcnow().strftime("%b %d, %Y"),
            ),
        ]

    whatsapp = WhatsAppService()
    ok, meta_response, err = whatsapp.send_template(
        recipient_phone=recipient,
        template_name=template_name,
        language_code=language_code,
        body_parameters=body_params,
    )

    if not ok:
        logger.error("Demo WhatsApp send failed for %s: %s", recipient, err)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=err or "Failed to send WhatsApp message.",
        )

    message_id = None
    if isinstance(meta_response, dict):
        messages = meta_response.get("messages") or []
        if messages and isinstance(messages[0], dict):
            message_id = messages[0].get("id")

    return {
        "ok": True,
        "to": recipient,
        "template": template_name,
        "language": language_code,
        "message_id": message_id,
        "message": (
            f"WhatsApp message sent to {recipient}. "
            "Open WhatsApp to confirm it arrived."
        ),
        "meta": meta_response,
    }


@webhooks_routes.post("/start-dialog")
async def start_dialog(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Handles incoming webhooks from either:
    1. Meta (Facebook/WhatsApp) webhooks - with 'object' and 'entry' fields
    2. Simple chat — preferred shape: ``customer_number``, ``company_number`` (merchant ``users.id``),
       ``message`` (requires X-Api-Key when PUBLIC_WEBHOOK_API_KEY is set)
    3. Legacy simple chat: ``userid`` + ``message``

    Meta payloads must present a valid X-Hub-Signature-256 when META_APP_SECRET is set.
    """
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Log the incoming webhook payload (avoid dumping PII at info in production)
    if settings.DEBUG:
        logger.info(f"Received webhook payload: {json.dumps(payload, indent=2)}")
    else:
        logger.info("Received webhook payload keys=%s", list(payload.keys()) if isinstance(payload, dict) else type(payload))
        if isinstance(payload, dict):
            entries = payload.get("entry") or []
            logger.info(
                "Webhook object=%s entries=%s",
                payload.get("object"),
                [
                    {
                        "id": e.get("id") if isinstance(e, dict) else None,
                        "keys": list(e.keys()) if isinstance(e, dict) else type(e).__name__,
                        "fields": [
                            c.get("field")
                            for c in (e.get("changes") or [])
                            if isinstance(c, dict)
                        ]
                        if isinstance(e, dict)
                        else [],
                    }
                    for e in entries[:5]
                ],
            )

    try:
        customer = _payload_str(payload, "customer_number", "customer_phone", "customer")
        company = _payload_str(payload, "company_number", "company_id", "merchant_id")
        company_name = _payload_str(
            payload, "company_name", "company", "business_name", "merchant_name"
        )
        msg = _payload_str(payload, "message", "webhook_message", "text", "body")

        if customer and msg and (company or company_name):
            _require_public_api_key(request.headers.get("X-Api-Key"))
            resolved_company, company_err = _resolve_company_number(
                db,
                company_number=company,
                company_name=company_name,
            )
            if company_err:
                return SimpleChatResponse(message=company_err)
            logger.info("Detected simple chat request (customer + company + message)")
            return await handle_simple_chat(
                customer_number=customer,
                company_number=resolved_company,
                message=msg,
                db=db,
            )

        # Legacy: Flutter / older clients
        if "userid" in payload and "message" in payload:
            _require_public_api_key(request.headers.get("X-Api-Key"))
            legacy_user = _payload_str(payload, "userid", "user_id", "phone")
            legacy_msg = _payload_str(payload, "message", "webhook_message", "text", "body")
            if legacy_user and legacy_msg:
                logger.info("Detected legacy simple chat request (userid + message)")
                return await handle_simple_chat(
                    customer_number=legacy_user,
                    company_number="",
                    message=legacy_msg,
                    db=db,
                )
        
        # Otherwise, treat as Meta WhatsApp webhook
        # Check if this is a valid Meta webhook payload
        if "object" not in payload or "entry" not in payload:
            logger.warning("Invalid webhook payload structure")
            return {"status": "ok", "message": "Invalid payload structure"}

        _verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256"))

        if str(payload.get("object") or "").lower() == "instagram":
            return handle_instagram_webhook(payload, db)

        # Extract entry and changes
        entries = payload.get("entry", [])
        if not entries:
            logger.warning("No entries in webhook payload")
            return {"status": "ok", "message": "No entries"}

        entry = entries[0]
        changes = entry.get("changes", [])
        if not changes:
            logger.warning("No changes in webhook entry")
            return {"status": "ok", "message": "No changes"}

        change = changes[0]
        value = change.get("value", {})
        field = change.get("field", "")

        logger.info(f"Webhook field type: {field}")

        # Route based on webhook type

        # 1. Handle incoming messages (text, image, etc.)
        if "messages" in value:
            return handle_incoming_message(
                value=value,
                db=db
            )

        # 2. Handle message status updates (delivered, read, failed)
        elif "statuses" in value:
            return handle_message_status(
                value=value,
                db=db
            )

        # 3. Partner / WABA lifecycle (Embedded Signup). Autobus still needs
        # the OAuth code to store a token; log enough to debug missed links.
        elif field == "account_update":
            waba_info = value.get("waba_info") if isinstance(value, dict) else None
            if not isinstance(waba_info, dict):
                waba_info = {}
            logger.info(
                "[WA] account_update entry_id=%s event=%s waba_id=%s keys=%s",
                entry.get("id"),
                value.get("event") or waba_info.get("event"),
                value.get("waba_id")
                or waba_info.get("waba_id")
                or waba_info.get("waba_business_id"),
                list(value.keys()) if isinstance(value, dict) else type(value).__name__,
            )
            return {"status": "ok", "message": "account_update acknowledged"}

        # 4. Handle other webhook types
        else:
            logger.info(f"Unsupported webhook field: {field}")
            return {"status": "ok", "message": "Webhook type not handled"}

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error processing webhook: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error processing webhook"
        )


def _ig_message_text(message: Any) -> str:
    from core.webhooks.instagram_inbound import ig_message_text

    return ig_message_text(message)


def _ig_image_url(message: Any) -> str:
    from core.webhooks.instagram_inbound import ig_image_url

    return ig_image_url(message)


def _remember_customer_identity(
    nlu_user_id: str,
    *,
    username: Optional[str] = None,
    phone: Optional[str] = None,
    display_name: Optional[str] = None,
) -> None:
    """Attach username / phone to the conversation so owners can recognize the customer."""
    if not nlu_user_id or not any((username, phone, display_name)):
        return
    try:
        get_nlu_system().conversation_manager.remember_customer_identity(
            nlu_user_id,
            username=username,
            phone=phone,
            display_name=display_name,
        )
    except Exception as exc:
        logger.warning("[identity] failed to store customer identity for %s: %s", nlu_user_id, exc)


def _nested_str(obj: Any, *keys: str) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _ig_event_profile(event: dict) -> Tuple[str, str]:
    """Username / display name from an Instagram webhook event when Meta includes them."""
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    from_obj = event.get("from")
    if not isinstance(from_obj, dict):
        from_obj = {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    msg_from = message.get("from") if isinstance(message.get("from"), dict) else {}
    username = (
        _nested_str(sender, "username", "user_name")
        or _nested_str(from_obj, "username", "user_name")
        or _nested_str(msg_from, "username", "user_name")
    )
    name = (
        _nested_str(sender, "name")
        or _nested_str(from_obj, "name")
        or _nested_str(msg_from, "name")
    )
    return username, name


def _ig_event_ids(event: dict, entry_id: str = "") -> Tuple[str, str]:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
    from_id = event.get("from")
    if isinstance(from_id, dict):
        from_id = from_id.get("id")
    to_id = event.get("to")
    if isinstance(to_id, dict):
        to_id = to_id.get("id")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    sender_id = str(sender.get("id") or from_id or "").strip()
    recipient_id = str(
        recipient.get("id")
        or to_id
        or metadata.get("phone_number_id")
        or entry_id
        or ""
    ).strip()
    return sender_id, recipient_id


def handle_instagram_webhook(payload: dict, db: Session):
    """Handle Instagram Login messaging webhooks (inbox DMs)."""
    from core.instagram.model.InstagramAccount import InstagramAccount
    from core.instagram.service.instagram_oauth_service import InstagramOAuthService

    entries = payload.get("entry") or []
    logger.info(
        "[IG webhook] object=%s entries=%s",
        payload.get("object"),
        [
            {
                "id": e.get("id") if isinstance(e, dict) else None,
                "keys": list(e.keys()) if isinstance(e, dict) else type(e).__name__,
                "messaging": len(e.get("messaging") or []) if isinstance(e, dict) else 0,
                "fields": [
                    c.get("field")
                    for c in (e.get("changes") or [])
                    if isinstance(c, dict)
                ]
                if isinstance(e, dict)
                else [],
            }
            for e in entries
        ],
    )

    events = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        for item in entry.get("messaging") or []:
            if isinstance(item, dict):
                events.append((item, entry_id))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "")
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            if field not in {"messages", "messaging", "message"}:
                continue
            nested_messages = value.get("messages")
            if isinstance(nested_messages, list):
                for msg in nested_messages:
                    if not isinstance(msg, dict):
                        continue
                    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                    events.append(
                        (
                            {
                                "sender": {"id": msg.get("from") or (msg.get("sender") or {}).get("id")},
                                "recipient": {"id": metadata.get("phone_number_id") or entry_id},
                                "message": msg,
                            },
                            entry_id,
                        )
                    )
            else:
                events.append((value, entry_id))

    if not events:
        logger.info("[IG webhook] no messaging events in payload")
        return {"status": "ok", "message": "No Instagram messaging events"}

    svc = InstagramOAuthService()
    handled = 0
    for event, entry_id in events:
        sender_id, recipient_id = _ig_event_ids(event, entry_id)
        if not sender_id or not recipient_id or sender_id == recipient_id:
            logger.info(
                "[IG webhook] skip event sender=%s recipient=%s keys=%s",
                sender_id or "-",
                recipient_id or "-",
                list(event.keys()) if isinstance(event, dict) else type(event).__name__,
            )
            continue
        message_body = event.get("message") or event
        text = _ig_message_text(message_body)
        image_url = _ig_image_url(message_body)
        if not text and image_url:
            text = "I am providing you with an image."
        if not text:
            logger.info(
                "[IG webhook] skip empty/echo sender=%s recipient=%s",
                sender_id,
                recipient_id,
            )
            continue

        lookup_ids = [recipient_id]
        if entry_id and entry_id not in lookup_ids:
            lookup_ids.append(entry_id)
        account = (
            db.query(InstagramAccount)
            .filter(
                InstagramAccount.ig_user_id.in_(lookup_ids),
                InstagramAccount.is_active.is_(True),
                InstagramAccount.messaging_enabled.is_(True),
            )
            .first()
        )
        if not account:
            logger.warning(
                "[IG webhook] no linked account for ig_user_id=%s entry_id=%s",
                recipient_id,
                entry_id or "-",
            )
            continue

        nlu_user_id = f"{account.user_id}:ig:{sender_id}"
        try:
            token = svc.decrypt_token(account.access_token_encrypted)
        except Exception as exc:
            logger.error("[IG webhook] token decrypt failed for %s: %s", sender_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to send Instagram reply",
            )
        svc.send_sender_action(token, sender_id, "mark_seen")
        svc.send_sender_action(token, sender_id, "typing_on")
        nlu_system = get_nlu_system()
        reply = nlu_system.process_message(nlu_user_id, text, image_url=image_url or None)
        username, display_name = _ig_event_profile(event)
        if not username:
            profile = svc.fetch_igsid_profile(token, sender_id)
            username = str(profile.get("username") or "").strip()
            display_name = display_name or str(profile.get("name") or "").strip()
        _remember_customer_identity(
            nlu_user_id, username=username, display_name=display_name
        )
        outbound = _nlu_reply_text(reply)
        reply_images = nlu_system.take_reply_images()
        if not outbound and not reply_images:
            svc.send_sender_action(token, sender_id, "typing_off")
            handled += 1
            continue
        sent = True
        if outbound:
            sent = svc.send_text(token, sender_id, outbound)
        for reply_image in reply_images:
            svc.send_image(token, sender_id, reply_image)
        if not sent:
            logger.error("[IG webhook] failed to send reply to %s", sender_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to send Instagram reply",
            )
        handled += 1

    return {"status": "ok", "message": f"Processed {handled} Instagram message(s)"}


async def handle_simple_chat(
    customer_number: str,
    company_number: str,
    message: str,
    db: Session,
):
    """
    Simple JSON chat: ``customer_number`` (chatter id / phone), ``company_number`` (merchant ``users.id``),
    ``message``. When ``company_number`` is empty, ``customer_number`` is used alone (legacy behaviour).
    """
    try:
        cust = (customer_number or "").strip()
        comp = (company_number or "").strip()
        msg = (message or "").strip()

        if not cust or not msg:
            return SimpleChatResponse(
                message="customer_number and message are required."
            )

        if comp:
            merchant = db.query(User).filter(User.id == comp).first()
            if not merchant:
                return SimpleChatResponse(
                    message="Unknown company_number: no merchant user with that id."
                )
            nlu_user_id = f"{comp}:{cust}"
            logger.info(
                "Processing simple chat (scoped) company=%s customer=%s",
                comp,
                cust[:32],
            )
        else:
            nlu_user_id = cust
            logger.info("Processing simple chat (legacy key) customer=%s", cust[:32])

        nlu_system = get_nlu_system()
        response_message = nlu_system.process_message(nlu_user_id, msg)
        from core.conversationmanager.service.customer_identity import looks_like_phone

        if looks_like_phone(cust):
            _remember_customer_identity(nlu_user_id, phone=cust)
        else:
            _remember_customer_identity(nlu_user_id, username=cust)

        logger.info("Generated response: %s", (response_message or "")[:200])

        return SimpleChatResponse(message=_nlu_reply_text(response_message))

    except Exception as e:
        logger.error(f"Error handling simple chat: {e}", exc_info=True)
        return SimpleChatResponse(
            message="An error occurred while processing your message. Please try again."
        )


def _whatsapp_merchant_user(phone_id: str, db: Session):
    """Resolve the Autobus business that owns the receiving WhatsApp number."""
    from core.whatsapp.model.WhatsAppAccount import WhatsAppAccount

    account = (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.phone_number_id == str(phone_id),
            WhatsAppAccount.is_active.is_(True),
        )
        .first()
    )
    if not account:
        return None, None

    display = (account.display_phone_number or "").strip()
    merchant = None
    if display:
        candidates = {
            display,
            normalize_ghana_phone_number(display),
            convert_to_local_ghana_format(display),
        }
        candidates = {c for c in candidates if c}
        matches = db.query(User).filter(User.phone.in_(list(candidates))).all()
        if len(matches) == 1:
            merchant = matches[0]
        elif len(matches) > 1:
            logger.warning(
                "Multiple Autobus users share WhatsApp display phone %s; using connected account owner",
                display,
            )

    if merchant is None:
        merchant = db.query(User).filter(User.id == account.user_id).first()

    return account, merchant


def _whatsapp_nlu_user_id(phone_id: str, customer_phone: str, db: Session) -> str:
    """
    Sender = customer (reply destination). Receiver phone_number_id = business.

    Conversation key matches Instagram/webchat: ``{merchant_users.id}:{customer_phone}``.
    """
    customer = (customer_phone or "").strip()
    _, merchant = _whatsapp_merchant_user(phone_id, db)
    if merchant and merchant.id and customer:
        logger.info(
            "WhatsApp session merchant=%s company=%s customer=%s phone_number_id=%s",
            merchant.id,
            merchant.company or merchant.fullname,
            customer,
            phone_id,
        )
        return f"{merchant.id}:{customer}"

    logger.warning(
        "WhatsApp business account not resolved for phone_number_id=%s; using unscoped customer key",
        phone_id,
    )
    return customer


def handle_incoming_message(value: dict, db: Session):
    """
    Handles incoming messages from users.
    Processes text messages, Flow responses, and other message types.
    """
    try:
        # Get phone number ID from metadata (needed for sending messages)
        # Meta WhatsApp Cloud API sends "phone_number_id"; keep "phone_id" as fallback
        metadata = value.get("metadata", {})
        phone_id = metadata.get("phone_number_id") or metadata.get("phone_id")
        if not phone_id:
            logger.error("Missing phone_number_id in metadata")
            return {"status": "error", "message": "Missing phone_number_id"}

        logger.info(f"Phone number ID: {phone_id}")

        # Get sender phone number
        contacts = value.get("contacts", [])
        if not contacts:
            logger.error("Missing contacts in webhook payload")
            return {"status": "error", "message": "Missing contacts"}

        phone = contacts[0].get("wa_id")
        if not phone:
            logger.error("Missing wa_id in contacts")
            return {"status": "error", "message": "Missing wa_id"}

        logger.info(f"Extracted phone number: {phone}")
        contact_profile = (
            contacts[0].get("profile") if isinstance(contacts[0].get("profile"), dict) else {}
        )
        whatsapp_profile_name = str(contact_profile.get("name") or "").strip()

        # Get the message
        messages = value.get("messages", [])
        if not messages:
            logger.warning("No messages in payload")
            return {"status": "ok", "message": "No messages"}

        message = messages[0]
        message_type = message.get("type")

        # Handle different message types
        if message_type == "text":
            return handle_text_message(
                message=message,
                phone=phone,
                phone_id=phone_id,
                db=db,
                display_name=whatsapp_profile_name,
            )

        elif message_type == "interactive":
            # This handles Flow responses
            return handle_interactive_message(
                message=message,
                phone=phone,
                phone_id=phone_id,
                db=db
            )

        elif message_type == "image":
            return handle_image_message(
                message=message,
                phone=phone,
                phone_id=phone_id,
                db=db,
                display_name=whatsapp_profile_name,
            )

        elif message_type == "audio":
            return handle_audio_message(
                message=message,
                phone=phone,
                phone_id=phone_id,
                db=db,
                display_name=whatsapp_profile_name,
            )

        elif message_type in ["video", "document"]:
            logger.info(f"Received {message_type} message from {phone}")
            whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
            whatsapp_service.send_message(
                phone_id=phone_id,
                recipient_phone=phone,
                message_text=f"Thanks for the {message_type}! I currently support text, images, and audio messages."
            )
            return {"status": "ok", "message": f"{message_type} message received"}

        else:
            logger.warning(f"Unsupported message type: {message_type}")
            whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
            whatsapp_service.send_message(
                phone_id=phone_id,
                recipient_phone=phone,
                message_text="Sorry, I don't support this message type yet."
            )
            return {"status": "ok", "message": f"Unsupported message type: {message_type}"}

    except Exception as e:
        logger.error(f"Error handling incoming message: {e}", exc_info=True)
        raise


def handle_text_message(
    message: dict,
    phone: str,
    phone_id: str,
    db: Session,
    display_name: str = "",
):
    """Handle regular text messages"""
    text_data = message.get("text")
    if not text_data or "body" not in text_data:
        logger.warning("Text message has no body")
        return {"status": "ok", "message": "Empty text message"}

    message_text = text_data.get("body")
    logger.info(f"Extracted text message: {message_text}")

    whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
    _whatsapp_show_typing(whatsapp_service, phone_id, message)
    nlu_user_id = _whatsapp_nlu_user_id(phone_id, phone, db)
    logger.info("Processing message through NLU for %s", nlu_user_id)

    nlu_system = get_nlu_system()
    response_message = nlu_system.process_message(nlu_user_id, message_text)
    _remember_customer_identity(nlu_user_id, phone=phone, display_name=display_name)
    outbound = _nlu_reply_text(response_message)
    reply_images = nlu_system.take_reply_images()

    logger.info("Generated response: %s", outbound[:200])

    if not outbound and not reply_images:
        return {"status": "success", "message": "Message recorded; no automated reply"}

    message_sent = _send_whatsapp_nlu_reply(
        whatsapp_service, phone_id, phone, outbound, reply_images
    )

    if not message_sent:
        logger.error("Failed to send WhatsApp message")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send WhatsApp message"
        )

    return {"status": "success", "message": "Message processed and sent"}


def handle_interactive_message(message: dict, phone: str, phone_id: str, db: Session):
    """
    Handle interactive messages like Flow responses and button replies.
    This is where you'll receive the registration form data!
    """
    interactive = message.get("interactive", {})
    interactive_type = interactive.get("type")

    logger.info(f"Received interactive message type: {interactive_type}")

    if interactive_type == "nfm_reply":
        # This is a Flow response (your registration form!)
        nfm_reply = interactive.get("nfm_reply", {})
        response_json = nfm_reply.get("response_json", "{}")
        flow_token = nfm_reply.get("flow_token")

        logger.info(f"Flow response received from {phone}")
        logger.info(f"Flow token: {flow_token}")
        logger.info(f"Response data: {response_json}")

        # Parse the registration form data
        registration_data = json.loads(response_json)

        # Log the complete registration data
        logger.info(f"Registration data received: {json.dumps(registration_data, indent=2)}")

        try:
            # Extract registration fields from Flow response
            first_name = registration_data.get("screen_0_First_Name_0", "").strip()
            last_name = registration_data.get("screen_0_Last_Name_1", "").strip()
            user_phone = registration_data.get("screen_0_phone_2", "").strip()
            email = registration_data.get("screen_0_email_3", "").strip()
            pin = registration_data.get("screen_0_PIN_4", "").strip()

            # Validate required fields
            if not all([first_name, last_name, user_phone, email, pin]):
                logger.error("Missing required fields in registration data")
                whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
                whatsapp_service.send_message(
                    phone_id=phone_id,
                    recipient_phone=phone,
                    message_text="Registration failed. Please ensure all fields are filled correctly."
                )
                return {"status": "error", "message": "Missing required fields"}

            # Normalize phone numbers
            normalized_user_phone = normalize_ghana_phone_number(user_phone)
            normalized_wa_id = normalize_ghana_phone_number(phone)

            logger.info(f"Phone normalization - Form: {user_phone} -> {normalized_user_phone}")
            logger.info(f"Phone normalization - WhatsApp: {phone} -> {normalized_wa_id}")

            # Check if user already exists
            existing_user = db.query(User).filter(
                (User.phone == normalized_wa_id) | (User.email == email)
            ).first()

            if existing_user:
                logger.warning(f"User already exists: {email} or {normalized_wa_id}")
                whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
                whatsapp_service.send_message(
                    phone_id=phone_id,
                    recipient_phone=phone,
                    message_text="You’re already registered with Lemo, so you can happily continue using the our service."
                )
                return {"status": "error", "message": "User already exists"}

            # Create user using AuthService
            auth_service = AuthService(db)

            # Generate unique user ID
            user_id = auth_service.generate_user_id()

            # Hash the PIN
            hashed_pin = auth_service.hash_password(pin)

            # Create new user
            new_user = User(
                id=user_id,
                username=email,  # Use email as username
                first_name=first_name,
                last_name=last_name,
                phone=normalized_wa_id,  # Use WhatsApp ID as primary phone
                email=email,
                hashed_pin=hashed_pin,
                enabled=True,  # Enable immediately for WhatsApp users
                created_at=datetime.now()
            )

            db.add(new_user)
            db.commit()
            db.refresh(new_user)

            logger.info(f"User registered successfully: {user_id} - {email}")

            # Send confirmation message
            whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
            whatsapp_service.send_message(
                phone_id=phone_id,
                recipient_phone=phone,
                message_text=f"🎉 Welcome {first_name}! Your registration is complete. You can now start using Autobus."
            )

        except Exception as e:
            logger.error(f"Error processing registration: {e}", exc_info=True)
            db.rollback()

            # Send error message to user
            whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
            whatsapp_service.send_message(
                phone_id=phone_id,
                recipient_phone=phone,
                message_text="Sorry, registration failed. Please try again later."
            )

            return {"status": "error", "message": f"Registration failed: {str(e)}"}

        return {"status": "success", "message": "Flow response processed"}

    elif interactive_type == "button_reply":
        # Handle button replies
        button_reply = interactive.get("button_reply", {})
        button_id = button_reply.get("id")
        button_text = button_reply.get("title")

        logger.info(f"Button clicked: {button_id} - {button_text}")

        # Process button action
        # ...

        return {"status": "success", "message": "Button reply processed"}

    else:
        logger.warning(f"Unsupported interactive type: {interactive_type}")
        return {"status": "ok", "message": f"Unsupported interactive type: {interactive_type}"}


def handle_message_status(value: dict, db: Session):
    """
    Handle message status updates (sent, delivered, read, failed).
    Useful for tracking message delivery and updating your database.
    """
    try:
        statuses = value.get("statuses", [])

        for status_update in statuses:
            message_id = status_update.get("id")
            status_type = status_update.get("status")  # sent, delivered, read, failed
            timestamp = status_update.get("timestamp")
            recipient_id = status_update.get("recipient_id")

            logger.info(f"Message {message_id} to {recipient_id}: {status_type}")

            if status_type == "failed":
                # Handle failed message
                errors = status_update.get("errors", [])
                if errors:
                    error = errors[0]
                    error_code = error.get("code")
                    error_title = error.get("title")
                    logger.error(f"Message failed: {error_code} - {error_title}")

            elif status_type == "read":
                # Message was read by recipient
                logger.info(f"Message {message_id} was read")

            # TODO: Update message status in your database
            # db.query(Message).filter(Message.whatsapp_id == message_id).update({
            #     "status": status_type,
            #     "delivered_at": timestamp if status_type == "delivered" else None,
            #     "read_at": timestamp if status_type == "read" else None
            # })
            # db.commit()

        return {"status": "success", "message": "Status updates processed"}

    except Exception as e:
        logger.error(f"Error handling message status: {e}", exc_info=True)
        raise


def handle_image_message(
    message: dict,
    phone: str,
    phone_id: str,
    db: Session,
    display_name: str = "",
):
    """
    Handle image messages from users.
    Images are processed by the LLM vision API for visual understanding.
    """
    try:
        image_data = message.get("image", {})
        media_id = image_data.get("id")
        
        if not media_id:
            logger.warning("Image message has no media ID")
            return {"status": "ok", "message": "Image received but no media ID"}
        
        logger.info(f"Received image message from {phone}, media_id: {media_id}")

        whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
        _whatsapp_show_typing(whatsapp_service, phone_id, message)
        nlu_user_id = _whatsapp_nlu_user_id(phone_id, phone, db)
        logger.info("Processing image through NLU for %s", nlu_user_id)

        nlu_system = get_nlu_system()

        caption = image_data.get("caption", "").strip()
        if caption:
            user_message = caption
            logger.info(f"Image caption found: {caption}")
        else:
            user_message = "I am providing you with an image. The image is referenced below, use it to infer the user's intent and extract slots."
            logger.info("No caption provided with image, using default message")

        response_message = nlu_system.process_message(
            nlu_user_id,
            user_message,
            image_media_id=media_id,
        )
        _remember_customer_identity(nlu_user_id, phone=phone, display_name=display_name)
        outbound = _nlu_reply_text(response_message)
        reply_images = nlu_system.take_reply_images()

        logger.info("Generated response for image message: %s", outbound[:200])

        if not outbound and not reply_images:
            return {"status": "success", "message": "Image recorded; no automated reply"}

        message_sent = _send_whatsapp_nlu_reply(
            whatsapp_service, phone_id, phone, outbound, reply_images
        )

        if not message_sent:
            logger.error("Failed to send WhatsApp message")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send WhatsApp message"
            )

        return {"status": "success", "message": "Image processed and response sent"}

    except Exception as e:
        logger.error(f"Error handling image message: {e}", exc_info=True)
        raise


def handle_audio_message(
    message: dict,
    phone: str,
    phone_id: str,
    db: Session,
    display_name: str = "",
):
    """
    Handle audio messages from users.
    Audio is transcribed through the configured Groq transcription model and processed as text.
    """
    try:
        audio_data = message.get("audio", {})
        media_id = audio_data.get("id")
        
        if not media_id:
            logger.warning("Audio message has no media ID")
            return {"status": "ok", "message": "Audio received but no media ID"}
        
        logger.info(f"Received audio message from {phone}, media_id: {media_id}")

        whatsapp_service = WhatsAppService.for_phone_id(phone_id, db)
        _whatsapp_show_typing(whatsapp_service, phone_id, message)
        nlu_user_id = _whatsapp_nlu_user_id(phone_id, phone, db)
        logger.info("Processing audio through NLU for %s", nlu_user_id)

        nlu_system = get_nlu_system()

        caption = audio_data.get("caption", "").strip()
        if caption:
            user_message = caption
            logger.info(f"Audio caption found: {caption}")
        else:
            user_message = "I'm sending you an audio message."
            logger.info("No caption provided with audio, using default message")

        response_message = nlu_system.process_message(
            nlu_user_id,
            user_message,
            audio_media_id=media_id,
        )
        _remember_customer_identity(nlu_user_id, phone=phone, display_name=display_name)
        outbound = _nlu_reply_text(response_message)
        reply_images = nlu_system.take_reply_images()

        logger.info("Generated response for audio message: %s", outbound[:200])

        if not outbound and not reply_images:
            return {"status": "success", "message": "Audio recorded; no automated reply"}

        message_sent = _send_whatsapp_nlu_reply(
            whatsapp_service, phone_id, phone, outbound, reply_images
        )

        if not message_sent:
            logger.error("Failed to send WhatsApp message")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send WhatsApp message"
            )

        return {"status": "success", "message": "Audio transcribed and response sent"}

    except Exception as e:
        logger.error(f"Error handling audio message: {e}", exc_info=True)
        raise
