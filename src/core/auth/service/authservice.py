from fastapi.responses import JSONResponse
import jwt
from passlib.context import CryptContext
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from fastapi import HTTPException
from fastapi import status
from datetime import datetime, timedelta, timezone
from typing import Optional
from core.auth.service.sessiondriver import SessionDriver
from core.exceptions.AuthException import InvalidCredentialsError, LinkedBusinessLoginError
from core.exceptions.UserException import GreenAccountExistsError, UserAlreadyExistsError
from core.user.model.User import User
from core.notification.model.Notification import (
    Notification,
    NotificationStatus,
    NotificationType,
)
from core.otp.service.otpservice import OTPService
import re
import secrets
import string
import logging
import os

logger = logging.getLogger(__name__)

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.session_driver = SessionDriver()
        self.otp_service = OTPService(db)

    @staticmethod
    def hash_password_static(password: str) -> str:
        return pwd_context.hash(password)

    def hash_password(self, password: str) -> str:
        """Hash a plain-text password."""
        return self.hash_password_static(password)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a plain-text password against a hashed one."""
        return pwd_context.verify(plain_password, hashed_password)

    def generate_user_id(self):
        """Generate a random user ID with alphanumeric characters."""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for i in range(20))

    @staticmethod
    def resolve_signup_email(email: Optional[str], phone: Optional[str]) -> str:
        """Never persist a blank email. JWT subject is the email, and a blank
        subject is rejected by onboarding and other authenticated routes.
        """
        cleaned = (email or "").strip()
        if cleaned:
            if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A valid email is required",
                )
            return cleaned
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) < 9:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email or phone is required",
            )
        return f"{digits}@phone.useautobus.com"

    @staticmethod
    def session_subject(user: User) -> str:
        """Subject stored in access and refresh tokens. Never blank."""
        email = (getattr(user, "email", None) or "").strip()
        if email:
            return email
        user_id = (getattr(user, "id", None) or "").strip()
        if user_id:
            return user_id
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not issue a session",
        )

    def _generate_notification_id(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for i in range(16))

    def create_user(self, request: BaseModel):
        """Create a new user in the database."""
        email = self.resolve_signup_email(request.email, request.phone)
        if self._green_account_presence(email) is True:
            adopted = self._adopt_green_account(
                email,
                request.password,
                referral_code=getattr(request, "referral_code", None),
            )
            if adopted is None:
                raise GreenAccountExistsError()
            self._keep_signup_extras(adopted, request)
            return {
                "message": "Your Green Account is on Autobus. You can add the rest of your business details next.",
                "user_id": adopted.id,
                "verification_required": False,
                "adopted": True,
                "otp_sent": False,
            }
        username = (request.fullname or "").strip()
        existing_user = (
            self.db.query(User)
            .filter(
                (func.lower(User.email) == email.lower())
                | (func.lower(User.fullname) == username.lower())
            )
            .first()
        )

        if existing_user:
            if (existing_user.email or "").lower() == email.lower():
                raise UserAlreadyExistsError(field="email")
            else:
                raise UserAlreadyExistsError(field="username")
            
        user_id = self.generate_user_id()

        db_user = User(
            id=user_id,
            fullname=request.fullname,
            phone=request.phone,
            email=email,
            hashed_password=self.hash_password(request.password),
            profile_picture_url=request.profile_picture_url,
            
            nationality=request.nationality,
            date_of_birth=request.date_of_birth,
            gender=request.gender,
            address=request.address,
            location=request.location,
            ghana_card=request.ghana_card,
            
            company=request.company,
            current_branch=request.current_branch,
            staff_id=request.staff_id,
            
            facebook_url=request.facebook_url,
            whatsapp_number=request.whatsapp_number,
            linkedin_url=request.linkedin_url,
            twitter_url=request.twitter_url,
            instagram_url=request.instagram_url,
            
            in_app_notification=request.in_app_notification,
            sms_notification=request.sms_notification,
            onboarding_completed=False,
            onboarding_profile=None,
            
            created_at=datetime.now(timezone.utc),
        )

        seed_notifications = [
            Notification(
                id=self._generate_notification_id(),
                user_id=user_id,
                type=NotificationType.WARNING,
                data={
                    "description": "Enable 2FA  to ensure added security",
                    "flutterpage": "Security",
                },
                status=NotificationStatus.UNREAD,
                sms_sent=False,
            ),
            Notification(
                id=self._generate_notification_id(),
                user_id=user_id,
                type=NotificationType.WARNING,
                data={
                    "description": "You need to set up your business profile",
                    "flutterpage": "Profile",
                },
                status=NotificationStatus.UNREAD,
                sms_sent=False,
            ),
        ]

        self.db.add(db_user)
        self.db.add_all(seed_notifications)
        self.db.commit()
        self.db.refresh(db_user)
        self._grant_signup_credits(
            db_user,
            getattr(request, "referral_code", None),
            new_account=True,
        )

        otp_send_result = None
        try:
            # Prefer phone OTP for account enablement (current `/verify-otp` contract uses phone).
            if getattr(request, "phone", None):
                otp_send_result = self.otp_service.send_otp_phone(request.phone)
            elif getattr(request, "email", None):
                otp_send_result = self.otp_service.send_otp_email(request.email)
        except Exception as e:
            logger.error(f"OTP send error during signup for user {db_user.id}: {e}", exc_info=True)

        otp_sent = bool(getattr(otp_send_result, "success", False)) if otp_send_result is not None else False
        otp_message = (
            "User account created successfully. Please verify your phone number with the OTP sent to you."
            if otp_sent
            else "User account created successfully, but we couldn't send your OTP. Please request a new OTP."
        )

        return {
            "message": otp_message,
            "user_id": db_user.id,
            "verification_required": True,
            "otp_sent": otp_sent,
            "otp_send_error": None if otp_sent else getattr(otp_send_result, "message", None),
        }
    
    def verify_and_enable_user(self, phone: str, otp: str):
        """Verify OTP and enable user account"""
        # Validate OTP
        is_valid = self.otp_service.validate_otp(phone=phone, otp=otp)
        
        if not is_valid:
            return {
                "success": False,
                "message": "Invalid or expired OTP"
            }
        
        # Find user by phone
        user = self.db.query(User).filter(User.phone == phone).first()
        
        if not user:
            return {
                "success": False,
                "message": "User not found"
            }
        
        # Enable user account
        user.enabled = True
        user.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(user)
        self._publish_green_account(user)

        payload = {
            "success": True,
            "message": "Phone number verified successfully. Your account is now active.",
            "user_id": user.id,
        }
        try:
            payload.update(self.issue_session_tokens(user, user.id, status="Phone verified"))
        except Exception as e:
            logger.error("Could not issue session after OTP for %s: %s", user.id, e)
        return payload
           
    def authenticate_user(self, identifier: str, password: str):
        identifier = identifier.strip()
        if not identifier:
            raise InvalidCredentialsError()

        ident = identifier.lower()
        db_user = self.db.query(User).filter(
            (func.lower(User.email) == ident) | (func.lower(User.fullname) == ident)
        ).first()

        if not db_user:
            raise InvalidCredentialsError()

        if not self.verify_password(password, db_user.hashed_password):
            raise InvalidCredentialsError()

        from core.user.model.User import UserStatus

        if getattr(db_user, "status", None) == UserStatus.DELETED or str(
            getattr(db_user, "status", "") or ""
        ).upper() == UserStatus.DELETED.value:
            raise InvalidCredentialsError()

        if db_user.managed_by_user_id:
            raise LinkedBusinessLoginError()

        return db_user

    def issue_session_tokens(self, user: User, manager_id: str, status: str = "Login successful"):
        """Mint access + refresh tokens with active-business `sub` and session `mgr`."""
        claims = {"sub": self.session_subject(user), "mgr": manager_id}
        access_token = self.session_driver.create_access_token(
            data=claims,
            expires_delta=timedelta(minutes=self.session_driver.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = self.session_driver.create_refresh_token(data=claims)
        self.session_driver.store_tokens(access_token, refresh_token)
        return {
            "status": status,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": self.session_driver.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

    def signin(self, user: BaseModel):
        """Login the user by generating a JWT token and returning tokens."""
        identifier = (user.login_identifier or "").strip()
        password = user.password
        email = self._email_for_green_lookup(identifier)
        exists = self._green_account_presence(email) if email else None
        if exists is True:
            db_user = self._adopt_green_account(email, password)
            if db_user is None:
                raise InvalidCredentialsError()
        else:
            try:
                db_user = self.authenticate_user(identifier, password)
            except InvalidCredentialsError:
                if exists is None and email:
                    db_user = self._adopt_green_account(email, password)
                    if db_user is None:
                        raise
                else:
                    raise
            else:
                if db_user.enabled:
                    self._publish_green_account(db_user)
        payload = self.issue_session_tokens(db_user, db_user.id)
        return JSONResponse(status_code=200, content=payload)

    def _keep_signup_extras(self, db_user: User, request: BaseModel) -> None:
        """Keep Autobus-only answers from the form. Identity stays with Green Account."""
        changed = False
        company = (getattr(request, "company", None) or "").strip()
        if company and not (db_user.company or "").strip():
            db_user.company = company
            changed = True
        phone = (getattr(request, "phone", None) or "").strip()
        if phone and not (db_user.phone or "").strip():
            db_user.phone = phone
            changed = True
        card = (getattr(request, "ghana_card", None) or "").strip()
        if card and not card.startswith("GHA--") and not (db_user.ghana_card or "").strip():
            db_user.ghana_card = card
            changed = True
        if not changed:
            return
        db_user.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(db_user)
        self._publish_green_account(db_user)

    def _email_for_green_lookup(self, identifier: str) -> Optional[str]:
        cleaned = (identifier or "").strip()
        if "@" in cleaned:
            if cleaned.lower().endswith("@phone.useautobus.com"):
                return None
            return cleaned
        if not cleaned:
            return None
        local = (
            self.db.query(User)
            .filter(func.lower(User.fullname) == cleaned.lower())
            .first()
        )
        email = (getattr(local, "email", None) or "").strip()
        if "@" not in email or email.lower().endswith("@phone.useautobus.com"):
            return None
        return email

    def _green_account_presence(self, email: str) -> Optional[bool]:
        """True or False when Green Account answers. None when the check cannot run."""
        if "@" not in (email or "") or email.lower().endswith("@phone.useautobus.com"):
            return False
        try:
            from core.auth.service.green_account_client import email_exists

            return email_exists(email)
        except Exception as exc:
            logger.warning("Green account lookup failed: %s", exc)
            return None

    def _publish_green_account(self, db_user: User) -> None:
        try:
            from core.auth.service.green_account_client import publish_user

            publish_user(db_user)
        except Exception as exc:
            logger.warning("Green account publish failed for %s: %s", getattr(db_user, "id", ""), exc)

    def _grant_signup_credits(self, user: User, referral_code: Optional[str], *, new_account: bool) -> None:
        try:
            from core.credits.service.credit_service import CreditService

            credits = CreditService(self.db)
            credits.grant_starter_credits(user.id)
            credits.ensure_referral_code(user)
            if new_account:
                credits.apply_referral(user.id, referral_code)
        except Exception as exc:
            logger.warning("Signup credits not granted for %s: %s", getattr(user, "id", None), exc)

    def _adopt_green_account(
        self,
        identifier: str,
        password: str,
        referral_code: Optional[str] = None,
    ) -> Optional[User]:
        """Create or unlock the local user when the Green account password matches."""
        from core.auth.service.green_account_client import authenticate
        from core.user.model.User import UserStatus

        account = authenticate(identifier, password)
        if account is None:
            return None
        email = (account.get("email") or "").strip()
        if not email:
            return None

        db_user = (
            self.db.query(User)
            .filter(func.lower(User.email) == email.lower())
            .first()
        )
        if db_user is not None:
            if str(getattr(db_user, "status", "") or "").upper() == UserStatus.DELETED.value:
                return None
            if db_user.managed_by_user_id:
                raise LinkedBusinessLoginError()
            db_user.hashed_password = self.hash_password(password)
            db_user.enabled = True
            if not (db_user.phone or "").strip() and account.get("phone"):
                db_user.phone = account.get("phone")
            if not (db_user.ghana_card or "").strip() and account.get("ghana_card"):
                db_user.ghana_card = account.get("ghana_card")
            db_user.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(db_user)
            self._publish_green_account(db_user)
            return db_user

        fullname = self._unique_fullname(account.get("display_name") or email.split("@")[0])
        db_user = User(
            id=self.generate_user_id(),
            fullname=fullname,
            phone=account.get("phone"),
            email=email,
            hashed_password=self.hash_password(password),
            ghana_card=account.get("ghana_card"),
            onboarding_completed=False,
            enabled=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(db_user)
        self.db.commit()
        self.db.refresh(db_user)
        self._grant_signup_credits(db_user, referral_code, new_account=True)
        self._publish_green_account(db_user)
        return db_user

    def _unique_fullname(self, preferred: str) -> str:
        base = (preferred or "Green Account").strip() or "Green Account"
        candidate = base
        suffix = 2
        while (
            self.db.query(User)
            .filter(func.lower(User.fullname) == candidate.lower())
            .first()
            is not None
        ):
            candidate = f"{base} {suffix}"
            suffix += 1
        return candidate

    def unlink_managed_account(self, db_user: User) -> bool:
        """Clear managed_by so the account can sign in independently. Returns True if it was linked."""
        if not db_user.managed_by_user_id:
            return False
        db_user.managed_by_user_id = None
        return True

    def signout(self, token: str):
        try:
            # Decode without expiration check
            payload = jwt.decode(
                token, 
                self.session_driver.SECRET_KEY, 
                algorithms=[self.session_driver.ALGORITHM],
                options={"verify_exp": False}
            )
            email = payload.get("sub")
            
            if not email:
                raise HTTPException(status_code=401, detail="Invalid token")
            
            # Triple protection:
            # 1. Blacklist this specific token
            self.session_driver.blacklist_token(token)
            
            # 2. Remove token storage
            self.session_driver.remove_tokens(email)
            
            return JSONResponse(
                status_code=200,
                content={"message": "Logout successful"}
            )
        except Exception as e:
            logger.error(f"Logout error: {str(e)}")
            # Still attempt to blacklist
            self.session_driver.blacklist_token(token)
            raise HTTPException(status_code=500, detail="Logout processing error")

    def refresh_tokens(self, refresh_token: str):
        """Refresh access token using refresh token"""
        try:
            new_access_token = self.session_driver.refresh_access_token(refresh_token)
            
            return JSONResponse(
                status_code=200,
                content={
                    "access_token": new_access_token,
                    "refresh_token": refresh_token,
                    "token_type": "bearer",
                    "expires_in": self.session_driver.ACCESS_TOKEN_EXPIRE_MINUTES * 60
                }
            )
        except HTTPException as e:
            raise e
        except Exception as e:
            logger.error(f"Error refreshing token: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
        
    def signout_all(self, token: str):
        """Logout the user from all devices by invalidating all their tokens"""
        try:
            payload = jwt.decode(
                token, 
                self.session_driver.SECRET_KEY, 
                algorithms=[self.session_driver.ALGORITHM],
                options={"verify_exp": False}
            )
            email = payload.get("sub")
            
            if not email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token"
                )
            
            # Triple protection:
            # 1. Blacklist this specific token
            self.session_driver.blacklist_token(token)
            
            # 2. Remove token storage
            self.session_driver.remove_tokens(email)
            
            return JSONResponse(
                status_code=200,
                content={"message": "Logged out from all devices"}
            )
        except jwt.PyJWTError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
    
    def verify_account(self, request: BaseModel):
        """Check that an account exists for the given email or phone (password recovery)."""
        email = getattr(request, "email", None)
        phone = getattr(request, "phone", None)

        if email:
            db_user = self.db.query(User).filter(User.email == email).first()
            not_found_detail = "No account found with this email"
        elif phone:
            db_user = self.db.query(User).filter(User.phone == phone).first()
            not_found_detail = "No account found with this phone number"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide either email or phone",
            )

        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=not_found_detail,
            )

        return JSONResponse(
            status_code=200,
            content={"message": "Account verified successfully"},
        )
            
    def reset_password(self, request: BaseModel):
        """Reset password using a valid reset token (authenticated version)"""
        try:
            # Verify the reset token
            payload = jwt.decode(
                request.reset_token,
                self.session_driver.SECRET_KEY,
                algorithms=[self.session_driver.ALGORITHM]
            )
            
            if payload.get("type") != "reset":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid token type"
                )
                
            email = payload.get("sub")
            if not email:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid token payload"
                )
            
            # Find user and update password
            db_user = self.db.query(User).filter(User.email == email).first()
            if not db_user:
                raise InvalidCredentialsError()
                
            db_user.hashed_password = self.hash_password(request.new_password)
            self.unlink_managed_account(db_user)
            self.db.commit()
            if db_user.enabled:
                self._publish_green_account(db_user)
            
            # Invalidate all existing tokens
            self.session_driver.remove_tokens(email)
            
            return JSONResponse(
                status_code=200,
                content={"message": "Password reset successfully"}
            )
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token has expired"
            )
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reset token"
            )

    def reset_password_no_auth(self, request: BaseModel):
        """
        Forgotten-password reset. Requires a valid OTP sent via /api/v1/otp/send
        to either the account email or phone. Never resets with identifier alone.
        """
        try:
            from core.otp.service.otpservice import OTPService

            otp_code = getattr(request, "otp", None)
            email = getattr(request, "email", None)
            phone = getattr(request, "phone", None)

            if not otp_code:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="OTP is required to reset password",
                )
            if bool(email) == bool(phone):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Provide either email or phone, not both or neither",
                )

            otp_ok = OTPService(self.db).validate_otp(
                email=email,
                phone=phone,
                otp=str(otp_code).strip(),
                consume=True,
            )
            if not otp_ok:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid or expired OTP",
                )

            if email:
                db_user = self.db.query(User).filter(User.email == email).first()
            else:
                db_user = self.db.query(User).filter(User.phone == phone).first()

            if not db_user:
                # OTP already consumed; do not reveal whether the account exists.
                return JSONResponse(
                    status_code=200,
                    content={"message": "If the account exists, password has been reset"},
                )

            db_user.hashed_password = self.hash_password(request.new_password)
            from core.auth.dependencies import platform_role_of

            if platform_role_of(db_user):
                db_user.enabled = True
            self.unlink_managed_account(db_user)
            self.db.commit()
            if db_user.enabled:
                self._publish_green_account(db_user)
            if db_user.email:
                self.session_driver.remove_tokens(db_user.email)

            return JSONResponse(
                status_code=200,
                content={"message": "If the account exists, password has been reset"},
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Password reset error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error resetting password",
            )

    def generate_password_reset_token(self, email: str) -> str:
        """Generate a password reset token for the user"""
        try:
            # Verify user exists
            db_user = self.db.query(User).filter(User.email == email).first()
            if not db_user:
                # Don't reveal whether email exists
                return None
                
            # Create reset token that expires in 1 hour
            reset_data = {
                "sub": email,
                "type": "reset",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1)
            }
            
            return jwt.encode(
                reset_data,
                self.session_driver.SECRET_KEY,
                algorithm=self.session_driver.ALGORITHM
            )
            
        except Exception as e:
            logger.error(f"Error generating reset token: {str(e)}")
            return None