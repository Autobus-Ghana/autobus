from fastapi import APIRouter, Depends, HTTPException, status, Path
from sqlalchemy.orm import Session
from typing import List
import logging

from core.customers.service.customer_service import CustomerService
from core.customers.dto.customer_dto import (
    CustomerCreateRequest,
    CustomerResponse,
    CustomerMessageSmsRequest,
    CustomerMessageEmailRequest,
    CustomerMessageResponse,
)
from core.customers.service.customer_messaging_service import CustomerMessagingService
from core.email.sender_email import SenderEmailNotConfigured
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.user.controller.usercontroller import validate_token, get_db
from another_fastapi_jwt_auth import AuthJWT

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

customer_routes = APIRouter()


@customer_routes.post("/add", response_model=CustomerResponse)
def add_customer(
    request: CustomerCreateRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token)
):
    """Add a new customer for the authenticated user."""
    try:
        user_id = authjwt.get_jwt_subject()
        logger.info(f"[BENEFICIARY_CONTROLLER] Adding customer for user: {user_id}")

        customer_service = CustomerService(db)
        success, customer, message = customer_service.add_customer(
            user_id=user_id,
            name=request.name,
            customer_number=request.customer_number,
            network=request.network,
            bank_code=request.bank_code,
            email=str(request.email) if request.email else None,
        )

        if not success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

        logger.info(f"[BENEFICIARY_CONTROLLER] Customer added successfully: {customer.id}")
        return CustomerResponse.from_customer(customer)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error adding customer: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error adding customer: {str(e)}"
        )


@customer_routes.get("/list", response_model=List[CustomerResponse])
def list_customers(
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token)
):
    """Get all customers for the authenticated user."""
    try:
        user_id = authjwt.get_jwt_subject()
        logger.info(f"[BENEFICIARY_CONTROLLER] Listing customers for user: {user_id}")

        customer_service = CustomerService(db)
        customers = customer_service.get_customers(user_id)

        logger.info(f"[BENEFICIARY_CONTROLLER] Found {len(customers)} customers")
        return [CustomerResponse.from_customer(b) for b in customers]

    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error listing customers: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving customers: {str(e)}"
        )


@customer_routes.get("/get/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: int = Path(..., description="Customer ID"),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token)
):
    """Get a specific customer by ID."""
    try:
        user_id = authjwt.get_jwt_subject()
        logger.info(f"[BENEFICIARY_CONTROLLER] Getting customer: {customer_id}")

        customer_service = CustomerService(db)
        customer = customer_service.get_customer(customer_id, user_id)

        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found"
            )

        return CustomerResponse.from_customer(customer)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error getting customer: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving customer: {str(e)}"
        )


@customer_routes.delete("/delete/{customer_id}")
def delete_customer(
    customer_id: int = Path(..., description="Customer ID"),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token)
):
    """Delete a customer."""
    try:
        user_id = authjwt.get_jwt_subject()
        logger.info(f"[BENEFICIARY_CONTROLLER] Deleting customer: {customer_id}")

        customer_service = CustomerService(db)
        success, message = customer_service.delete_customer(customer_id, user_id)

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

        logger.info(f"[BENEFICIARY_CONTROLLER] Customer deleted: {customer_id}")
        return {"message": message}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error deleting customer: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting customer: {str(e)}"
        )


@customer_routes.put("/update/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: int = Path(..., description="Customer ID"),
    request: CustomerCreateRequest = None,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token)
):
    """Update customer details."""
    try:
        user_id = authjwt.get_jwt_subject()
        logger.info(f"[BENEFICIARY_CONTROLLER] Updating customer: {customer_id}")

        customer_service = CustomerService(db)
        success, customer, message = customer_service.update_customer(
            customer_id=customer_id,
            user_id=user_id,
            name=request.name if request else None,
            customer_number=request.customer_number if request else None,
            bank_code=request.bank_code if request else None,
            email=str(request.email) if request and request.email else None,
        )

        if not success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

        logger.info(f"[BENEFICIARY_CONTROLLER] Customer updated: {customer_id}")
        return CustomerResponse.from_customer(customer)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error updating customer: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating customer: {str(e)}"
        )


@customer_routes.post("/message/sms", response_model=CustomerMessageResponse)
def send_customer_sms(
    request: CustomerMessageSmsRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Send a custom SMS to one or more customers by customer ID."""
    try:
        user_id = authjwt.get_jwt_subject()
        recipient_count = max(1, len(request.customer_ids))
        CreditService(db).require_credits(
            user_id,
            CreditType.SMS.value,
            float(recipient_count),
            "customer_sms",
        )
        messaging_service = CustomerMessagingService(db)
        return messaging_service.send_sms(
            user_id=user_id,
            customer_ids=request.customer_ids,
            message=request.message.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error sending customer SMS: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error sending SMS: {str(e)}",
        )


@customer_routes.post("/message/email", response_model=CustomerMessageResponse)
def send_customer_email(
    request: CustomerMessageEmailRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Send a custom email to one or more customers by customer ID."""
    try:
        user_id = authjwt.get_jwt_subject()
        recipient_count = max(1, len(request.customer_ids))
        CreditService(db).require_credits(
            user_id,
            CreditType.EMAIL.value,
            float(recipient_count),
            "customer_email",
        )
        messaging_service = CustomerMessagingService(db)
        return messaging_service.send_email(
            user_id=user_id,
            customer_ids=request.customer_ids,
            subject=request.subject.strip(),
            body=request.body,
        )
    except HTTPException:
        raise
    except SenderEmailNotConfigured as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[BENEFICIARY_CONTROLLER] Error sending customer email: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error sending email: {str(e)}",
        )
