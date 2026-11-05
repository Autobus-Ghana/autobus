from fastapi import APIRouter, Depends, HTTPException, status, Query
from datetime import datetime
from typing import List, Optional
import jwt
from pydantic import BaseModel
from core.auth.service.sessiondriver import SessionDriver, TokenData
from another_fastapi_jwt_auth import AuthJWT
from core.exceptions import *
from core.user.dto.response.paged_users import PagedUserResponse
from utilities.dbconfig import SessionLocal
from sqlalchemy.orm import Session
from core.user.model.User import User
from core.auth.dependencies import (
    validate_token,
    get_current_user as get_current_user_orm,
    get_manager_id_from_jwt,
    require_admin,
    require_self_or_admin,
    resolve_session_manager,
    resolve_user_from_jwt,
)

# Re-export for callers that historically imported validate_token / get_db from here
__all__ = ["user_routes", "validate_token", "get_db"]
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# DTO Models
from core.user.dto.response.message_response import MessageResponse
from core.user.dto.response.user_response import UserResponse
from core.user.dto.response.sent_emails_response import SentEmailsResponse, SentEmailItem
from core.user.dto.response.sent_sms_response import SentSmsResponse, SentSmsItem
from core.customers.service.customer_messaging_service import CustomerMessagingService

from core.user.service.user_service import UserService
from another_fastapi_jwt_auth.exceptions import MissingTokenError
from core.user.dto.request.user_update_request import UserUpdateRequest
from core.user.dto.request.notification_settings_update_request import (
    NotificationSettingsUpdateRequest,
)
from core.user.dto.request.profile_image_update_request import ProfileImageUpdateRequest
from core.user.dto.request.sender_email_update_request import SenderEmailUpdateRequest
from core.user.dto.request.two_factor_contact_request import TwoFactorContactRequest
from core.notification.service.notification_service import NotificationService
from core.notification.dto.response.paged_notifications import PagedNotificationResponse
from core.notification.model.Notification import NotificationStatus, NotificationType
from core.histories.service.historyservice import HistoryService
from core.histories.dto.response.historyresponse import HistoryResponseDTO
from core.receipts.service.receipt_service import ReceiptService
from core.receipts.dto.response.receiptresponse import ReceiptResponse
from core.agent.tools.email.email import EmailTool

# Controller (Router)
user_routes = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@user_routes.get("/me", response_model=UserResponse)
def get_current_user_endpoint(authjwt: AuthJWT = Depends(validate_token), db: Session = Depends(get_db)):
    user = resolve_user_from_jwt(authjwt, db)
    user_service = UserService(db)
    return user_service.get_current_user(user.id)


