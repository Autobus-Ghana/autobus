import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from another_fastapi_jwt_auth import AuthJWT
from core.billing.dto.request.billing_create import BillingCreateRequest
from core.billing.dto.response.billing_paginated_response import BillingPaginatedResponse
from core.billing.dto.response.billing_response import BillingResponse
from core.billing.exceptions.billing_exceptions import BillingNotFoundException
from core.billing.service.billing_service import BillingService
from core.user.controller.usercontroller import get_db, validate_token
from core.user.service.user_service import UserService

logger = logging.getLogger(__name__)

billing_routes = APIRouter()


@billing_routes.post("/", response_model=BillingResponse)
async def create_billing(
    request: BillingCreateRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """
    Create a standalone bill and return a Paystack payment link for the customer.
    """
    user_service = UserService(db)
    user = user_service.get_current_user(authjwt.get_jwt_subject())

    billing_service = BillingService(db)
    return await billing_service.create_billing(request, created_by_user_id=user.id)


@billing_routes.get("/", response_model=BillingPaginatedResponse)
def list_billings(
    page: int = 0,
    size: int = 20,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user = UserService(db).get_current_user(authjwt.get_jwt_subject())
    billing_service = BillingService(db)
    return billing_service.list_billings(
        page=page,
        size=size,
        created_by_user_id=user.id,
    )


@billing_routes.get("/public/{reference}", response_model=BillingResponse)
def get_public_billing_checkout(
    reference: str,
    db: Session = Depends(get_db),
):
    """
    Public endpoint for end customers to retrieve bill details and the Paystack payment URL.
    No authentication required.
    """
    billing_service = BillingService(db)
    return billing_service.get_by_reference(reference)


@billing_routes.get("/reference/{reference}", response_model=BillingResponse)
def get_billing_by_reference(
    reference: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    billing_service = BillingService(db)
    return billing_service.get_by_reference(reference)


@billing_routes.get("/external/{external_id}", response_model=BillingResponse)
def get_billing_by_external_id(
    external_id: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    billing_service = BillingService(db)
    return billing_service.get_by_external_id(external_id)


@billing_routes.post("/reference/{reference}/verify", response_model=BillingResponse)
async def verify_billing_payment(
    reference: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Verify payment status with Paystack and sync the billing record."""
    billing_service = BillingService(db)
    return await billing_service.verify_and_sync(reference)


@billing_routes.post("/webhook/paystack")
async def paystack_billing_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_paystack_signature: str = Header(None, alias="x-paystack-signature"),
):
    """
    Paystack webhook for billing charge lifecycle updates.
    Configure this URL in your Paystack dashboard.
    """
    body = await request.body()
    billing_service = BillingService(db)
    try:
        return billing_service.handle_paystack_webhook(body, x_paystack_signature or "")
    except BillingNotFoundException as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[BILLING_WEBHOOK_ERROR] %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@billing_routes.get("/{billing_id}", response_model=BillingResponse)
def get_billing_by_id(
    billing_id: int,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    billing_service = BillingService(db)
    return billing_service.get_by_id(billing_id)
