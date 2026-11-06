"""Tool schemas and executors for the owner-facing business agent."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from core.conversationmanager.service.conversation_list_service import ConversationListService
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.customers.service.customer_messaging_service import CustomerMessagingService
from core.customers.service.customer_service import CustomerService
from core.email.sender_email import SenderEmailNotConfigured
from core.intelligence.service.business_context_assembler import BusinessContextAssembler
from core.orders.service.order_service import OrderService
from core.product.dto.product_create_dto import ProductCreateDTO
from core.product.dto.product_response_dto import ProductResponseDTO
from core.product.dto.product_update_dto import ProductUpdateDTO
from core.product.service.product_service import ProductService
from core.user.model.User import User
from utilities.plain_text import strip_markdown_formatting

logger = logging.getLogger(__name__)


def openai_tools() -> List[Dict[str, Any]]:
    return [{"type": "function", "function": spec} for spec in _FUNCTION_SPECS]


def execute_tool(db: Session, user: User, name: str, args: Dict[str, Any]) -> str:
    """Run a tool and return a compact JSON string for the model."""
    fn = _EXECUTORS.get(name)
    if fn is None:
        return _err(f"Unknown tool: {name}")
    try:
        result = fn(db, user, args or {})
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            return _err(str(detail.get("message") or detail))
        return _err(str(detail))
    except SenderEmailNotConfigured as exc:
        return _err(str(exc))
    except ValidationError as exc:
        return _err(exc.errors()[0].get("msg") if exc.errors() else str(exc))
    except Exception as exc:
        logger.exception("[OWNER_AGENT] tool %s failed for user %s", name, user.id)
        return _err(str(exc) or "That action failed. Please try again.")


def _ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


def _enum_val(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _product_brief(product) -> Dict[str, Any]:
    dto = ProductResponseDTO.from_product(product)
    data = dto.model_dump(mode="json")
    keep = (
        "product_id",
        "name",
        "price",
        "category",
        "condition",
        "number_in_stock",
        "description",
        "photos",
        "videos",
        "link",
    )
    return {k: data.get(k) for k in keep}


def _order_brief(order) -> Dict[str, Any]:
    return {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "status": _enum_val(order.order_status),
        "payment_status": _enum_val(order.payment_status),
        "fulfillment_status": _enum_val(order.fulfillment_status),
        "total_amount": float(order.total_amount) if order.total_amount is not None else None,
        "currency": order.currency_code,
        "items": order.order_items,
        "order_date": order.order_date.isoformat() if order.order_date else None,
    }


def _customer_brief(customer) -> Dict[str, Any]:
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "customer_number": customer.customer_number,
        "network": customer.network,
        "is_active": customer.is_active,
    }


def _tool_get_business_snapshot(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    context = BusinessContextAssembler(db).assemble(user, "overview snapshot dashboard")
    return _ok({"snapshot": context.snapshot, "sources": list(context.sources)})


def _tool_list_products(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 50))
    category = (args.get("category") or "").strip() or None
    products = ProductService(db).get_products_by_user(user.id, skip=0, limit=limit, category=category)
    return _ok([_product_brief(p) for p in products])


def _tool_get_product(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    product_id = str(args.get("product_id") or "").strip()
    if not product_id:
        return json.loads(_err("product_id is required"))
    product = ProductService(db).get_product_by_id(product_id)
    if not product or (product.user_id and product.user_id != user.id):
        return json.loads(_err("Product not found"))
    return _ok(_product_brief(product))


def _tool_list_orders(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 15)
    limit = max(1, min(limit, 40))
    status = (args.get("order_status") or "").strip() or None
    orders = OrderService(db).get_orders_by_user(user.id, skip=0, limit=limit, order_status=status)
    return _ok([_order_brief(o) for o in orders])


def _tool_get_order(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    service = OrderService(db)
    order = None
    order_id = str(args.get("order_id") or "").strip()
    order_number = str(args.get("order_number") or "").strip()
    if order_id:
        order = service.get_order_by_id(order_id)
    elif order_number:
        order = service.get_order_by_number(order_number)
    else:
        return json.loads(_err("order_id or order_number is required"))
    if not order or (order.user_id and order.user_id != user.id):
        return json.loads(_err("Order not found"))
    return _ok(_order_brief(order))


def _tool_list_customers(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    customers = CustomerService(db).get_customers(user.id)
    limit = int(args.get("limit") or 30)
    limit = max(1, min(limit, 80))
    return _ok([_customer_brief(c) for c in customers[:limit]])


def _tool_list_inbox(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    completed, waiting = ConversationListService(db).list_grouped_conversations_for_user(
        user.id, skip=0, limit=int(args.get("limit") or 12)
    )

    def _brief(row) -> Dict[str, Any]:
        return {
            "id": row.id,
            "name": row.customer_display_name or row.user_fullname or row.customer_phone,
            "last_message": row.last_message,
            "waiting": bool(row.intervention_active),
            "intent": row.current_intent,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    return _ok(
        {
            "waiting": [_brief(r) for r in waiting],
            "recent": [_brief(r) for r in completed],
        }
    )


def _tool_search_knowledge(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return json.loads(_err("query is required"))
    context = BusinessContextAssembler(db).assemble(user, query)
    return _ok(
        {
            "snapshot": context.snapshot,
            "prompt_excerpt": (context.as_prompt_block() or "")[:2500],
            "sources": list(context.sources),
        }
    )


def _tool_create_product(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(args)
    photo = payload.pop("photo", None)
    if photo and not payload.get("photos"):
        payload["photos"] = [photo]
    if not str(payload.get("condition") or "").strip():
        payload["condition"] = "New"
    dto = ProductCreateDTO(**payload)
    ok, product, message = ProductService(db).create_product(dto, user_id=user.id)
    if not ok or product is None:
        return json.loads(_err(message))
    return _ok({"message": message, "product": _product_brief(product)})


def _tool_update_product(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    product_id = str(args.get("product_id") or "").strip()
    if not product_id:
        return json.loads(_err("product_id is required"))
    existing = ProductService(db).get_product_by_id(product_id)
    if not existing or (existing.user_id and existing.user_id != user.id):
        return json.loads(_err("Product not found"))
    update_args = {k: v for k, v in args.items() if k != "product_id" and v is not None}
    dto = ProductUpdateDTO(**update_args)
    ok, product, message = ProductService(db).update_product(product_id, dto)
    if not ok or product is None:
        return json.loads(_err(message))
    return _ok({"message": message, "product": _product_brief(product)})


def _tool_create_customer(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    name = str(args.get("name") or "").strip()
    number = str(args.get("customer_number") or "").strip()
    if not name or not number:
        return json.loads(_err("name and customer_number are required"))
    ok, customer, message = CustomerService(db).add_customer(
        user_id=user.id,
        name=name,
        customer_number=number,
        network=(str(args.get("network")).strip() if args.get("network") else None),
        bank_code=(str(args.get("bank_code")).strip() if args.get("bank_code") else None),
        email=(str(args.get("email")).strip() if args.get("email") else None),
    )
    if not ok or customer is None:
        return json.loads(_err(message))
    return _ok({"message": message, "customer": _customer_brief(customer)})


def _tool_send_customer_sms(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    ids = _int_ids(args.get("customer_ids"))
    message = str(args.get("message") or "").strip()
    if not ids or not message:
        return json.loads(_err("customer_ids and message are required"))
    CreditService(db).require_credits(user.id, CreditType.SMS.value, float(len(ids)), "customer_sms")
    try:
        result = CustomerMessagingService(db).send_sms(user.id, ids, message)
    except ValueError as exc:
        return json.loads(_err(str(exc)))
    return _ok(result.model_dump(mode="json"))


def _tool_send_customer_email(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    ids = _int_ids(args.get("customer_ids"))
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not ids or not subject or not body:
        return json.loads(_err("customer_ids, subject, and body are required"))
    CreditService(db).require_credits(user.id, CreditType.EMAIL.value, float(len(ids)), "customer_email")
    result = CustomerMessagingService(db).send_email(user.id, ids, subject, body)
    return _ok(result.model_dump(mode="json"))


def _tool_draft_marketing_copy(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    from core.intelligence.service.owner_agent_campaign import save_campaign
    from core.nlu.config import SYSTEM_PROMPTS
    from core.nlu.service.llmclient import LLMClient

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return json.loads(_err("prompt is required"))
    raw = LLMClient().chat_completion(
        system_prompt=SYSTEM_PROMPTS.get("marketing")
        or "Write short marketing copy. Plain text only.",
        user_message=prompt,
        conversation_history=None,
        temperature=0.8,
        max_tokens=800,
    )
    text = strip_markdown_formatting(raw or "").strip()
    if not text:
        return json.loads(_err("Could not draft copy right now."))
    save_campaign(user, caption=text, copy=text)
    return _ok({"copy": text, "caption": text})


def _suggested_caption(prompt: str) -> str:
    from core.nlu.config import SYSTEM_PROMPTS
    from core.nlu.service.llmclient import LLMClient

    raw = LLMClient().chat_completion(
        system_prompt=SYSTEM_PROMPTS.get("marketing")
        or "Write a short Instagram caption. Plain text only. No hashtag spam.",
        user_message=f"Write a short Instagram caption for this campaign: {prompt}",
        conversation_history=None,
        temperature=0.7,
        max_tokens=180,
    )
    return strip_markdown_formatting(raw or "").strip()


def _tool_generate_marketing_image(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    from core.agent.tools.google_image.google_image_service import GoogleImageService
    from core.intelligence.service.owner_agent_campaign import (
        save_campaign,
        upload_image_bytes,
        _await,
    )
    from core.media.service.media_rag_prompt import enrich_media_generation_prompt

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return json.loads(_err("prompt is required"))
    CreditService(db).require_credits(
        user.id, CreditType.IMAGE_GEN.value, 1.0, "owner_agent_image"
    )
    grounded = enrich_media_generation_prompt(
        prompt, db, req_user_id=user.id, media_kind="image"
    )
    service = GoogleImageService()
    b64 = _await(service.generate_image_base64(grounded, user_id=user.id))
    mime = service.last_mime_type or "image/png"
    url = upload_image_bytes(user.id, b64, mime)
    caption = str(args.get("caption") or "").strip() or _suggested_caption(prompt)
    save_campaign(
        user,
        kind="image",
        url=url,
        media_urls=[url],
        caption=caption,
        copy=caption,
        prompt=prompt,
    )
    return _ok(
        {
            "kind": "image",
            "url": url,
            "mime": mime,
            "caption": caption,
            "name": "Generated campaign image",
        }
    )


def _tool_generate_marketing_video(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    from core.agent.tools.google_veo.google_veo_service import GoogleVeoService
    from core.intelligence.service.owner_agent_campaign import _await, save_campaign
    from core.media.service.media_rag_prompt import enrich_media_generation_prompt

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return json.loads(_err("prompt is required"))
    CreditService(db).require_credits(
        user.id, CreditType.VIDEO_GEN.value, 1.0, "owner_agent_video"
    )
    grounded = enrich_media_generation_prompt(
        prompt, db, req_user_id=user.id, media_kind="video"
    )
    url = _await(GoogleVeoService().generate_video_and_store(grounded, user_id=user.id))
    caption = str(args.get("caption") or "").strip() or _suggested_caption(prompt)
    save_campaign(
        user,
        kind="video",
        url=url,
        media_urls=[url],
        caption=caption,
        copy=caption,
        prompt=prompt,
    )
    return _ok(
        {
            "kind": "video",
            "url": url,
            "mime": "video/mp4",
            "caption": caption,
            "name": "Generated campaign video",
        }
    )


def _tool_list_instagram_accounts(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    from core.intelligence.service.owner_agent_campaign import list_accounts

    accounts = list_accounts(db, user)
    return _ok(
        {
            "accounts": accounts,
            "connect_hint": None
            if accounts
            else "No Instagram account is linked. Ask the owner to connect Instagram under Marketing → Manage Outlets.",
        }
    )


def _tool_list_social_accounts(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    from core.intelligence.service.owner_agent_campaign import list_social_destinations

    accounts = list_social_destinations(db, user)
    return _ok(
        {
            "accounts": accounts,
            "connect_hint": None
            if accounts
            else "No social accounts are linked. Ask the owner to connect Instagram, YouTube, or TikTok under Marketing → Manage Outlets.",
        }
    )


def _tool_publish_instagram_post(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(args or {})
    if not payload.get("platforms") and not payload.get("account_ids") and not payload.get("account_id"):
        payload["platforms"] = ["instagram"]
    return _publish_to_destinations(db, user, payload, instagram_only=True)


def _tool_publish_social_post(db: Session, user: User, args: Dict[str, Any]) -> Dict[str, Any]:
    return _publish_to_destinations(db, user, args or {}, instagram_only=False)


def _publish_to_destinations(
    db: Session,
    user: User,
    args: Dict[str, Any],
    *,
    instagram_only: bool,
) -> Dict[str, Any]:
    from core.instagram.service.instagram_publish_service import InstagramPublishService
    from core.intelligence.service.owner_agent_campaign import (
        VIDEO_ONLY_PROVIDERS,
        build_postiz_payload,
        fill_publish_args,
        media_looks_like_video,
        pick_publish_account,
        run_async,
        save_campaign,
    )
    from core.socialmedia.service.instagram_media_prepare import InstagramMediaPrepareError, InstagramMediaPrepareService
    from core.socialmedia.service.postiz_api_service import PostizAPIError
    from core.socialmedia.service.postiz_native import publish_marketing_posts
    from core.socialmedia.service.tiktok_direct_post import coerce_tiktok_privacy_for_unaudited_app
    from core.socialmedia.service.zernio_client import ZernioAPIError

    payload = fill_publish_args(user, args, db=db, instagram_only=instagram_only)
    urls = list(payload.get("media_urls") or [])
    caption = str(payload.get("caption") or "").strip()
    if not urls:
        return json.loads(
            _err("There is no generated image or video to post. Generate campaign content first.")
        )
    dests = list(payload.get("destinations") or [])
    if not dests:
        if instagram_only:
            return json.loads(
                _err("No Instagram account is linked. Connect Instagram from Inbox.")
            )
        if media_looks_like_video(urls):
            return json.loads(
                _err("No social accounts are linked. Connect YouTube, TikTok, Facebook, or Instagram under Marketing.")
            )
        return json.loads(
            _err(
                "No linked accounts can take this image. Connect Facebook or Instagram, or generate a video for YouTube and TikTok."
            )
        )

    published: List[str] = []
    errors: List[str] = []
    ig_dests = [d for d in dests if d.get("channel") == "autobus_instagram"]
    social_dests = [d for d in dests if d.get("channel") in {"zernio", "postiz"}]
    has_video = media_looks_like_video(urls)

    for dest in ig_dests:
        account = pick_publish_account(db, user, dest.get("account_id"))
        if not account:
            errors.append(f"{dest.get('label') or 'Instagram'}: account not found")
            continue
        if not account.publishing_enabled:
            errors.append(f"{dest.get('label') or 'Instagram'}: publishing is disabled")
            continue
        try:
            result = InstagramPublishService().publish(account, caption=caption, media_urls=list(urls))
            label = dest.get("label") or f"@{account.username or account.ig_user_id}"
            published.append(label)
            save_campaign(user, account_id=account.id)
            logger.info("[OWNER_AGENT] Published Instagram post %s", result.get("post_id"))
        except Exception as exc:
            errors.append(f"{dest.get('label') or 'Instagram'}: {exc}")

    ready_social: List[Dict[str, Any]] = []
    for dest in social_dests:
        provider = str(dest.get("provider") or "").strip().lower()
        if provider in VIDEO_ONLY_PROVIDERS and not has_video:
            errors.append(f"{dest.get('label') or provider}: needs a video")
            continue
        ready_social.append(dest)

    if ready_social:
        try:
            social_payload = coerce_tiktok_privacy_for_unaudited_app(
                build_postiz_payload(ready_social, caption=caption, media_urls=list(urls))
            )
            social_payload = InstagramMediaPrepareService().prepare_postiz_payload(
                social_payload
            )
            run_async(publish_marketing_posts(db, user.id, social_payload))
            published.extend(str(d.get("label") or d.get("provider")) for d in ready_social)
        except InstagramMediaPrepareError as exc:
            errors.append(str(exc))
        except (ZernioAPIError, PostizAPIError) as exc:
            errors.append(str(exc))
        except Exception as exc:
            logger.exception("[OWNER_AGENT] Social publish failed")
            errors.append(str(exc))

    if not published:
        return json.loads(_err("; ".join(errors) if errors else "Publishing failed."))
    message = "Published to " + (
        published[0]
        if len(published) == 1
        else ", ".join(published[:-1]) + f", and {published[-1]}"
    )
    if errors:
        message += ". Some destinations failed: " + "; ".join(errors)
    return _ok({"message": message, "published": published, "errors": errors})


def _int_ids(raw: Any) -> List[int]:
    if raw is None:
        return []
    if isinstance(raw, int):
        return [raw]
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


_EXECUTORS = {
    "get_business_snapshot": _tool_get_business_snapshot,
    "list_products": _tool_list_products,
    "get_product": _tool_get_product,
    "list_orders": _tool_list_orders,
    "get_order": _tool_get_order,
    "list_customers": _tool_list_customers,
    "list_inbox": _tool_list_inbox,
    "search_knowledge": _tool_search_knowledge,
    "create_product": _tool_create_product,
    "update_product": _tool_update_product,
    "create_customer": _tool_create_customer,
    "send_customer_sms": _tool_send_customer_sms,
    "send_customer_email": _tool_send_customer_email,
    "draft_marketing_copy": _tool_draft_marketing_copy,
    "generate_marketing_image": _tool_generate_marketing_image,
    "generate_marketing_video": _tool_generate_marketing_video,
    "list_instagram_accounts": _tool_list_instagram_accounts,
    "list_social_accounts": _tool_list_social_accounts,
    "publish_instagram_post": _tool_publish_instagram_post,
    "publish_social_post": _tool_publish_social_post,
}

_FUNCTION_SPECS: List[Dict[str, Any]] = [
    {
        "name": "get_business_snapshot",
        "description": "Live overview of this business: profile, product count, open orders, inbox.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_products",
        "description": "List products in the owner's catalog.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "get_product",
        "description": "Get one product by product_id.",
        "parameters": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "list_orders",
        "description": "List recent orders. Optional order_status: pending, processing, confirmed, completed, cancelled.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
            },
        },
    },
    {
        "name": "get_order",
        "description": "Get one order by order_id or order_number.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "order_number": {"type": "string"},
            },
        },
    },
    {
        "name": "list_customers",
        "description": "List saved customers.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 80}},
        },
    },
    {
        "name": "list_inbox",
        "description": "Customer inbox: conversations waiting for the owner, plus recent chats.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        },
    },
    {
        "name": "search_knowledge",
        "description": "Search uploaded files, indexed websites, and the business profile.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "create_product",
        "description": "Create a catalog product. Requires at least one photo or video URL already uploaded.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "price": {"type": "number"},
                "condition": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string"},
                "number_in_stock": {"type": "integer"},
                "link": {"type": "string"},
                "photos": {"type": "array", "items": {"type": "string"}},
                "videos": {"type": "array", "items": {"type": "string"}},
                "photo": {"type": "string"},
            },
            "required": ["name", "price", "condition"],
        },
    },
    {
        "name": "update_product",
        "description": "Update an existing product. Only send fields that should change.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "name": {"type": "string"},
                "price": {"type": "number"},
                "condition": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string"},
                "number_in_stock": {"type": "integer"},
                "link": {"type": "string"},
                "photos": {"type": "array", "items": {"type": "string"}},
                "videos": {"type": "array", "items": {"type": "string"}},
                "photo": {"type": "string"},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "create_customer",
        "description": "Save a customer. customer_number is a phone/MoMo number or bank account.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "customer_number": {"type": "string"},
                "network": {"type": "string"},
                "bank_code": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": ["name", "customer_number"],
        },
    },
    {
        "name": "send_customer_sms",
        "description": "Send an SMS to saved customers by id. Max 160 characters.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_ids": {"type": "array", "items": {"type": "integer"}},
                "message": {"type": "string"},
            },
            "required": ["customer_ids", "message"],
        },
    },
    {
        "name": "send_customer_email",
        "description": "Email saved customers. Requires a sender email on the account.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_ids": {"type": "array", "items": {"type": "integer"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["customer_ids", "subject", "body"],
        },
    },
    {
        "name": "draft_marketing_copy",
        "description": "Draft marketing copy or an Instagram caption. Does not publish.",
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_marketing_image",
        "description": (
            "Generate a campaign image/poster the owner can preview in chat. "
            "Use for sale content, flyers, and Instagram posts unless they only want words or a video. "
            "Does not publish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_marketing_video",
        "description": (
            "Generate a short campaign video/reel the owner can preview. "
            "Only when they ask for a video or reel. Does not publish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_instagram_accounts",
        "description": "List Instagram Business accounts linked to this owner.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_social_accounts",
        "description": (
            "List linked social destinations this owner can publish to: Autobus Instagram plus "
            "Postiz YouTube/TikTok (and other Postiz channels except Facebook/WhatsApp)."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "publish_instagram_post",
        "description": (
            "Publish the last generated campaign to a linked Autobus Instagram account. "
            "The app asks the owner to confirm before this runs. Prefer publish_social_post "
            "when they want more than Instagram."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "caption": {"type": "string"},
                "media_urls": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "publish_social_post",
        "description": (
            "Publish the last generated campaign to linked social accounts. "
            "The app asks the owner to confirm and lists every destination before this runs. "
            "Call list_social_accounts first. Omit media_urls/caption to reuse the last generated campaign. "
            "Pass platforms (instagram, youtube, tiktok) or account_ids to target specific outlets; "
            "otherwise post to every linked account that can take the media. YouTube and TikTok need a video."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "account_id": {"type": "string"},
                "platforms": {"type": "array", "items": {"type": "string"}},
                "caption": {"type": "string"},
                "media_urls": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the owner for input the static app already supports: text, image, video, file, "
            "or a multiple-choice. Use this when a product photo/video or a missing field is required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["text", "image", "video", "file", "choice"],
                },
                "prompt": {"type": "string"},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["id", "label"],
                    },
                },
            },
            "required": ["kind", "prompt"],
        },
    },
]
