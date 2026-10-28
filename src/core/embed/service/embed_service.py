"""Catalog sync, conversational turns, and outbound webhooks for embedded chat."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import URLError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.embed.model.embed import EmbedIntegration, EmbedMessageReceipt
from core.embed.scope import for_sub_business, session_user_id
from core.embed.turn_context import begin_embed_turn, end_embed_turn
from core.nlu.nlu import get_nlu_system
from core.nlu.service.customer_shop import CUSTOMER_SHOP_INTENTS, catalog_items_from_products
from core.orders.dto.order_update_dto import OrderUpdateDTO
from core.orders.model.order import Order
from core.orders.service.order_service import OrderService
from core.product.model.product import Inventory, Product, ProductImage
from core.user.model.User import User

logger = logging.getLogger(__name__)

_PLACEHOLDER_PHOTO = "https://useautobus.com/embed/catalog-placeholder.png"
_KEY_PREFIX = "ab_live_"


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sign_webhook_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sub_business(body: Dict[str, Any]) -> Dict[str, str]:
    raw = body.get("sub_business") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return {"external_id": "", "name": ""}
    return {
        "external_id": str(raw.get("external_id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
    }


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip()).strip("-").upper()
    return cleaned[:40] or "ITEM"


class EmbedService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, user_id: str) -> EmbedIntegration:
        row = self.db.query(EmbedIntegration).filter(EmbedIntegration.user_id == user_id).first()
        if row:
            return row
        row = EmbedIntegration(user_id=user_id, webhook_secret=secrets.token_urlsafe(24))
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def public_settings(self, row: EmbedIntegration) -> Dict[str, Any]:
        return {
            "enabled": bool(row.enabled),
            "has_api_key": bool(row.key_hash),
            "api_key_prefix": row.key_prefix or "",
            "webhook_url": row.webhook_url or "",
            "webhook_secret": row.webhook_secret or "",
            "catalog_mode": row.catalog_mode or "managed",
            "enforce_stock": bool(row.enforce_stock),
            "handoff_enabled": bool(row.handoff_enabled),
        }

    def update_settings(self, user_id: str, payload: Dict[str, Any]) -> EmbedIntegration:
        row = self.get_or_create(user_id)
        if "enabled" in payload and payload["enabled"] is not None:
            row.enabled = bool(payload["enabled"])
        if "webhook_url" in payload:
            url = (payload.get("webhook_url") or "").strip()
            row.webhook_url = url or None
        if "catalog_mode" in payload and payload["catalog_mode"]:
            mode = str(payload["catalog_mode"]).strip().lower()
            if mode not in {"managed", "synced"}:
                raise ValueError("catalog_mode must be managed or synced")
            row.catalog_mode = mode
        if "enforce_stock" in payload and payload["enforce_stock"] is not None:
            row.enforce_stock = bool(payload["enforce_stock"])
        if "handoff_enabled" in payload and payload["handoff_enabled"] is not None:
            row.handoff_enabled = bool(payload["handoff_enabled"])
        if not row.webhook_secret:
            row.webhook_secret = secrets.token_urlsafe(24)
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def rotate_key(self, user_id: str) -> Tuple[EmbedIntegration, str]:
        row = self.get_or_create(user_id)
        raw = _KEY_PREFIX + secrets.token_urlsafe(32)
        row.key_hash = hash_api_key(raw)
        row.key_prefix = raw[:16]
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row, raw

    def revoke_key(self, user_id: str) -> EmbedIntegration:
        row = self.get_or_create(user_id)
        row.key_hash = None
        row.key_prefix = None
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    def authenticate(self, raw_key: str) -> EmbedIntegration:
        key = (raw_key or "").strip()
        if not key.startswith(_KEY_PREFIX):
            raise PermissionError("Invalid API key")
        row = (
            self.db.query(EmbedIntegration)
            .filter(EmbedIntegration.key_hash == hash_api_key(key))
            .first()
        )
        if not row or not row.enabled:
            raise PermissionError("Invalid API key")
        return row

    def upsert_catalog(self, row: EmbedIntegration, items: List[Dict[str, Any]], sub_business: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if (row.catalog_mode or "managed") != "synced":
            raise PermissionError("Catalog sync is off. Set catalog mode to synced in the portal.")
        parent = sub_business or {"external_id": "", "name": ""}
        upserted = []
        failed = []
        for item in items:
            try:
                item_sub = _sub_business(item) if isinstance(item, dict) and item.get("sub_business") else parent
                product = self._upsert_one(row.user_id, item, item_sub.get("external_id") or "")
                upserted.append(self._product_card(product, self._currency(row.user_id)))
            except Exception as exc:
                failed.append({"external_id": (item or {}).get("external_id"), "error": str(exc)})
        self.db.commit()
        result = {"upserted": upserted, "failed": failed}
        if failed:
            self.deliver(
                row,
                "catalog.sync_failed",
                {"failed": failed, "upserted_count": len(upserted), "sub_business": parent},
            )
        if parent.get("external_id"):
            result["sub_business"] = parent
        return result

    def list_catalog(self, row: EmbedIntegration, sub_business_id: str = "") -> List[Dict[str, Any]]:
        products = (
            self.db.query(Product)
            .filter(Product.user_id == row.user_id, Product.external_id.isnot(None))
            .order_by(Product.name)
            .all()
        )
        if sub_business_id:
            products = for_sub_business(products, sub_business_id)
        currency = self._currency(row.user_id)
        return [self._product_card(p, currency) for p in products]

    def handle_message(self, row: EmbedIntegration, body: Dict[str, Any]) -> Dict[str, Any]:
        message = body.get("message") if isinstance(body.get("message"), dict) else {}
        message_id = str(message.get("id") or "").strip()
        text = str(message.get("text") or "").strip()
        image_url = str(message.get("image_url") or "").strip()
        customer = body.get("customer") if isinstance(body.get("customer"), dict) else {}
        external_id = str(customer.get("external_id") or "").strip()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not message_id or not external_id or (not text and not image_url):
            raise ValueError("conversation customer.external_id, message.id, and message.text or message.image_url are required")
        if not text and image_url:
            text = "I am providing you with an image."
        sub = _sub_business(body)
        receipt_id = f"{sub['external_id']}:{message_id}" if sub["external_id"] else message_id

        existing = (
            self.db.query(EmbedMessageReceipt)
            .filter(
                EmbedMessageReceipt.integration_id == row.id,
                EmbedMessageReceipt.message_id == receipt_id,
            )
            .first()
        )
        if existing and isinstance(existing.response_json, dict):
            return existing.response_json

        nlu_user_id = session_user_id(row.user_id, external_id, sub["external_id"])
        self._seed_customer_slots(nlu_user_id, customer)
        tokens = begin_embed_turn(
            {
                "conversation_id": conversation_id,
                "external_customer_id": external_id,
                "sub_business_id": sub["external_id"],
                "sub_business_name": sub["name"],
                "channel": "embed",
            }
        )
        try:
            paused = CreditService(self.db).consume_or_pause(
                row.user_id, CreditType.LLM.value, 1.0, "embed_chat"
            )
            if paused:
                reply = paused
                reply_images = []
                actions = end_embed_turn(tokens)
            else:
                nlu = get_nlu_system()
                reply = nlu.process_message(nlu_user_id, text, image_url=image_url or None) or ""
                reply_images = nlu.take_reply_images()
                actions = end_embed_turn(tokens)
        except Exception:
            end_embed_turn(tokens)
            raise

        state = get_nlu_system().conversation_manager.get_conversation_state(nlu_user_id)
        if row.handoff_enabled and getattr(state, "intervention_active", False):
            if not any(a.get("type") == "conversation.handoff" for a in actions):
                actions.append(
                    {
                        "type": "conversation.handoff",
                        "conversation_id": conversation_id,
                        "external_customer_id": external_id,
                        "sub_business": sub if sub["external_id"] else None,
                    }
                )

        products = self._products_for_turn(
            row.user_id,
            getattr(state, "current_intent", ""),
            reply,
            self._currency(row.user_id),
            sub["external_id"],
        )
        response = {
            "conversation_id": conversation_id or nlu_user_id,
            "reply": {"text": reply, "products": products, "images": reply_images},
            "actions": actions,
        }
        if sub["external_id"]:
            response["sub_business"] = sub
        self.db.add(
            EmbedMessageReceipt(
                integration_id=row.id,
                message_id=receipt_id,
                response_json=response,
            )
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raced = (
                self.db.query(EmbedMessageReceipt)
                .filter(
                    EmbedMessageReceipt.integration_id == row.id,
                    EmbedMessageReceipt.message_id == receipt_id,
                )
                .first()
            )
            if raced and isinstance(raced.response_json, dict):
                return raced.response_json
            raise

        for action in actions:
            if action.get("type") == "order.created":
                self.deliver(row, "order.created", action.get("order") or action)
            elif action.get("type") == "conversation.handoff":
                self.deliver(row, "conversation.handoff", action)
        return response

    def update_order(self, row: EmbedIntegration, order_number: str, body: Dict[str, Any]) -> Dict[str, Any]:
        order = (
            self.db.query(Order)
            .filter(Order.user_id == row.user_id, Order.order_number == order_number)
            .first()
        )
        if not order:
            raise LookupError("Order not found")
        sub = _sub_business(body)
        metadata = order.custom_metadata if isinstance(order.custom_metadata, dict) else {}
        stored_sub = str(metadata.get("sub_business_id") or "")
        if sub["external_id"] and stored_sub and stored_sub != sub["external_id"]:
            raise LookupError("Order not found")
        payload = {}
        for field in ("payment_status", "fulfillment_status", "order_status", "payment_reference"):
            if body.get(field):
                payload[field] = body[field]
        if not payload:
            raise ValueError("No order fields to update")
        success, updated, message = OrderService(self.db).update_order(str(order.order_id), OrderUpdateDTO(**payload))
        if not success or not updated:
            raise ValueError(message or "Order update failed")
        card = self._order_card(updated, body.get("external_customer_id"))
        self.deliver(row, "order.updated", card)
        return card

    def deliver(self, row: EmbedIntegration, event: str, data: Dict[str, Any]) -> None:
        url = (row.webhook_url or "").strip()
        secret = (row.webhook_secret or "").strip()
        if not url or not secret:
            return
        envelope = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": event,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "data": data,
        }
        raw = json.dumps(envelope, default=str).encode("utf-8")
        signature = sign_webhook_body(secret, raw)

        def _send() -> None:
            req = urlrequest.Request(
                url,
                data=raw,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Autobus-Event": event,
                    "X-Autobus-Signature": signature,
                },
            )
            try:
                with urlrequest.urlopen(req, timeout=8) as resp:
                    resp.read()
            except (URLError, TimeoutError, OSError) as exc:
                logger.warning("[EMBED] webhook %s failed: %s", event, exc)

        threading.Thread(target=_send, daemon=True).start()

    def _seed_customer_slots(self, nlu_user_id: str, customer: Dict[str, Any]) -> None:
        nlu = get_nlu_system()
        state = nlu.conversation_manager.get_conversation_state(nlu_user_id)
        slots = dict(state.collected_slots or {})
        name = str(customer.get("name") or "").strip()
        phone = str(customer.get("phone") or "").strip()
        email = str(customer.get("email") or "").strip()
        if name and not slots.get("customer_name"):
            slots["customer_name"] = name
        if phone and not slots.get("customer_phone"):
            slots["customer_phone"] = phone
        if email and not slots.get("customer_email"):
            slots["customer_email"] = email
        state.collected_slots = slots
        nlu.conversation_manager.persist(nlu_user_id)
        if phone:
            nlu.conversation_manager.remember_customer_identity(nlu_user_id, phone=phone, display_name=name or None)
        elif name:
            nlu.conversation_manager.remember_customer_identity(nlu_user_id, username=name, display_name=name)

    def _currency(self, user_id: str) -> str:
        user = self.db.query(User).filter(User.id == user_id).first()
        code = getattr(user, "currency_code", None) if user else None
        return (code or "GHS").upper()

    def _products_for_turn(self, user_id: str, intent: str, reply: str, currency: str, sub_business_id: str = "") -> List[Dict[str, Any]]:
        products = (
            self.db.query(Product)
            .filter(Product.user_id == user_id)
            .limit(100)
            .all()
        )
        products = for_sub_business(products, sub_business_id)
        active = [p for p in products if getattr(p, "is_active", True) is not False]
        if intent == "view_products":
            return [self._product_card(p, currency) for p in active[:30]]
        if intent in CUSTOMER_SHOP_INTENTS or intent == "view_product":
            reply_l = (reply or "").lower()
            matched = [p for p in active if (p.name or "").lower() in reply_l]
            return [self._product_card(p, currency) for p in matched[:12]]
        return []

    def _upsert_one(self, user_id: str, item: Dict[str, Any], sub_business_id: str = "") -> Product:
        external_id = str(item.get("external_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not external_id or not name:
            raise ValueError("external_id and name are required")
        kind = str(item.get("kind") or "product").strip().lower()
        if kind not in {"product", "service"}:
            raise ValueError("kind must be product or service")
        price_block = item.get("price") if isinstance(item.get("price"), dict) else {}
        amount = price_block.get("amount", item.get("price"))
        try:
            price = Decimal(str(amount if amount is not None else "0"))
        except Exception as exc:
            raise ValueError("price.amount must be a number") from exc
        if price < 0:
            raise ValueError("price.amount must be non-negative")
        stock = item.get("stock") if isinstance(item.get("stock"), dict) else {}
        tracked = bool(stock.get("tracked", kind == "product"))
        quantity = stock.get("quantity")
        stock_qty = None
        if tracked:
            if quantity is None:
                stock_qty = 0
            else:
                stock_qty = int(quantity)
                if stock_qty < 0:
                    raise ValueError("stock.quantity must be non-negative")
        active = bool(item.get("active", True))
        image = str(item.get("image_url") or "").strip() or _PLACEHOLDER_PHOTO
        link = str(item.get("link") or "").strip() or None
        description = str(item.get("description") or "").strip() or None
        category = str(item.get("category") or "").strip() or None

        product = (
            self.db.query(Product)
            .filter(Product.user_id == user_id, Product.external_id == external_id)
            .all()
        )
        product = next(
            (row for row in product if (row.sub_business_id or "") == (sub_business_id or "")),
            None,
        )
        if product is None:
            inventory_id = f"EXT-{user_id}-{_slug(sub_business_id or 'root')}-{_slug(external_id)}"[:100]
            if self.db.query(Product).filter(Product.inventory_id == inventory_id).first():
                inventory_id = f"{inventory_id[:90]}-{uuid.uuid4().hex[:8]}"
            product = Product(
                inventory_id=inventory_id,
                user_id=user_id,
                photo=image,
                name=name,
                description=description,
                price=price,
                category=category,
                condition="Service" if kind == "service" else "Available",
                number_in_stock=stock_qty,
                link=link,
                external_id=external_id,
                sub_business_id=sub_business_id or None,
                kind=kind,
                is_active=active,
                stock_tracked=tracked,
            )
            self.db.add(product)
            self.db.flush()
            self.db.add(ProductImage(product_id=product.product_id, url=image, sort_order=0, is_primary=True))
            self.db.add(
                Inventory(
                    product_id=product.product_id,
                    name=f"{name} - Default Inventory",
                    quantity_on_hand=stock_qty or 0,
                    last_counted_at=datetime.utcnow(),
                )
            )
        else:
            product.name = name
            product.description = description
            product.price = price
            product.category = category
            product.kind = kind
            product.is_active = active
            product.stock_tracked = tracked
            product.number_in_stock = stock_qty
            product.link = link
            product.photo = image
            product.condition = "Service" if kind == "service" else "Available"
            product.updated_at = datetime.utcnow()
        merchant = self.db.query(User).filter(User.id == user_id).first()
        if merchant is None:
            raise ValueError("Business not found")
        return product

    def _product_card(self, product: Product, currency: str = "GHS") -> Dict[str, Any]:
        items = catalog_items_from_products([product])
        stock = items[0].stock if items else product.number_in_stock
        from core.cloudstorage.service.storageservice import refresh_public_object_url

        photo = refresh_public_object_url(product.photo or "")
        if photo and "placeholder" in photo.lower():
            photo = ""
        return {
            "external_id": product.external_id or "",
            "sub_business_id": product.sub_business_id or "",
            "product_id": str(product.product_id),
            "kind": product.kind or "product",
            "name": product.name,
            "description": product.description or "",
            "price": str(product.price),
            "currency": currency,
            "stock": stock,
            "active": bool(product.is_active),
            "category": product.category or "",
            "image_url": photo,
        }

    def _order_card(self, order: Order, external_customer_id: Optional[str] = None) -> Dict[str, Any]:
        metadata = order.custom_metadata if isinstance(order.custom_metadata, dict) else {}
        items = order.order_items if isinstance(order.order_items, list) else []
        return {
            "order_id": str(order.order_id),
            "order_number": order.order_number,
            "source": "embed" if metadata.get("channel") == "embed" else (order.order_source or "chat"),
            "external_customer_id": external_customer_id or metadata.get("external_customer_id") or "",
            "conversation_id": metadata.get("conversation_id") or "",
            "sub_business": {
                "external_id": metadata.get("sub_business_id") or "",
                "name": metadata.get("sub_business_name") or "",
            }
            if metadata.get("sub_business_id")
            else None,
            "items": items,
            "total": str(order.total_amount),
            "currency": order.currency_code,
            "status": str(order.order_status.value if hasattr(order.order_status, "value") else order.order_status),
            "payment_status": str(order.payment_status.value if hasattr(order.payment_status, "value") else order.payment_status),
            "fulfillment_status": str(
                order.fulfillment_status.value if hasattr(order.fulfillment_status, "value") else order.fulfillment_status
            ),
        }
