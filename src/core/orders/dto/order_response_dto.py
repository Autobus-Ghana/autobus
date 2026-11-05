"""Order Response DTO"""
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from decimal import Decimal


class OrderResponseDTO(BaseModel):
    """Response model for order details."""
    
    # Primary Identifiers
    order_id: str
    order_number: str
    user_id: Optional[str] = None

    # Customer Relationship
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    customer_location: Optional[str] = None
    
    # Order Details
    order_type: str
    item_name: Optional[str] = None
    quantity: Optional[int] = None
    order_status: str
    payment_status: str
    fulfillment_status: str
    
    # Financials
    subtotal_amount: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    shipping_amount: Decimal
    total_amount: Decimal
    currency_code: str
    
    # Payment Details
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    payment_details: Optional[dict] = None
    
    # Timestamps
    order_date: datetime
    payment_date: Optional[datetime] = None
    fulfillment_date: Optional[datetime] = None
    delivery_date: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    
    # Metadata
    order_source: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    custom_metadata: Optional[dict] = None

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "order_id": "550e8400-e29b-41d4-a716-446655440000",
                "order_number": "ORD-2026-00001",
                "customer_id": None,
                "customer_name": "Jane Doe",
                "customer_phone": "+233201234567",
                "customer_email": "customer@example.com",
                "customer_location": "Accra, Ghana",
                "order_type": "sale",
                "item_name": "Rice Bag 5kg",
                "quantity": 2,
                "order_status": "confirmed",
                "payment_status": "paid",
                "fulfillment_status": "shipped",
                "subtotal_amount": "150.00",
                "discount_amount": "10.00",
                "tax_amount": "20.00",
                "shipping_amount": "5.00",
                "total_amount": "165.00",
                "currency_code": "GHS",
                "payment_method": "card",
                "payment_reference": "TXN-12345",
                "payment_details": {"provider": "paystack", "reference": "TXN-12345"},
                "order_date": "2026-03-20T10:30:00Z",
                "payment_date": "2026-03-20T10:35:00Z",
                "fulfillment_date": "2026-03-21T08:00:00Z",
                "delivery_date": None,
                "created_at": "2026-03-20T10:30:00Z",
                "updated_at": "2026-03-21T08:00:00Z",
                "order_source": "web",
                "notes": "Handle with care",
                "tags": ["urgent", "vip"],
                "custom_metadata": {"campaign_id": "spring-sale-2026"}
            }
        }

    @classmethod
    def from_order(cls, order):
        """Convert Order model to response DTO."""
        item_name = None
        quantity = None
        if isinstance(order.order_items, list) and order.order_items:
            first_item = order.order_items[0]
            if isinstance(first_item, dict):
                item_name = first_item.get("name")
                quantity = first_item.get("quantity")
        elif isinstance(order.order_items, dict):
            item_name = order.order_items.get("name")
            quantity = order.order_items.get("quantity")

        quantity = _as_int(quantity)
        if quantity is None:
            quantity = _as_int(getattr(order, "total_quantity", None))

        return cls(
            order_id=str(order.order_id),
            order_number=order.order_number,
            user_id=getattr(order, "user_id", None),
            customer_id=order.customer_id,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            customer_email=order.customer_email,
            customer_location=order.customer_location,
            order_type=_enum_text(order.order_type) or "sale",
            item_name=item_name,
            quantity=quantity,
            order_status=_enum_text(order.order_status) or "pending",
            payment_status=_enum_text(order.payment_status) or "pending",
            fulfillment_status=_enum_text(order.fulfillment_status) or "unfulfilled",
            subtotal_amount=_money(order.subtotal_amount),
            discount_amount=_money(order.discount_amount),
            tax_amount=_money(order.tax_amount),
            shipping_amount=_money(order.shipping_amount),
            total_amount=_money(order.total_amount),
            currency_code=order.currency_code or "GHS",
            payment_method=_enum_text(order.payment_method),
            payment_reference=order.payment_reference,
            payment_details=order.payment_details if isinstance(order.payment_details, dict) else None,
            order_date=order.order_date or order.created_at or datetime.utcnow(),
            payment_date=order.payment_date,
            fulfillment_date=order.fulfillment_date,
            delivery_date=order.delivery_date,
            created_at=order.created_at or order.order_date or datetime.utcnow(),
            updated_at=order.updated_at or order.created_at or order.order_date or datetime.utcnow(),
            order_source=_enum_text(order.order_source),
            notes=order.notes,
            tags=[str(tag) for tag in order.tags] if isinstance(order.tags, list) else None,
            custom_metadata=order.custom_metadata if isinstance(order.custom_metadata, dict) else None,
        )


def _enum_text(value):
    if value is None:
        return None
    if hasattr(value, "value"):
        return value.value
    text = str(value).strip()
    return text or None


def _money(value):
    return 0 if value is None else value


def _as_int(value):
    """Coerce JSON quantities (including 2.0) into an int for the response model."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None
