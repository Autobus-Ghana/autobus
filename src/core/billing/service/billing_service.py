import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, desc, or_
from sqlalchemy.orm import Session

from core.billing.dto.request.billing_create import BillingCreateRequest
from core.billing.dto.response.billing_paginated_response import BillingPaginatedResponse
from core.billing.dto.response.billing_response import BillingResponse
from core.billing.exceptions.billing_exceptions import (
    BillingNotFoundException,
    BillingValidationException,
)
from core.billing.model.billing_charge import BillingCharge, BillingChargeStatus, BillingSourceType
from core.orders.model.order import Order
from core.billing.service.paystack_checkout_client import PaystackCheckoutClient

logger = logging.getLogger(__name__)


class BillingService:
    """Standalone billing orchestration with Paystack payment links."""

    def __init__(self, db: Session):
        self.db = db
        self.paystack = PaystackCheckoutClient()

    async def create_billing(
        self,
        request: BillingCreateRequest,
        created_by_user_id: Optional[str] = None,
    ) -> BillingResponse:
        amount = Decimal(str(request.amount))
        if amount <= 0:
            raise BillingValidationException("Amount must be greater than zero")

        amount_subunit = self.paystack.to_subunit(float(amount), request.currency)
        reference = self.paystack.generate_reference()

        metadata = {
            "billing_source_type": request.source_type.value,
            "external_id": request.external_id,
            **(request.metadata or {}),
        }

        checkout = await self.paystack.initialize_checkout(
            email=request.customer_email,
            amount_subunit=amount_subunit,
            reference=reference,
            callback_url=request.callback_url,
            metadata=metadata,
        )

        charge = BillingCharge(
            reference=checkout.reference,
            external_id=request.external_id,
            source_type=request.source_type,
            customer_email=request.customer_email,
            customer_name=request.customer_name,
            description=request.description,
            currency=request.currency.upper(),
            amount=float(amount),
            amount_subunit=amount_subunit,
            status=BillingChargeStatus.PENDING,
            payment_url=checkout.authorization_url,
            access_code=checkout.access_code,
            charge_metadata=metadata,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(charge)
        self.db.commit()
        self.db.refresh(charge)

        logger.info(
            "[BILLING_CREATED] reference=%s amount=%s %s payment_url set",
            charge.reference,
            charge.amount,
            charge.currency,
        )
        return BillingResponse.from_charge(charge)

    def create_billing_sync(
        self,
        request: BillingCreateRequest,
        created_by_user_id: Optional[str] = None,
    ) -> BillingResponse:
        """Synchronous billing creation for NLU / non-async callers."""
        amount = Decimal(str(request.amount))
        if amount <= 0:
            raise BillingValidationException("Amount must be greater than zero")

        amount_subunit = self.paystack.to_subunit(float(amount), request.currency)
        reference = self.paystack.generate_reference()

        metadata = {
            "billing_source_type": request.source_type.value,
            "external_id": request.external_id,
            **(request.metadata or {}),
        }

        checkout = self.paystack.initialize_checkout_sync(
            email=request.customer_email,
            amount_subunit=amount_subunit,
            reference=reference,
            callback_url=request.callback_url,
            metadata=metadata,
        )

        charge = BillingCharge(
            reference=checkout.reference,
            external_id=request.external_id,
            source_type=request.source_type,
            customer_email=request.customer_email,
            customer_name=request.customer_name,
            description=request.description,
            currency=request.currency.upper(),
            amount=float(amount),
            amount_subunit=amount_subunit,
            status=BillingChargeStatus.PENDING,
            payment_url=checkout.authorization_url,
            access_code=checkout.access_code,
            charge_metadata=metadata,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(charge)
        self.db.commit()
        self.db.refresh(charge)
        return BillingResponse.from_charge(charge)

    def get_by_id(self, billing_id: int) -> BillingResponse:
        charge = self.db.query(BillingCharge).filter(BillingCharge.id == billing_id).first()
        if not charge:
            raise BillingNotFoundException(f"Billing not found with id: {billing_id}")
        return BillingResponse.from_charge(charge)

    def get_by_reference(self, reference: str) -> BillingResponse:
        charge = (
            self.db.query(BillingCharge)
            .filter(BillingCharge.reference == reference)
            .first()
        )
        if not charge:
            raise BillingNotFoundException(f"Billing not found with reference: {reference}")
        return BillingResponse.from_charge(charge)

    def get_by_external_id(self, external_id: str) -> BillingResponse:
        charge = (
            self.db.query(BillingCharge)
            .filter(BillingCharge.external_id == external_id)
            .order_by(desc(BillingCharge.created_on))
            .first()
        )
        if not charge:
            raise BillingNotFoundException(
                f"Billing not found for external_id: {external_id}"
            )
        return BillingResponse.from_charge(charge)

    def list_billings(
        self,
        page: int,
        size: int,
        created_by_user_id: Optional[str] = None,
    ) -> BillingPaginatedResponse:
        owner_id = (created_by_user_id or "").strip()
        if not owner_id:
            return BillingPaginatedResponse(
                items=[],
                total=0,
                page=page,
                size=size,
                has_next=False,
                has_prev=page > 0,
            )
        query = self.db.query(BillingCharge).filter(self._owned_by_merchant(owner_id))
        total = query.count()
        charges = (
            query.order_by(desc(BillingCharge.created_on))
            .offset(page * size)
            .limit(size)
            .all()
        )
        return BillingPaginatedResponse(
            items=[BillingResponse.from_charge(c) for c in charges],
            total=total,
            page=page,
            size=size,
            has_next=(page + 1) * size < total,
            has_prev=page > 0,
        )

    def _owned_by_merchant(self, user_id: str):
        """Charges this merchant created, plus order invoices for their orders."""
        owned_order_ids = [
            str(order_id)
            for (order_id,) in self.db.query(Order.order_id)
            .filter(Order.user_id == user_id)
            .all()
        ]
        filters = [BillingCharge.created_by_user_id == user_id]
        if owned_order_ids:
            filters.append(
                and_(
                    BillingCharge.source_type == BillingSourceType.ORDER,
                    BillingCharge.external_id.in_(owned_order_ids),
                )
            )
        return or_(*filters)

    async def verify_and_sync(self, reference: str) -> BillingResponse:
        charge = (
            self.db.query(BillingCharge)
            .filter(BillingCharge.reference == reference)
            .first()
        )
        if not charge:
            raise BillingNotFoundException(f"Billing not found with reference: {reference}")

        result = await self.paystack.verify_transaction(reference)
        if result.get("status") and result.get("data"):
            self._apply_paystack_data(charge, result["data"])

        self.db.add(charge)
        self.db.commit()
        self.db.refresh(charge)
        return BillingResponse.from_charge(charge)

    def handle_paystack_webhook(self, payload: bytes, signature: str) -> BillingResponse:
        secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
        if not secret:
            raise BillingValidationException("PAYSTACK_SECRET_KEY is not configured")

        computed = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(computed, signature or ""):
            raise BillingValidationException("Invalid Paystack webhook signature")

        event = json.loads(payload.decode("utf-8"))
        event_type = event.get("event")
        data = event.get("data") or {}
        reference = data.get("reference")
        if not reference:
            raise BillingValidationException("Webhook payload missing transaction reference")

        charge = (
            self.db.query(BillingCharge)
            .filter(BillingCharge.reference == reference)
            .first()
        )
        if not charge:
            raise BillingNotFoundException(f"Billing not found for reference: {reference}")

        if event_type == "charge.success":
            self._apply_paystack_data(charge, data)
        elif event_type in {"charge.failed", "transfer.failed"}:
            charge.status = BillingChargeStatus.FAILED
            charge.paystack_status = data.get("status")
            charge.gateway_response = data.get("gateway_response")

        self.db.add(charge)
        self.db.commit()
        self.db.refresh(charge)
        logger.info(
            "[BILLING_WEBHOOK] reference=%s event=%s status=%s",
            reference,
            event_type,
            charge.status.value,
        )
        return BillingResponse.from_charge(charge)

    def _apply_paystack_data(self, charge: BillingCharge, data: dict[str, Any]) -> None:
        paystack_status = (data.get("status") or "").lower()
        charge.paystack_status = paystack_status
        charge.gateway_response = data.get("gateway_response")

        if paystack_status == "success":
            charge.status = BillingChargeStatus.PAID
            paid_at = data.get("paid_at") or data.get("paidAt")
            if paid_at:
                try:
                    charge.paid_at = datetime.fromisoformat(
                        paid_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    charge.paid_at = datetime.utcnow()
            else:
                charge.paid_at = datetime.utcnow()
        elif paystack_status == "failed":
            charge.status = BillingChargeStatus.FAILED
