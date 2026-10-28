
from fastapi import APIRouter, Depends, HTTPException, status, Query
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from core.auth.service.sessiondriver import SessionDriver, TokenData
from another_fastapi_jwt_auth import AuthJWT
from core.exceptions import *
from utilities.dbconfig import SessionLocal
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from core.user.model.User import User
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# DTO Models
from core.user.dto.request.user_filter_request import UserFilterRequest
from core.user.dto.response.message_response import MessageResponse
from core.user.dto.response.user_response import UserResponse
from core.user.dto.request.user_update_request import UserUpdateRequest

# Service Class
def _sender_email_of(user: User) -> Optional[str]:
    from core.email.sender_email import sender_email_from_user

    return sender_email_from_user(user)


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def _to_response(self, user: User, include_referral: bool = False) -> UserResponse:
        referral = {}
        if include_referral:
            try:
                from core.credits.service.credit_service import CreditService

                referral = CreditService(self.db).referral_profile(user)
            except Exception as exc:
                logger.warning("Referral profile skipped for %s: %s", user.id, exc)
        return UserResponse(
            id=user.id,
            fullname=user.fullname,
            email=user.email,
            phone=user.phone,
            nationality=user.nationality,
            date_of_birth=user.date_of_birth,
            gender=user.gender,
            address=user.address,
            location=user.location,
            ghana_card=user.ghana_card,
            profile_picture_url=user.profile_picture_url,
            company=user.company,
            current_branch=user.current_branch,
            staff_id=user.staff_id,
            facebook_url=user.facebook_url,
            whatsapp_number=user.whatsapp_number,
            linkedin_url=user.linkedin_url,
            twitter_url=user.twitter_url,
            instagram_url=user.instagram_url,
            profile_sharing=user.profile_sharing,
            in_app_notification=user.in_app_notification,
            sms_notification=user.sms_notification,
            onboarding_completed=bool(getattr(user, "onboarding_completed", True)),
            onboarding_profile=user.onboarding_profile
            if isinstance(getattr(user, "onboarding_profile", None), dict)
            else None,
            currency_code=(getattr(user, "currency_code", None) or "GHS").upper(),
            sender_email=_sender_email_of(user),
            managed_by_user_id=getattr(user, "managed_by_user_id", None),
            referral_code=referral.get("referral_code"),
            referrals_invited=int(referral.get("referrals_invited") or 0),
            referrals_joined=int(referral.get("referrals_joined") or 0),
            referral_rewards=float(referral.get("referral_rewards") or 0),
            referral_reward_credits=float(referral.get("referral_reward_credits") or 0),
            referral_welcome_credits=float(referral.get("referral_welcome_credits") or 0),
            enabled=user.enabled,
            status=user.status,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    def get_current_user(self, identifier: str) -> UserResponse:
        # Try to find by email first, then by id as a fallback.
        user = self.db.query(User).filter(User.email == identifier).first()
        if not user:
            user = self.db.query(User).filter(User.id == identifier).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return self._to_response(user, include_referral=True)

    def get_user_by_id(self, user_id: str) -> UserResponse:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return self._to_response(user, include_referral=True)

    # get user by phone number
    def get_user_by_phone(self, phone: str) -> UserResponse:
        user = self.db.query(User).filter(User.phone == phone).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return self._to_response(user, include_referral=True)
    
    def set_user_enabled_status(self, user_id: str, enabled: bool) -> MessageResponse:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.enabled = enabled
        self.db.commit()
        status_msg = "enabled" if enabled else "disabled"
        return MessageResponse(message=f"User {status_msg} successfully")

    def delete_user(self, user_id: str) -> MessageResponse:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        self.db.delete(user)
        self.db.commit()
        return MessageResponse(message="User deleted successfully")

    def get_all_users_paged(self, page: int, size: int):
        query = self.db.query(User)
        total = query.count()
        users = query.offset((page - 1) * size).limit(size).all()
        
        return {
            "total": total,
            "page": page,
            "size": size,
            "users": [self._to_response(user) for user in users]
        }

    def _resolve_identifier(self, identifier: str) -> User:
        user = self.db.query(User).filter(User.email == identifier).first()
        if not user:
            user = self.db.query(User).filter(User.id == identifier).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def _apply_profile_update(self, user: User, payload: UserUpdateRequest) -> UserResponse:
        data = payload.model_dump(exclude_unset=True)
        if "email" in data:
            data.pop("email", None)
        if "fullname" in data:
            name = (data.get("fullname") or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Full name is required")
            clash = (
                self.db.query(User)
                .filter(User.fullname == name, User.id != user.id)
                .first()
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail="That full name is already used by another account. Choose a different name.",
                )
            data["fullname"] = name

        changed = set(data.keys())
        for key, value in data.items():
            if hasattr(user, key):
                setattr(user, key, value)

        user.updated_at = datetime.utcnow()
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise HTTPException(
                status_code=409,
                detail="That profile update conflicts with another account. Check your name and try again.",
            )
        self.db.refresh(user)

        if changed & {"company", "location", "whatsapp_number", "phone", "fullname"}:
            try:
                from core.intelligence.service.onboarding_index_service import (
                    OnboardingIndexService,
                )

                OnboardingIndexService(self.db).refresh_from_user_profile(user)
            except Exception as e:
                logger.warning(
                    "Onboarding reindex after profile update failed for %s: %s",
                    user.id,
                    e,
                    exc_info=True,
                )
        return self._to_response(user)

    def update_user(self, email: str, payload: UserUpdateRequest) -> UserResponse:
        logger.debug(
            "Updating user %s with data: %s",
            email,
            payload.model_dump(exclude_unset=True),
        )
        return self._apply_profile_update(self._resolve_identifier(email), payload)

    def update_current_user(self, email: str, payload: UserUpdateRequest) -> UserResponse:
        return self._apply_profile_update(self._resolve_identifier(email), payload)

    def update_current_user_sender_email(self, identifier: str, sender_email: str) -> UserResponse:
        from core.email.sender_email import SenderEmailInvalid, normalize_sender_email

        user = self._resolve_identifier(identifier)
        try:
            normalized = normalize_sender_email(sender_email)
        except SenderEmailInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        current = user.get_agent("email_agent") or {}
        params = dict(current.get("params") or {})
        params["sender_email"] = normalized
        current["params"] = params
        current.setdefault("status", "active")
        user.set_agent("email_agent", current)
        flag_modified(user, "agents")
        user.updated_at = datetime.utcnow()
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return self._to_response(user)

    def update_current_user_notification_settings(
        self,
        identifier: str,
        *,
        in_app_notification: bool | None = None,
        sms_notification: bool | None = None,
    ) -> UserResponse:
        user = self.db.query(User).filter(User.email == identifier).first()
        if not user:
            user = self.db.query(User).filter(User.id == identifier).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if in_app_notification is not None:
            user.in_app_notification = in_app_notification
        if sms_notification is not None:
            user.sms_notification = sms_notification

        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return self.get_user_by_id(user.id)

    def update_current_user_profile_image(self, identifier: str, *, profile_picture_url: str) -> UserResponse:
        user = self.db.query(User).filter(User.email == identifier).first()
        if not user:
            user = self.db.query(User).filter(User.id == identifier).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.profile_picture_url = profile_picture_url
        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return self.get_user_by_id(user.id)

    def update_user_notification_settings(
        self,
        user_id: str,
        *,
        in_app_notification: bool | None = None,
        sms_notification: bool | None = None,
    ) -> UserResponse:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if in_app_notification is not None:
            user.in_app_notification = in_app_notification
        if sms_notification is not None:
            user.sms_notification = sms_notification

        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return self.get_user_by_id(user.id)

    def update_user_profile_image(self, user_id: str, *, profile_picture_url: str) -> UserResponse:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.profile_picture_url = profile_picture_url
        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return self.get_user_by_id(user.id)

    def _resolve_user_by_phone(self, user_id: str) -> User:
        """Resolve a user from their WhatsApp / conversation phone id."""
        user = self.db.query(User).filter(User.phone == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def update_user_details(self, user_id: str, update_data: dict) -> UserResponse:
        """Update profile fields for the user identified by phone."""
        user = self._resolve_user_by_phone(user_id)
        field_map = {
            "phone": "phone",
            "phone_number": "phone",
            "username": "fullname",
            "fullname": "fullname",
            "name": "fullname",
            "location": "location",
            "occupation": "occupation",
            "address": "address",
            "company": "company",
        }

        updated = False
        for key, value in (update_data or {}).items():
            if value is None:
                continue
            attr = field_map.get(key, key)
            if hasattr(user, attr):
                setattr(user, attr, value)
                updated = True

        if not updated:
            raise HTTPException(
                status_code=400,
                detail="No valid profile fields to update.",
            )

        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return self.get_user_by_id(user.id)

    def get_user_profile(self, user_id: str) -> dict:
        """Return a profile summary dict for NLU display."""
        user = self._resolve_user_by_phone(user_id)
        email_agent = user.get_agent("email_agent") if user.agents else None
        sender_email = None
        if email_agent:
            sender_email = (email_agent.get("params") or {}).get("sender_email")

        return {
            "fullname": user.fullname,
            "username": user.fullname,
            "email": user.email,
            "phone": user.phone,
            "location": user.location,
            "occupation": user.occupation,
            "company": user.company,
            "sender_email": sender_email,
        }