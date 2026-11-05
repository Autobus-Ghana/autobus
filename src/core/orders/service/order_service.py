"""Order Service"""
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime, timezone
import logging
import re
import uuid
import secrets
import string

from core.orders.model.order import (
    Order, OrderType, OrderStatus, OrderPaymentStatus,
    FulfillmentStatus, OrderPaymentMethod, OrderSource
)
from core.orders.dto.order_create_dto import OrderCreateDTO
from core.orders.dto.order_update_dto import OrderUpdateDTO
from core.orders.dto.order_response_dto import OrderResponseDTO
from core.customers.model.customer import Customer
from core.customers.service.customer_service import CustomerService
from core.notification.service.event_notification_service import EventNotificationService
from core.product.service.product_service import ProductService
from core.user.model.User import User
from utilities.phone_utils import convert_to_local_ghana_format

logger = logging.getLogger(__name__)

# Order statuses the admin queue should surface (needs fulfillment / follow-up).
_ADMIN_ACTIVE_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.PROCESSING.value,
    OrderStatus.CONFIRMED.value,
)

class OrderService:
    """
    Service for managing customer orders.
    Handles CRUD operations and order lifecycle management.
    """

    def __init__(self, db: Session):
        self.db = db

    def _normalize_phone_like(self, value: str) -> str:
        cleaned = "".join(ch for ch in (value or "") if ch.isdigit())
        if cleaned.startswith("233") and len(cleaned) > 3:
            cleaned = "0" + cleaned[3:]
        elif cleaned and not cleaned.startswith("0") and len(cleaned) == 9:
            cleaned = "0" + cleaned
        return cleaned

    def _resolve_user_db_id(self, user_identifier: str) -> Optional[str]:
        if not user_identifier:
            return None

        user = self.db.query(User).filter(User.id == user_identifier).first()
        if user:
            return user.id

        user = self.db.query(User).filter(User.email == user_identifier).first()
        if user:
            return user.id

        normalized_phone = self._normalize_phone_like(user_identifier)
        phone_candidates = {user_identifier}
        if normalized_phone:
            phone_candidates.add(normalized_phone)

        user = self.db.query(User).filter(User.phone.in_(list(phone_candidates))).first()
        if user:
            return user.id

        if ":" in user_identifier:
            merchant_id, _, rest = user_identifier.partition(":")
            merchant_id = merchant_id.strip()
            if merchant_id and (rest or "").strip() and merchant_id != user_identifier:
                return self._resolve_user_db_id(merchant_id)
        return None

    @staticmethod
    def _generate_order_number() -> str:
        """Generate a unique order number."""
        timestamp = datetime.utcnow().strftime("%Y%m%d")
        random_suffix = ''.join(secrets.choice(string.digits) for _ in range(5))
        return f"ORD-{timestamp}-{random_suffix}"

    def create_order(
        self, order_data: OrderCreateDTO, user_id: Optional[str] = None
    ) -> Tuple[bool, Optional[Order], str]:
        """
        Create a new order.

        Args:
            order_data: OrderCreateDTO with order details

        Returns:
            Tuple of (success, order_object, message)
        """
        try:
            logger.info(f"[ORDER_SERVICE] Creating order for customer phone: {order_data.customer_phone}")

            resolved_user_id = self._resolve_user_db_id(user_id) if user_id else None

            product_service = ProductService(self.db)
            listed_product = (
                product_service.find_product_for_user(order_data.item_name, resolved_user_id)
                if resolved_user_id
                else None
            )
            product_listed = listed_product is not None

            order_metadata = dict(order_data.custom_metadata or {})
            order_metadata["product_listed"] = product_listed
            if not product_listed:
                order_metadata["requires_cs_followup"] = True

            # Validate order type
            try:
                OrderType(order_data.order_type)
            except ValueError:
                return False, None, f"Invalid order_type: {order_data.order_type}"

            # Validate optional fields
            if order_data.order_source:
                try:
                    OrderSource(order_data.order_source)
                except ValueError:
                    return False, None, f"Invalid order_source: {order_data.order_source}"

            if order_data.payment_method:
                try:
                    OrderPaymentMethod(order_data.payment_method)
                except ValueError:
                    return False, None, f"Invalid payment_method: {order_data.payment_method}"

            # Calculate total amount (cap discount so unpriced / partial orders stay valid)
            billable_base = (
                order_data.subtotal_amount +
                order_data.tax_amount +
                order_data.shipping_amount
            )
            effective_discount = order_data.discount_amount
            if effective_discount > billable_base:
                logger.warning(
                    "[ORDER_SERVICE] Discount %s exceeds billable base %s; capping discount",
                    effective_discount,
                    billable_base,
                )
                effective_discount = billable_base

            total_amount = billable_base - effective_discount
            total_quantity = int(order_data.quantity)

            if total_amount < 0:
                return False, None, "Total amount cannot be negative"

            # Generate unique order number
            order_number = self._generate_order_number()

            # Create order
            order = Order(
                order_number=order_number,
                user_id=resolved_user_id,
                customer_id=None,
                customer_name=order_data.customer_name,
                customer_phone=order_data.customer_phone,
                customer_email=order_data.customer_email,
                customer_location=order_data.customer_location,
                order_type=order_data.order_type,
                # Persist as structured JSON internally while exposing simplified DTO fields.
                order_items=[{"name": order_data.item_name, "quantity": int(order_data.quantity)}],
                order_status=OrderStatus.PENDING.value,
                payment_status=OrderPaymentStatus.PENDING.value,
                fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                subtotal_amount=order_data.subtotal_amount,
                total_quantity=total_quantity,
                discount_amount=effective_discount,
                tax_amount=order_data.tax_amount,
                shipping_amount=order_data.shipping_amount,
                total_amount=total_amount,
                currency_code=order_data.currency_code,
                payment_method=order_data.payment_method,
                payment_reference=order_data.payment_reference,
                payment_details=order_data.payment_details,
                order_source=order_data.order_source,
                notes=order_data.notes,
                tags=order_data.tags,
                custom_metadata=order_metadata,
            )

            self.db.add(order)
            self.db.commit()
            self.db.refresh(order)

            logger.info(f"[ORDER_SERVICE] Order created successfully: {order.order_number}")

            try:
                EventNotificationService(self.db).notify_order_created(order)
            except Exception as notify_exc:
                logger.error(
                    "[ORDER_SERVICE] Order created but notification failed: %s",
                    notify_exc,
                    exc_info=True,
                )

            return True, order, f"Order {order.order_number} created successfully!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error creating order: {str(e)}", exc_info=True)
            return False, None, f"Error creating order: {str(e)}"

    def get_order_by_id(self, order_id: str) -> Optional[Order]:
        """Get an order by its ID."""
        try:
            logger.info(f"[ORDER_SERVICE] Fetching order: {order_id}")
            order = self.db.query(Order).filter(Order.order_id == uuid.UUID(order_id)).first()
            return order
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching order: {str(e)}", exc_info=True)
            return None

    def get_order_by_number(self, order_number: str) -> Optional[Order]:
        """Get an order by its order number."""
        try:
            logger.info(f"[ORDER_SERVICE] Fetching order by number: {order_number}")
            order = self.db.query(Order).filter(Order.order_number == order_number).first()
            return order
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching order: {str(e)}", exc_info=True)
            return None

    def get_customer_orders(self, customer_id: str, skip: int = 0, limit: int = 100) -> List[Order]:
        """Get all orders for a customer."""
        try:
            logger.info(f"[ORDER_SERVICE] Fetching orders for customer: {customer_id}")
            orders = self.db.query(Order).filter(
                Order.customer_id == customer_id
            ).order_by(desc(Order.order_date)).offset(skip).limit(limit).all()
            logger.info(f"[ORDER_SERVICE] Found {len(orders)} orders for customer")
            return orders
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching customer orders: {str(e)}", exc_info=True)
            return []

    def get_orders_by_user(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        order_status: Optional[str] = None,
    ) -> List[Order]:
        """Get orders for the authenticated merchant (users.id, email, or phone)."""
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                logger.info(f"[ORDER_SERVICE] No user found for identifier: {user_id}")
                return []

            query = self.db.query(Order).filter(Order.user_id == resolved_user_id)

            if order_status:
                try:
                    OrderStatus(order_status)
                    query = query.filter(Order.order_status == order_status)
                except ValueError:
                    logger.warning(f"[ORDER_SERVICE] Invalid order_status filter: {order_status}")

            orders = (
                query.order_by(desc(Order.order_date)).offset(skip).limit(limit).all()
            )
            logger.info(
                f"[ORDER_SERVICE] Found {len(orders)} orders for user {resolved_user_id}"
            )
            return orders
        except Exception as e:
            logger.error(
                f"[ORDER_SERVICE] Error fetching orders for user: {str(e)}", exc_info=True
            )
            return []

    def get_all_orders(self, skip: int = 0, limit: int = 100, order_status: Optional[str] = None) -> List[Order]:
        """Get all orders with optional filtering."""
        try:
            query = self.db.query(Order)
            
            if order_status:
                try:
                    OrderStatus(order_status)
                    query = query.filter(Order.order_status == order_status)
                except ValueError:
                    logger.warning(f"[ORDER_SERVICE] Invalid order_status filter: {order_status}")

            orders = query.order_by(desc(Order.order_date)).offset(skip).limit(limit).all()
            logger.info(f"[ORDER_SERVICE] Found {len(orders)} orders")
            return orders
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching orders: {str(e)}", exc_info=True)
            return []

    def get_admin_active_orders(self, user_id: str, skip: int = 0, limit: int = 100) -> List[Order]:
        """Orders for this merchant that still need attention (not completed or cancelled)."""
        try:
            orders = (
                self.db.query(Order)
                .filter(
                    Order.user_id == user_id,
                    Order.order_status.in_(_ADMIN_ACTIVE_STATUSES),
                )
                .order_by(desc(Order.order_date))
                .offset(skip)
                .limit(limit)
                .all()
            )
            logger.info(f"[ORDER_SERVICE] Found {len(orders)} admin active orders for user {user_id}")
            return orders
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching admin active orders: {str(e)}", exc_info=True)
            return []

    def get_admin_completed_orders(self, user_id: str, skip: int = 0, limit: int = 100) -> List[Order]:
        """Orders finished successfully for this merchant."""
        try:
            orders = (
                self.db.query(Order)
                .filter(
                    Order.user_id == user_id,
                    Order.order_status == OrderStatus.COMPLETED.value,
                )
                .order_by(desc(Order.order_date))
                .offset(skip)
                .limit(limit)
                .all()
            )
            logger.info(f"[ORDER_SERVICE] Found {len(orders)} admin completed orders for user {user_id}")
            return orders
        except Exception as e:
            logger.error(f"[ORDER_SERVICE] Error fetching admin completed orders: {str(e)}", exc_info=True)
            return []

    def update_order(self, order_id: str, update_data: OrderUpdateDTO) -> Tuple[bool, Optional[Order], str]:
        """
        Update an existing order.

        Args:
            order_id: Order ID
            update_data: OrderUpdateDTO with fields to update

        Returns:
            Tuple of (success, order_object, message)
        """
        try:
            logger.info(f"[ORDER_SERVICE] Updating order: {order_id}")

            order = self.get_order_by_id(order_id)
            if not order:
                return False, None, "Order not found"

            # Validate statuses if provided
            if update_data.order_status:
                try:
                    OrderStatus(update_data.order_status)
                except ValueError:
                    return False, None, f"Invalid order_status: {update_data.order_status}"

            if update_data.payment_status:
                try:
                    OrderPaymentStatus(update_data.payment_status)
                except ValueError:
                    return False, None, f"Invalid payment_status: {update_data.payment_status}"

            if update_data.fulfillment_status:
                try:
                    FulfillmentStatus(update_data.fulfillment_status)
                except ValueError:
                    return False, None, f"Invalid fulfillment_status: {update_data.fulfillment_status}"

            if update_data.payment_method:
                try:
                    OrderPaymentMethod(update_data.payment_method)
                except ValueError:
                    return False, None, f"Invalid payment_method: {update_data.payment_method}"

            # Update fields
            if update_data.order_status:
                order.order_status = update_data.order_status

            if update_data.payment_status:
                order.payment_status = update_data.payment_status
                # Set payment_date if status changed to paid
                if update_data.payment_status in [OrderPaymentStatus.PAID.value]:
                    order.payment_date = datetime.utcnow()

            if update_data.fulfillment_status:
                order.fulfillment_status = update_data.fulfillment_status
                # Set fulfillment_date if status changed to fulfilled/shipped
                if update_data.fulfillment_status in [
                    FulfillmentStatus.FULFILLED.value,
                    FulfillmentStatus.SHIPPED.value,
                    FulfillmentStatus.DELIVERED.value
                ]:
                    if update_data.fulfillment_status == FulfillmentStatus.DELIVERED.value:
                        order.delivery_date = datetime.utcnow()
                    else:
                        order.fulfillment_date = datetime.utcnow()

            if update_data.subtotal_amount:
                order.subtotal_amount = update_data.subtotal_amount
            if update_data.quantity is not None:
                order.total_quantity = update_data.quantity

            if update_data.discount_amount is not None:
                order.discount_amount = update_data.discount_amount

            if update_data.tax_amount is not None:
                order.tax_amount = update_data.tax_amount

            if update_data.shipping_amount is not None:
                order.shipping_amount = update_data.shipping_amount

            if update_data.payment_method:
                order.payment_method = update_data.payment_method

            if update_data.payment_reference:
                order.payment_reference = update_data.payment_reference

            if update_data.payment_details:
                order.payment_details = update_data.payment_details
            if update_data.item_name is not None or update_data.quantity is not None:
                current_item = {}
                if isinstance(order.order_items, list) and order.order_items and isinstance(order.order_items[0], dict):
                    current_item = order.order_items[0].copy()
                elif isinstance(order.order_items, dict):
                    current_item = order.order_items.copy()

                if update_data.item_name is not None:
                    current_item["name"] = update_data.item_name
                if update_data.quantity is not None:
                    current_item["quantity"] = int(update_data.quantity)
                    order.total_quantity = int(update_data.quantity)

                order.order_items = [current_item]

            if update_data.customer_email:
                order.customer_email = update_data.customer_email
            if update_data.customer_name:
                order.customer_name = update_data.customer_name
            if update_data.customer_phone:
                order.customer_phone = update_data.customer_phone
            if update_data.customer_location is not None:
                order.customer_location = update_data.customer_location

            if update_data.notes is not None:
                order.notes = update_data.notes

            if update_data.tags is not None:
                order.tags = update_data.tags

            if update_data.custom_metadata is not None:
                order.custom_metadata = update_data.custom_metadata

            # Recalculate total if amounts changed
            if any([
                update_data.subtotal_amount,
                update_data.discount_amount is not None,
                update_data.tax_amount is not None,
                update_data.shipping_amount is not None
            ]):
                order.total_amount = (
                    order.subtotal_amount +
                    order.tax_amount +
                    order.shipping_amount -
                    order.discount_amount
                )

            order.updated_at = datetime.utcnow()

            self.db.commit()
            self.db.refresh(order)

            logger.info(f"[ORDER_SERVICE] Order updated successfully: {order.order_number}")
            return True, order, f"Order {order.order_number} updated successfully!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error updating order: {str(e)}", exc_info=True)
            return False, None, f"Error updating order: {str(e)}"

    def cancel_order(self, order_id: str, reason: Optional[str] = None) -> Tuple[bool, Optional[Order], str]:
        """Cancel an order."""
        try:
            logger.info(f"[ORDER_SERVICE] Cancelling order: {order_id}")

            order = self.get_order_by_id(order_id)
            if not order:
                return False, None, "Order not found"

            if order.order_status == OrderStatus.CANCELLED.value:
                return False, None, "Order is already cancelled"

            if order.order_status == OrderStatus.COMPLETED.value:
                return False, None, "Cannot cancel a completed order"

            order.order_status = OrderStatus.CANCELLED.value
            if reason:
                order.notes = f"{order.notes or ''}\nCancellation reason: {reason}".strip()

            order.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(order)

            logger.info(f"[ORDER_SERVICE] Order cancelled successfully: {order.order_number}")
            return True, order, f"Order {order.order_number} cancelled successfully!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error cancelling order: {str(e)}", exc_info=True)
            return False, None, f"Error cancelling order: {str(e)}"

    def complete_order(self, order_id: str) -> Tuple[bool, Optional[Order], str]:
        """Mark an order as completed."""
        try:
            logger.info(f"[ORDER_SERVICE] Completing order: {order_id}")

            order = self.get_order_by_id(order_id)
            if not order:
                return False, None, "Order not found"

            if order.order_status == OrderStatus.CANCELLED.value:
                return False, None, "Cannot complete a cancelled order"

            if order.order_status == OrderStatus.COMPLETED.value:
                return False, None, "Order is already completed"

            order.order_status = OrderStatus.COMPLETED.value
            order.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(order)

            logger.info(f"[ORDER_SERVICE] Order completed successfully: {order.order_number}")
            return True, order, f"Order {order.order_number} marked as completed!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error completing order: {str(e)}", exc_info=True)
            return False, None, f"Error completing order: {str(e)}"

    def delete_order(self, order_id: str) -> Tuple[bool, str]:
        """Delete an order (hard delete)."""
        try:
            logger.info(f"[ORDER_SERVICE] Deleting order: {order_id}")

            order = self.get_order_by_id(order_id)
            if not order:
                return False, "Order not found"

            self.db.delete(order)
            self.db.commit()

            logger.info(f"[ORDER_SERVICE] Order deleted successfully: {order_id}")
            return True, "Order deleted successfully!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error deleting order: {str(e)}", exc_info=True)
            return False, f"Error deleting order: {str(e)}"

    def _merchant_owns_order(self, order: Order, merchant_user_id: str) -> bool:
        if not order.user_id:
            return True
        resolved = self._resolve_user_db_id(merchant_user_id)
        if not resolved:
            return False
        return str(order.user_id) == str(resolved)

    @staticmethod
    def _customer_number_from_order_phone(phone_raw: str) -> Optional[str]:
        """Fit an order phone (often a WhatsApp id) into customers.customer_number."""
        local = convert_to_local_ghana_format(phone_raw) or ""
        digits = re.sub(r"\D", "", local)
        if digits:
            if len(digits) > 20:
                digits = digits[-20:]
            return digits
        compact = re.sub(r"\s+", "", phone_raw).strip()
        return compact[:20] or None

    @staticmethod
    def _usable_contact_value(value: Optional[str]) -> Optional[str]:
        text = (value or "").strip()
        if not text:
            return None
        if text.upper() in {"N/A", "NA", "NONE", "NULL", "-", "UNKNOWN"}:
            return None
        return text

    def save_customer_from_order(
        self, order_id: str, merchant_user_id: str
    ) -> Tuple[bool, Optional[Customer], Optional[Order], bool, str]:
        """
        Copy contact fields from a received order into the merchant's customers list
        and link the order via customer_id.

        Returns (success, customer, order, created, message).
        """
        try:
            order = self.get_order_by_id(order_id)
            if not order:
                return False, None, None, False, "Order not found"

            if not self._merchant_owns_order(order, merchant_user_id):
                return False, None, None, False, "You do not have permission to save a customer from this order."

            resolved_user_id = self._resolve_user_db_id(merchant_user_id)
            if not resolved_user_id:
                return False, None, None, False, "User not found. Please log in again."

            customer_service = CustomerService(self.db)
            existing_linked = None
            if order.customer_id:
                try:
                    existing_linked = customer_service.get_customer(int(order.customer_id), resolved_user_id)
                except (TypeError, ValueError):
                    existing_linked = None
            if existing_linked:
                return True, existing_linked, order, False, f"Customer '{existing_linked.name}' is already saved from this order."

            phone_raw = self._usable_contact_value(order.customer_phone)
            if not phone_raw:
                return False, None, order, False, "This order has no customer phone number to save."

            local_phone = self._customer_number_from_order_phone(phone_raw)
            if not local_phone:
                return False, None, order, False, "This order has no customer phone number to save."

            name = (self._usable_contact_value(order.customer_name) or "Customer")[:100]
            email = self._usable_contact_value(order.customer_email)
            if email and "@" not in email:
                email = None
            if email:
                email = email[:255]

            customer = customer_service.find_customer_by_phone(resolved_user_id, local_phone)
            created = False
            if customer:
                if email and not customer.email:
                    customer.email = email
                    customer.updated_at = datetime.now()
                    self.db.commit()
                    self.db.refresh(customer)
            else:
                success, customer, message = customer_service.add_customer(
                    user_id=resolved_user_id,
                    name=name,
                    customer_number=local_phone,
                    email=email,
                    allow_unknown_network=True,
                )
                if not success:
                    # Race / duplicate stored under a slightly different format.
                    customer = customer_service.find_customer_by_phone(resolved_user_id, local_phone)
                    if not customer:
                        if (message or "").lower().startswith("error saving customer"):
                            message = "Could not save this customer from the order. Check the phone number and try again."
                        return False, None, order, False, message
                else:
                    created = True

            if not customer:
                return False, None, order, False, "Could not save this customer from the order."

            order.customer_id = str(customer.id)
            order.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(order)

            if created:
                message = f"Customer '{customer.name}' saved from this order."
            else:
                message = f"Customer '{customer.name}' was already in your list and is now linked to this order."

            logger.info(
                "[ORDER_SERVICE] Saved customer %s from order %s (created=%s)",
                customer.id,
                order.order_number,
                created,
            )
            return True, customer, order, created, message

        except Exception as e:
            self.db.rollback()
            logger.error(f"[ORDER_SERVICE] Error saving customer from order: {str(e)}", exc_info=True)
            return False, None, None, False, "Could not save this customer from the order. Check the phone number and try again."