@user_routes.get("/me/emails/sent", response_model=SentEmailsResponse)
def list_my_sent_emails(
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Recent emails sent through Autobus EmailTool (ZeptoMail), keyed by your account phone.

    History is populated when a send succeeds from the NLU/agent flow; max 100 retained in Redis.
    """
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    user = user_service.get_current_user(current_user_email)
    phone = (user.phone or "").strip()
    if not phone:
        return SentEmailsResponse(emails=[], total_returned=0)

    tool = EmailTool(db_pool=db)
    rows = tool.list_sent_emails_for_user(phone, limit=limit)
    items: List[SentEmailItem] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        items.append(
            SentEmailItem(
                to=str(r.get("to") or ""),
                subject=str(r.get("subject") or ""),
                sent_at=str(r.get("sent_at") or ""),
            )
        )
    return SentEmailsResponse(emails=items, total_returned=len(items))


@user_routes.get("/me/sms/sent", response_model=SentSmsResponse)
def list_my_sent_sms(
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Recent SMS sent through Autobus customer messaging, keyed by your account.

    History is populated when a send succeeds from the messaging feature; max 100 retained in Redis.
    """
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    user = user_service.get_current_user(current_user_email)
    messaging = CustomerMessagingService(db)
    rows = messaging.list_sent_sms_for_user(user.id, limit=limit)
    items: List[SentSmsItem] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        items.append(
            SentSmsItem(
                phone=str(r.get("phone") or r.get("to") or ""),
                message=str(r.get("message") or r.get("body") or ""),
                sent_at=str(r.get("sent_at") or ""),
                status=str(r.get("status") or "Sent"),
            )
        )
    return SentSmsResponse(messages=items, total_returned=len(items))


@user_routes.get("/me/notifications", response_model=PagedNotificationResponse)
def get_my_notifications(
    authjwt: AuthJWT = Depends(validate_token),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1),
    status: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    user = user_service.get_current_user(current_user_email)
    notif_service = NotificationService(db)

    status_enum = None
    type_enum = None
    try:
        if status:
            status_enum = NotificationStatus(status)
        if type:
            type_enum = NotificationType(type)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid status or type value")

    return notif_service.get_user_notifications_paged(user.id, page, size, status_enum, type_enum)


@user_routes.get("/{user_id}/notifications", response_model=PagedNotificationResponse)
def get_user_notifications(
    user_id: str,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1),
    status: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    require_self_or_admin(user_id, current_user)
    notif_service = NotificationService(db)

    status_enum = None
    type_enum = None
    try:
        if status:
            status_enum = NotificationStatus(status)
        if type:
            type_enum = NotificationType(type)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid status or type value")

    return notif_service.get_user_notifications_paged(user_id, page, size, status_enum, type_enum)


@user_routes.get("/me/financials", response_model=List[HistoryResponseDTO])
def get_my_financials(
    authjwt: AuthJWT = Depends(validate_token),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    intent: Optional[str] = None,
    transaction_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    user = user_service.get_current_user(current_user_email)
    history_service = HistoryService(db)
    return history_service.get_user_histories(user.id, page, page_size, intent, transaction_type, start_date, end_date)


@user_routes.get("/{user_id}/financials", response_model=List[HistoryResponseDTO])
def get_user_financials(
    user_id: str,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    intent: Optional[str] = None,
    transaction_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db)
):
    require_self_or_admin(user_id, current_user)
    history_service = HistoryService(db)
    return history_service.get_user_histories(user_id, page, page_size, intent, transaction_type, start_date, end_date)


@user_routes.get("/me/receipts", response_model=List[ReceiptResponse])
def get_my_receipts(
    authjwt: AuthJWT = Depends(validate_token),
    limit: int = Query(10, ge=1),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    user = user_service.get_current_user(current_user_email)
    receipt_service = ReceiptService(db)
    return receipt_service.get_user_receipts(user.id, limit)


@user_routes.get("/{user_id}/receipts", response_model=List[ReceiptResponse])
def get_user_receipts(
    user_id: str,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    limit: int = Query(10, ge=1),
    db: Session = Depends(get_db)
):
    require_self_or_admin(user_id, current_user)
    receipt_service = ReceiptService(db)
    return receipt_service.get_user_receipts(user_id, limit)


def _login_account(authjwt: AuthJWT, db: Session) -> User:
    """The signed-in account, not a business switched into for this session."""
    current = resolve_user_from_jwt(authjwt, db)
    return resolve_session_manager(authjwt, db, current)


@user_routes.get("/me/2fa")
def get_my_two_factor(authjwt: AuthJWT = Depends(validate_token), db: Session = Depends(get_db)):
    """Phone and email already used for sign-in codes. 2FA is on for every account."""
    account = _login_account(authjwt, db)
    return UserService(db).two_factor_status(account)


@user_routes.post("/me/2fa")
def update_my_two_factor(
    payload: TwoFactorContactRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Change the phone or email that receives sign-in codes, after an OTP to the new contact."""
    current = resolve_user_from_jwt(authjwt, db)
    account = resolve_session_manager(authjwt, db, current)
    previous_email = (account.email or "").strip().lower()
    status_body, email_changed = UserService(db).change_two_factor_contact(
        account,
        channel=payload.channel,
        value=payload.value,
        otp=payload.otp,
    )
    subject = (authjwt.get_jwt_subject() or "").strip().lower()
    if email_changed and subject == previous_email:
        from core.auth.service.authservice import AuthService

        db.refresh(current)
        manager_id = get_manager_id_from_jwt(authjwt) or account.id
        tokens = AuthService(db).issue_session_tokens(
            current,
            manager_id,
            status="Email updated",
        )
        status_body["access_token"] = tokens["access_token"]
        status_body["refresh_token"] = tokens["refresh_token"]
        status_body["token_type"] = tokens["token_type"]
        status_body["expires_in"] = tokens["expires_in"]
    return status_body


@user_routes.put("/me", response_model=UserResponse)
def update_current_user_endpoint(payload: UserUpdateRequest, authjwt: AuthJWT = Depends(validate_token), db: Session = Depends(get_db)):
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    return user_service.update_current_user(current_user_email, payload)


@user_routes.put("/me/sender-email", response_model=UserResponse)
def update_my_sender_email(
    payload: SenderEmailUpdateRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Set the From address used when Autobus sends email on the user's behalf."""
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    return user_service.update_current_user_sender_email(
        current_user_email, sender_email=payload.sender_email
    )


@user_routes.patch("/me", response_model=UserResponse)
def patch_current_user_endpoint(payload: UserUpdateRequest, authjwt: AuthJWT = Depends(validate_token), db: Session = Depends(get_db)):
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    return user_service.update_current_user(current_user_email, payload)


@user_routes.patch("/me/notification-settings", response_model=UserResponse)
def update_my_notification_settings(
    payload: NotificationSettingsUpdateRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Update only notification preference flags for the current user.
    Allowed fields: in_app_notification, sms_notification
    """
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    data = payload.model_dump(exclude_unset=True)
    return user_service.update_current_user_notification_settings(
        current_user_email,
        in_app_notification=data.get("in_app_notification"),
        sms_notification=data.get("sms_notification"),
    )


@user_routes.patch("/me/profile-image", response_model=UserResponse)
def update_my_profile_image(
    payload: ProfileImageUpdateRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Update only profile picture URL for the current user.
    Allowed field: profile_picture_url
    """
    current_user_email = authjwt.get_jwt_subject()
    user_service = UserService(db)
    return user_service.update_current_user_profile_image(
        current_user_email, profile_picture_url=str(payload.profile_picture_url)
    )


@user_routes.patch("/{user_id}/notification-settings", response_model=UserResponse)
def update_user_notification_settings(
    user_id: str,
    payload: NotificationSettingsUpdateRequest,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Update only notification preference flags for a specific user.
    Allowed fields: in_app_notification, sms_notification
    """
    require_self_or_admin(user_id, current_user)
    user_service = UserService(db)
    data = payload.model_dump(exclude_unset=True)
    return user_service.update_user_notification_settings(
        user_id,
        in_app_notification=data.get("in_app_notification"),
        sms_notification=data.get("sms_notification"),
    )


@user_routes.patch("/{user_id}/profile-image", response_model=UserResponse)
def update_user_profile_image(
    user_id: str,
    payload: ProfileImageUpdateRequest,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Update only profile picture URL for a specific user.
    Allowed field: profile_picture_url
    """
    require_self_or_admin(user_id, current_user)
    user_service = UserService(db)
    return user_service.update_user_profile_image(
        user_id, profile_picture_url=str(payload.profile_picture_url)
    )
    
@user_routes.get("/all", response_model=PagedUserResponse)
def get_all_users(
    admin: User = Depends(require_admin),
    authjwt: AuthJWT = Depends(validate_token),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1),
    db: Session = Depends(get_db)
):
    user_service = UserService(db)
    return user_service.get_all_users_paged(page, size)

@user_routes.get("/{user_id}", response_model=UserResponse)
def get_user_by_id(
    user_id: str,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    require_self_or_admin(user_id, current_user)
    user_service = UserService(db)
    return user_service.get_user_by_id(user_id)


@user_routes.put("/{user_id}", response_model=UserResponse)
def update_user_endpoint(
    user_id: str,
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    require_self_or_admin(user_id, current_user)
    user_service = UserService(db)
    return user_service.update_user(user_id, payload)


@user_routes.patch("/{user_id}", response_model=UserResponse)
def patch_user_endpoint(
    user_id: str,
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user_orm),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    require_self_or_admin(user_id, current_user)
    user_service = UserService(db)
    return user_service.update_user(user_id, payload)

@user_routes.put("/{user_id}/status", response_model=MessageResponse)
def update_user_status(
    user_id: str,
    enabled: bool = Query(...),
    admin: User = Depends(require_admin),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_service = UserService(db)
    user_service.set_user_enabled_status(user_id, enabled)
    return {"message": "User status updated successfully"}

@user_routes.delete("/{user_id}", response_model=MessageResponse)
def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_service = UserService(db)
    return user_service.delete_user(user_id)

@user_routes.put("/{user_id}/role/{role_id}", response_model=MessageResponse)
def update_user_role(
    user_id: str,
    role_id: str,
    admin: User = Depends(require_admin),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db),
):
    # Role model not implemented; block privilege escalation surface.
    raise HTTPException(
        status_code=501,
        detail="Role management is not enabled. Configure ADMIN_USER_IDS / ADMIN_EMAILS instead.",
    )