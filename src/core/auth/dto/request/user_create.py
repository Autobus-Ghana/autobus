from datetime import date, datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from core.auth.dto.request.password_policy import PASSWORD_MIN_LENGTH

class UserCreateRequest(BaseModel):  
    fullname: Optional[str] = None
    username: Optional[str] = None
    email: str
    phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH)
    
    # Personal Information
    nationality: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    location: Optional[str] = None
    ghana_card: str = Field(..., min_length=3)
    
    # Membership Information
    company: Optional[str] = None
    current_branch: Optional[str] = None
    staff_id: Optional[str] = None
    
    # Connection Information
    facebook_url: Optional[str] = None
    whatsapp_number: Optional[str] = None
    linkedin_url: Optional[str] = None
    twitter_url: Optional[str] = None
    instagram_url: Optional[str] = None
    
    # Notification Preferences
    profile_sharing: Optional[bool] = None
    in_app_notification: Optional[bool] = None
    sms_notification: Optional[bool] = None

    referral_code: Optional[str] = None

    @model_validator(mode="after")
    def resolve_username(self):
        name = (self.username or self.fullname or "").strip()
        if not name:
            raise ValueError("Username is required")
        self.fullname = name
        return self
