from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional
from datetime import datetime


class CustomerCreateRequest(BaseModel):
    """Request model for creating/updating a customer."""
    name: str
    customer_number: str
    network: Optional[str] = None  # Auto-detected if not provided
    bank_code: Optional[str] = None
    email: Optional[EmailStr] = None

    class Config:
        json_schema_extra = {
            "example": {
                "name": "John Agyeman",
                "customer_number": "0550748724",
                "network": "MTN",
                "bank_code": None,
                "email": "john@example.com"
            }
        }


class CustomerResponse(BaseModel):
    """Response model for customer."""
    id: int
    name: str
    email: Optional[str] = None
    customer_number: str
    network: str
    bank_code: Optional[str]
    account_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_customer(cls, customer):
        """Convert Customer model to response DTO."""
        account_type_value = customer.account_type
        if hasattr(account_type_value, "value"):
            account_type_value = account_type_value.value
        elif isinstance(account_type_value, str):
            account_type_value = account_type_value.lower()
        else:
            account_type_value = str(account_type_value or "mobile_money")

        created_at = customer.created_at or datetime.now()
        updated_at = customer.updated_at or created_at
        return cls(
            id=customer.id,
            name=customer.name or "Customer",
            email=customer.email,
            customer_number=customer.customer_number,
            network=str(customer.network or ""),
            bank_code=customer.bank_code,
            account_type=str(account_type_value),
            is_active=bool(customer.is_active),
            created_at=created_at,
            updated_at=updated_at,
        )


class CustomerMessageSmsRequest(BaseModel):
    """Send a custom SMS to one or more saved customers."""
    customer_ids: List[int] = Field(..., min_length=1, description="Customer IDs to message")
    message: str = Field(..., min_length=1, max_length=160, description="SMS body (max 160 chars)")

    class Config:
        json_schema_extra = {
            "example": {
                "customer_ids": [1, 2],
                "message": "Hi! Your order is ready for pickup."
            }
        }


class CustomerMessageEmailRequest(BaseModel):
    """Send a custom email to one or more saved customers."""
    customer_ids: List[int] = Field(..., min_length=1, description="Customer IDs to email")
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=100_000)

    class Config:
        json_schema_extra = {
            "example": {
                "customer_ids": [1],
                "subject": "Order update",
                "body": "Hello, your order has shipped."
            }
        }


class CustomerMessageRecipientResult(BaseModel):
    customer_id: int
    customer_name: str
    success: bool
    message: str
    destination: Optional[str] = None


class CustomerMessageResponse(BaseModel):
    total: int
    sent: int
    failed: int
    results: List[CustomerMessageRecipientResult]
