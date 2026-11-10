from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DeletionBusiness(BaseModel):
    id: str
    email: str
    fullname: str
    company: Optional[str] = None
    is_manager: bool = False
    is_active: bool = False


class DeletionConnection(BaseModel):
    kind: str
    label: str
    detail: Optional[str] = None


class DeletionSubscription(BaseModel):
    active: bool = False
    provider: Optional[str] = None
    plan_name: Optional[str] = None


class DeletionBlocker(BaseModel):
    """Something the person must clear before Autobus will delete the login."""

    area: str
    label: str
    detail: Optional[str] = None


class AccountDeletionPreview(BaseModel):
    login_email: str
    businesses: List[DeletionBusiness] = Field(default_factory=list)
    connections: List[DeletionConnection] = Field(default_factory=list)
    blockers: List[DeletionBlocker] = Field(default_factory=list)
    can_delete: bool = True
    subscription: DeletionSubscription = Field(default_factory=DeletionSubscription)
    data_summary: Dict[str, int] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class AccountDeletionResult(BaseModel):
    status: str = "ok"
    message: str = "Your Autobus account has been deleted."
    deleted_user_ids: List[str] = Field(default_factory=list)
