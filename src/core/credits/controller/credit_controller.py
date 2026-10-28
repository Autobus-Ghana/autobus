from fastapi import APIRouter, Depends, HTTPException, Query, status
from another_fastapi_jwt_auth import AuthJWT
from sqlalchemy.orm import Session

from core.credits.credit_catalog import get_pack, packs_public
from core.credits.usd_ghs import usd_to_ghs
from core.credits.dto.credit_response import (
    CreditBalanceItem,
    CreditCheckoutRequest,
    CreditConsumptionResponse,
    CreditPackItem,
    UserCreditsResponse,
    WalletBalance,
)
from core.credits.service.credit_service import CreditService
from core.paystack.dto.request.paystack_request import PaystackInitializeRequest
from core.paystack.service.paystack_service import PaystackService
from core.user.controller.usercontroller import validate_token, get_db
from core.user.service.user_service import UserService

credit_routes = APIRouter()


@credit_routes.get("/me", response_model=UserCreditsResponse)
def get_my_credits(
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Current user's unified wallet and per-feature remaining actions."""
    user = UserService(db).get_current_user(authjwt.get_jwt_subject())
    data = CreditService(db).get_user_credits(user.id)
    credits = {
        key: CreditBalanceItem(**value) for key, value in data["credits"].items()
    }
    wallet = data.get("wallet") or {}
    return UserCreditsResponse(
        user_id=data["user_id"],
        plan_id=data.get("plan_id"),
        plan_name=data.get("plan_name") or "Credits",
        has_active_subscription=True,
        wallet=WalletBalance(**wallet) if wallet else None,
        costs=data.get("costs") or {},
        packs=[CreditPackItem(**p) for p in data.get("packs") or []],
        credits=credits,
    )


@credit_routes.get("/usage", response_model=CreditConsumptionResponse)
def get_my_credit_usage(
    days: int = Query(30, ge=1, le=365),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """What has been spending this user's credits, grouped by activity."""
    user = UserService(db).get_current_user(authjwt.get_jwt_subject())
    data = CreditService(db).consumption(user.id, days)
    return CreditConsumptionResponse(**data)


@credit_routes.get("/packs")
def list_credit_packs():
    """Consumable credit packs sold via IAP and Paystack."""
    return {"packs": packs_public()}


@credit_routes.post("/checkout")
async def checkout_credit_pack(
    request: CreditCheckoutRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Start a Paystack checkout for a credit pack (web / Android)."""
    pack = get_pack(request.pack_id)
    if not pack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credit pack not found")

    user = UserService(db).get_current_user(authjwt.get_jwt_subject())
    email = (request.email or user.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required to buy credits")

    try:
        amount_ghs, usd_ghs_rate = await usd_to_ghs(float(pack["price_usd"]))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not convert the dollar price to cedis. Try again shortly.",
        ) from exc

    amount_subunit = int(round(amount_ghs * 100))
    paystack = PaystackService(db)
    init = await paystack.initialize_transaction(
        user_id=user.id,
        request=PaystackInitializeRequest(
            email=email,
            amount=amount_subunit,
            callback_url=request.callback_url,
            currency="GHS",
            metadata={
                "purpose": "credits",
                "pack_id": pack["id"],
                "credits": pack["credits"],
                "price_usd": pack["price_usd"],
                "usd_ghs_rate": usd_ghs_rate,
                "amount_ghs": amount_ghs,
            },
        ),
    )
    return {
        "pack": pack,
        "authorization_url": init.authorization_url,
        "access_code": init.access_code,
        "reference": init.reference,
        "amount": amount_ghs,
        "price_usd": pack["price_usd"],
        "currency": "GHS",
        "usd_ghs_rate": usd_ghs_rate,
    }
