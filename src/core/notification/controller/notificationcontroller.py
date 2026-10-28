from fastapi import APIRouter, Depends, HTTPException, status, Query
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from core.auth.service.sessiondriver import SessionDriver, TokenData
from another_fastapi_jwt_auth import AuthJWT
from core.exceptions import *
from core.notification.dto.request.notificationcreate import NotificationCreateRequest
from core.notification.dto.request.notificationupdate import NotificationUpdateRequest
from utilities.dbconfig import SessionLocal
from sqlalchemy.orm import Session
import logging
from core.notification.model.Notification import Notification, NotificationStatus, NotificationType
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.user.model.User import User

# DTO Models
from core.notification.dto.response.notification_response import NotificationResponse
from core.notification.dto.response.paged_notifications import PagedNotificationResponse
from core.notification.dto.response.message_response import MessageResponse

from core.notification.service.notification_service import NotificationService
from another_fastapi_jwt_auth.exceptions import MissingTokenError

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Reuse your existing token validation and DB dependencies
from core.user.controller.usercontroller import validate_token, get_db

# Controller (Router)
notification_routes = APIRouter()

@notification_routes.post("/", response_model=NotificationResponse)
def create_notification(
    notification_data: NotificationCreateRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    notification_service = NotificationService(db)
    
    # If user_id not provided, use current user
    user_id = notification_data.user_id
    if not user_id:
        user = db.query(User).filter(User.email == current_user_email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user_id = user.id
    
    return notification_service.create_notification(
        user_id=user_id,
        notification_type=notification_data.type,
        data=notification_data.data
    )

@notification_routes.get("/", response_model=PagedNotificationResponse)
def get_user_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    status: Optional[NotificationStatus] = Query(None),
    type: Optional[NotificationType] = Query(None),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    return notification_service.get_user_notifications_paged(
        user_id=user.id,
        page=page,
        size=size,
        status=status,
        notification_type=type
    )

@notification_routes.get("/{notification_id}", response_model=NotificationResponse)
def get_notification(
    notification_id: str,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification = notification_service.get_notification(notification_id)
    
    # Verify the notification belongs to the user
    if notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this notification")
    
    return notification

@notification_routes.put("/{notification_id}", response_model=NotificationResponse)
def update_notification(
    notification_id: str,
    update_data: NotificationUpdateRequest,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification = notification_service.get_notification(notification_id)
    
    # Verify the notification belongs to the user
    if notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this notification")
    
    return notification_service.update_notification(
        notification_id=notification_id,
        status=update_data.status,
        data=update_data.data
    )

@notification_routes.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_as_read(
    notification_id: str,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification = notification_service.get_notification(notification_id)
    
    # Verify the notification belongs to the user
    if notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this notification")
    
    return notification_service.mark_notification_as_read(notification_id)

@notification_routes.delete("/{notification_id}", response_model=MessageResponse)
def delete_notification(
    notification_id: str,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification = notification_service.get_notification(notification_id)
    
    # Verify the notification belongs to the user
    if notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this notification")
    
    notification_service.delete_notification(notification_id)
    return {"message": "Notification deleted successfully"}

@notification_routes.post("/mark-all-read", response_model=MessageResponse)
def mark_all_notifications_as_read(
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification_service.mark_all_notifications_as_read(user.id)
    return {"message": "All notifications marked as read"}

# Add these endpoints to your existing notification_routes

@notification_routes.post("/send-sms", response_model=NotificationResponse)
def send_sms_notification(
    phone: str = Query(..., description="Phone number with country code"),
    message: str = Query(..., max_length=160, description="SMS message"),
    notification_type: NotificationType = Query(NotificationType.INFO),
    user_id: Optional[str] = Query(None, description="User ID (optional)"),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """Send an SMS notification to a phone number"""
    current_user_email = authjwt.get_jwt_subject()
    CreditService(db).require_credits(current_user_email, CreditType.SMS.value, 1.0, "notification_sms")
    
    # If user_id not provided, use current user
    if not user_id:
        user = db.query(User).filter(User.email == current_user_email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user_id = user.id
    
    notification_service = NotificationService(db)
    
    # Create notification data
    notification_data = {
        "message": message,
        "source": "sms_direct"
    }
    
    return notification_service.create_notification(
        user_id=user_id,
        notification_type=notification_type,
        data=notification_data,
        send_sms=True,
        sms_phone=phone
    )


@notification_routes.post("/bulk-sms", response_model=Dict[str, Any])
def send_bulk_sms_notifications(
    user_ids: List[str] = Query(..., description="List of user IDs"),
    message: str = Query(..., max_length=160, description="SMS message"),
    notification_type: NotificationType = Query(NotificationType.INFO),
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """Send bulk SMS notifications to multiple users"""
    # Verify admin or appropriate permissions here
    current_user_email = authjwt.get_jwt_subject()
    CreditService(db).require_credits(
        current_user_email,
        CreditType.SMS.value,
        float(max(1, len(user_ids))),
        "notification_bulk_sms",
    )
    
    notification_service = NotificationService(db)
    
    result = notification_service.send_bulk_sms_notifications(
        user_ids=user_ids,
        message=message,
        notification_type=notification_type
    )
    
    return result


@notification_routes.get("/{notification_id}/sms-status", response_model=Dict[str, Any])
def check_notification_sms_status(
    notification_id: str,
    authjwt: AuthJWT = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """Check SMS delivery status for a notification"""
    current_user_email = authjwt.get_jwt_subject()
    user = db.query(User).filter(User.email == current_user_email).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    notification_service = NotificationService(db)
    notification = notification_service.get_notification(notification_id)
    
    # Verify the notification belongs to the user
    if notification.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this notification")
    
    return notification_service.check_sms_delivery_status(notification_id)