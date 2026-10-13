"""Platform admin API for the Autobus admin portal."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.admin.dto.admin_dto import (
    AdminAuthResponse,
    AdminInviteRequest,
    AdminMeResponse,
    AdminRoleUpdateRequest,
    AdminSignInRequest,
    AdminUserResponse,
    AdResponse,
    AdUpsertRequest,
    OwnerFeedResponse,
    ResourceItemResponse,
    ResourceUpsertRequest,
    CustomerResponse,
    CustomerUpdateRequest,
    DashboardResponse,
    MerchantResponse,
    PlanAdminResponse,
    PlanUpsertRequest,
    SenderIdAdminResponse,
    SenderIdRejectRequest,
    TransactionAdminResponse,
)
from core.admin.service.admin_service import AdminService
from core.auth.dependencies import get_current_user, get_db, require_admin, require_super_admin
from core.admin.service.owner_resource_service import OwnerResourceService
from core.admin.dto.automation_request_dto import (
    AutomationRequestResponse,
    CreateAutomationRequestPayload,
    PortalSurveySubmitRequest,
    UpdateAutomationRequestStatusPayload,
)
from core.admin.service.automation_request_service import AutomationRequestService
from core.user.model.User import User

admin_routes = APIRouter()
public_ads_routes = APIRouter()
owner_resource_routes = APIRouter()
public_automation_routes = APIRouter()


@admin_routes.post("/auth/signin", response_model=AdminAuthResponse)
def admin_signin(payload: AdminSignInRequest, db: Session = Depends(get_db)):
    return AdminService(db).signin(payload.email, payload.password)


@admin_routes.get("/me", response_model=AdminMeResponse)
def admin_me(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return AdminService(db).me(admin)


@admin_routes.get("/dashboard", response_model=DashboardResponse)
def admin_dashboard(
    period: str = Query("Last 30 days"),
    custom_from: Optional[str] = Query(None),
    custom_to: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).dashboard(period, custom_from, custom_to)


@admin_routes.get("/merchants", response_model=List[MerchantResponse])
def list_merchants(
    q: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).list_merchants(q=q, limit=limit)


@admin_routes.get("/merchants/{merchant_id}", response_model=MerchantResponse)
def get_merchant(
    merchant_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).get_merchant(merchant_id)


@admin_routes.patch("/merchants/{merchant_id}/enabled", response_model=MerchantResponse)
def toggle_merchant(
    merchant_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).toggle_merchant(merchant_id)


@admin_routes.delete("/merchants/{merchant_id}")
def delete_merchant(
    merchant_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AdminService(db).delete_merchant(merchant_id)
    return {"message": "Merchant deleted"}


@admin_routes.get("/customers", response_model=List[CustomerResponse])
def list_customers(
    merchant_id: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).list_customers(merchant_id)


@admin_routes.get("/customers/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).get_customer(customer_id)


@admin_routes.patch("/customers/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: str,
    payload: CustomerUpdateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).update_customer(customer_id, payload)


@admin_routes.patch("/customers/{customer_id}/status", response_model=CustomerResponse)
def toggle_customer(
    customer_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).toggle_customer(customer_id)


@admin_routes.delete("/customers/{customer_id}")
def delete_customer(
    customer_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AdminService(db).delete_customer(customer_id)
    return {"message": "Customer deleted"}


@admin_routes.get("/plans", response_model=List[PlanAdminResponse])
def list_plans(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    _ = admin
    return AdminService(db).list_plans()


@admin_routes.post("/plans", response_model=PlanAdminResponse)
def create_plan(
    payload: PlanUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).create_plan(payload)


@admin_routes.put("/plans/{plan_id}", response_model=PlanAdminResponse)
def update_plan(
    plan_id: str,
    payload: PlanUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).update_plan(plan_id, payload)


@admin_routes.patch("/plans/{plan_id}/active", response_model=PlanAdminResponse)
def toggle_plan(
    plan_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).toggle_plan(plan_id)


@admin_routes.get("/sender-ids", response_model=List[SenderIdAdminResponse])
def list_sender_ids(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    _ = admin
    return AdminService(db).list_sender_ids()


@admin_routes.post("/sender-ids/{registration_id}/approve", response_model=SenderIdAdminResponse)
def approve_sender_id(
    registration_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return AdminService(db).approve_sender_id(registration_id, admin)


@admin_routes.post("/sender-ids/{registration_id}/reject", response_model=SenderIdAdminResponse)
def reject_sender_id(
    registration_id: str,
    payload: SenderIdRejectRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return AdminService(db).reject_sender_id(registration_id, payload.reason, admin)


@admin_routes.get("/transactions", response_model=List[TransactionAdminResponse])
def list_transactions(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    _ = admin
    return AdminService(db).list_transactions()


@admin_routes.post("/transactions/{txn_id}/refund", response_model=TransactionAdminResponse)
def refund_transaction(
    txn_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).set_transaction_status(txn_id, "refunded")


@admin_routes.post("/transactions/{txn_id}/resolve", response_model=TransactionAdminResponse)
def resolve_transaction(
    txn_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).set_transaction_status(txn_id, "success")


@admin_routes.post("/transactions/{txn_id}/retry", response_model=TransactionAdminResponse)
def retry_transaction(
    txn_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).set_transaction_status(txn_id, "success")


@admin_routes.get("/ads", response_model=List[AdResponse])
def list_ads(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    _ = admin
    return AdminService(db).list_ads()


@admin_routes.post("/ads", response_model=AdResponse)
def create_ad(
    payload: AdUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).create_ad(payload)


@admin_routes.put("/ads/{ad_id}", response_model=AdResponse)
def update_ad(
    ad_id: str,
    payload: AdUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).update_ad(ad_id, payload)


@admin_routes.patch("/ads/{ad_id}/active", response_model=AdResponse)
def toggle_ad(
    ad_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AdminService(db).toggle_ad(ad_id)


@admin_routes.delete("/ads/{ad_id}")
def delete_ad(
    ad_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AdminService(db).delete_ad(ad_id)
    return {"message": "Ad deleted"}


@public_ads_routes.get("/active", response_model=List[AdResponse])
def list_active_ads(db: Session = Depends(get_db)):
    return AdminService(db).list_active_ads()


@admin_routes.get("/admins", response_model=List[AdminUserResponse])
def list_admins(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return AdminService(db).list_admins(admin)


@admin_routes.post("/admins", response_model=AdminUserResponse)
def invite_admin(
    payload: AdminInviteRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return AdminService(db).invite_admin(payload, admin)


@admin_routes.post("/admins/{admin_id}/resend")
def resend_admin_invite(
    admin_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    AdminService(db).resend_invite(admin_id)
    return {"message": "Invite resent"}


@admin_routes.patch("/admins/{admin_id}/role", response_model=AdminUserResponse)
def update_admin_role(
    admin_id: str,
    payload: AdminRoleUpdateRequest,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return AdminService(db).update_admin_role(admin_id, payload.role, admin)


@admin_routes.delete("/admins/{admin_id}")
def remove_admin(
    admin_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    AdminService(db).remove_admin(admin_id, admin)
    return {"message": "Admin removed"}


@admin_routes.get("/resources", response_model=List[ResourceItemResponse])
def list_resources(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    _ = admin
    return OwnerResourceService(db).list_all()


@admin_routes.post("/resources", response_model=ResourceItemResponse)
def create_resource(
    payload: ResourceUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return OwnerResourceService(db).create(payload)


@admin_routes.put("/resources/{resource_id}", response_model=ResourceItemResponse)
def update_resource(
    resource_id: str,
    payload: ResourceUpsertRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return OwnerResourceService(db).update(resource_id, payload)


@admin_routes.patch("/resources/{resource_id}/active", response_model=ResourceItemResponse)
def toggle_resource(
    resource_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return OwnerResourceService(db).toggle(resource_id)


@admin_routes.delete("/resources/{resource_id}")
def delete_resource(
    resource_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    OwnerResourceService(db).delete(resource_id)
    return {"message": "Resource deleted"}


@owner_resource_routes.get("/feed", response_model=OwnerFeedResponse)
def owner_resource_feed(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return OwnerResourceService(db).feed_for(user)


# ---------------------------------------------------------------------------
# Automation Requests & Portal Survey Endpoints
# ---------------------------------------------------------------------------

@admin_routes.get("/automation-requests", response_model=List[AutomationRequestResponse])
def list_automation_requests(
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AutomationRequestService(db).list_all(q=q, status_filter=status, category=category)


@admin_routes.post("/automation-requests", response_model=AutomationRequestResponse)
def create_automation_request(
    payload: CreateAutomationRequestPayload,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return AutomationRequestService(db).create(payload, user=admin)


@admin_routes.get("/automation-requests/{request_id}", response_model=AutomationRequestResponse)
def get_automation_request(
    request_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AutomationRequestService(db).get_by_id(request_id)


@admin_routes.patch("/automation-requests/{request_id}/status", response_model=AutomationRequestResponse)
def update_automation_request_status(
    request_id: str,
    payload: UpdateAutomationRequestStatusPayload,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AutomationRequestService(db).update_status(request_id, payload.status)


@admin_routes.delete("/automation-requests/{request_id}")
def delete_automation_request(
    request_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    return AutomationRequestService(db).delete(request_id)


# Public / Portal Survey submission endpoints
@public_automation_routes.post("/automation-requests", response_model=AutomationRequestResponse)
def submit_portal_automation_request(
    payload: CreateAutomationRequestPayload,
    db: Session = Depends(get_db),
):
    """Allows merchants or portal visitors to submit automation requests or survey responses."""
    return AutomationRequestService(db).create(payload)


@public_automation_routes.post("/automation-requests/survey", response_model=AutomationRequestResponse)
def submit_portal_survey(
    payload: PortalSurveySubmitRequest,
    db: Session = Depends(get_db),
):
    """Direct portal survey submission endpoint that stores responses in the database."""
    return AutomationRequestService(db).submit_portal_survey(payload)
