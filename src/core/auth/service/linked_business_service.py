"""Create, list, switch, and detach linked business accounts."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from another_fastapi_jwt_auth import AuthJWT

from core.auth.dependencies import resolve_session_manager
from core.auth.dto.request.linked_business import (
    CreateBusinessRequest,
    DetachBusinessRequest,
    SwitchBusinessRequest,
)
from core.auth.dto.response.linked_business import (
    LinkedBusinessItem,
    LinkedBusinessListResponse,
)
from core.auth.service.authservice import AuthService
from core.exceptions.UserException import UserAlreadyExistsError
from core.notification.model.Notification import (
    Notification,
    NotificationStatus,
    NotificationType,
)
from core.otp.service.otpservice import OTPService
from core.user.model.User import User

logger = logging.getLogger(__name__)

MAX_LINKED_BUSINESSES = 10


class LinkedBusinessService:
    def __init__(self, db: Session):
        self.db = db
        self.auth_service = AuthService(db)
        self.otp_service = OTPService(db)

    def _item(self, user: User, *, manager_id: str, active_user_id: str) -> LinkedBusinessItem:
        return LinkedBusinessItem(
            id=user.id,
            email=user.email,
            fullname=user.fullname,
            company=user.company,
            is_manager=user.id == manager_id,
            is_active=user.id == active_user_id,
            onboarding_completed=bool(user.onboarding_completed),
            managed_by_user_id=user.managed_by_user_id,
            created_at=user.created_at,
        )

    def _can_access(self, manager: User, target: User) -> bool:
        if target.id == manager.id:
            return True
        return target.managed_by_user_id == manager.id

    def list_businesses(self, current_user: User, authjwt: AuthJWT) -> LinkedBusinessListResponse:
        manager = resolve_session_manager(authjwt, self.db, current_user)
        children = (
            self.db.query(User)
            .filter(User.managed_by_user_id == manager.id)
            .order_by(User.created_at.asc())
            .all()
        )
        items = [self._item(manager, manager_id=manager.id, active_user_id=current_user.id)]
        items.extend(
            self._item(child, manager_id=manager.id, active_user_id=current_user.id)
            for child in children
        )
        return LinkedBusinessListResponse(
            manager_id=manager.id,
            active_user_id=current_user.id,
            items=items,
        )

    def create_business(
        self,
        current_user: User,
        authjwt: AuthJWT,
        request: CreateBusinessRequest,
    ) -> LinkedBusinessItem:
        manager = resolve_session_manager(authjwt, self.db, current_user)
        email = (request.email or "").strip()
        username = (request.fullname or "").strip()

        linked_count = (
            self.db.query(User)
            .filter(User.managed_by_user_id == manager.id)
            .count()
        )
        if linked_count >= MAX_LINKED_BUSINESSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"You can attach at most {MAX_LINKED_BUSINESSES} businesses to this login",
            )

        existing = (
            self.db.query(User)
            .filter(
                (func.lower(User.email) == email.lower())
                | (func.lower(User.fullname) == username.lower())
            )
            .first()
        )
        if existing:
            if (existing.email or "").lower() == email.lower():
                raise UserAlreadyExistsError(field="email")
            raise UserAlreadyExistsError(field="username")

        user_id = self.auth_service.generate_user_id()
        random_password = secrets.token_urlsafe(32)
        db_user = User(
            id=user_id,
            fullname=username,
            email=email,
            hashed_password=self.auth_service.hash_password(random_password),
            ghana_card=manager.ghana_card,
            nationality=manager.nationality,
            date_of_birth=manager.date_of_birth,
            gender=manager.gender,
            address=manager.address,
            location=manager.location,
            profile_picture_url=manager.profile_picture_url,
            currency_code=manager.currency_code or "GHS",
            company=(request.company or "").strip() or None,
            enabled=True,
            onboarding_completed=False,
            onboarding_profile=None,
            managed_by_user_id=manager.id,
            created_at=datetime.now(timezone.utc),
        )

        seed = Notification(
            id=self.auth_service._generate_notification_id(),
            user_id=user_id,
            type=NotificationType.WARNING,
            data={
                "description": "You need to set up your business profile",
                "flutterpage": "Profile",
            },
            status=NotificationStatus.UNREAD,
            sms_sent=False,
        )
        self.db.add(db_user)
        self.db.add(seed)
        self.db.commit()
        self.db.refresh(db_user)

        try:
            from core.credits.service.credit_service import CreditService

            credits = CreditService(self.db)
            credits.grant_starter_credits(db_user.id)
            credits.ensure_referral_code(db_user)
        except Exception as e:
            logger.warning("Starter credits not granted for linked business %s: %s", db_user.id, e)

        return self._item(db_user, manager_id=manager.id, active_user_id=current_user.id)

    def switch_business(
        self,
        current_user: User,
        authjwt: AuthJWT,
        request: SwitchBusinessRequest,
    ) -> JSONResponse:
        manager = resolve_session_manager(authjwt, self.db, current_user)
        target = self.db.query(User).filter(User.id == request.user_id.strip()).first()
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
        if not self._can_access(manager, target):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot switch to this business",
            )
        payload = self.auth_service.issue_session_tokens(
            target, manager.id, status="Switched"
        )
        return JSONResponse(status_code=200, content=payload)

    def send_detach_otp(
        self,
        current_user: User,
        authjwt: AuthJWT,
        business_id: str,
    ):
        manager = resolve_session_manager(authjwt, self.db, current_user)
        target = self.db.query(User).filter(User.id == business_id).first()
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
        if target.id == manager.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your primary login cannot be detached this way",
            )
        if not self._can_access(manager, target):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot detach this business",
            )
        if not target.managed_by_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This business is already independent",
            )
        result = self.otp_service.send_otp_email(target.email)
        if not getattr(result, "success", False):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=getattr(result, "message", None) or "Could not send OTP",
            )
        return {
            "message": "Detach code sent",
            "email": target.email,
        }

    def detach_business(
        self,
        current_user: User,
        authjwt: AuthJWT,
        business_id: str,
        request: DetachBusinessRequest,
    ) -> JSONResponse:
        manager = resolve_session_manager(authjwt, self.db, current_user)
        target = self.db.query(User).filter(User.id == business_id).first()
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
        if target.id == manager.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your primary login cannot be detached this way",
            )
        if not self._can_access(manager, target):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot detach this business",
            )
        if not target.managed_by_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This business is already independent",
            )

        otp_ok = self.otp_service.validate_otp(
            email=target.email,
            otp=str(request.otp).strip(),
            consume=True,
        )
        if not otp_ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OTP",
            )

        was_active = current_user.id == target.id
        target.hashed_password = self.auth_service.hash_password(request.new_password)
        self.auth_service.unlink_managed_account(target)
        self.db.commit()

        if target.email:
            self.auth_service.session_driver.remove_tokens(target.email)

        content = {
            "message": "Business detached. It can now be signed into separately.",
            "switched_to_manager": was_active,
            "email": target.email,
        }
        if was_active:
            content.update(self.auth_service.issue_session_tokens(manager, manager.id, status="Switched"))
        return JSONResponse(status_code=200, content=content)
