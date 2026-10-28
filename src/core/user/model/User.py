from sqlalchemy import JSON, Column, Integer, String, DateTime, ForeignKey, Boolean, Date, Enum as SQLEnum
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, object_session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.dialects.postgresql import JSON as JSONB
from utilities.dbconfig import Base
from datetime import datetime, date
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from enum import Enum as PyEnum

from sqlalchemy import String, Boolean, DateTime
from sqlalchemy.orm import relationship, Mapped, mapped_column

# At the top of core/user/model/User.py
from core.auth.model.password_reset_token import PasswordResetToken
from core.auth.model.refreshtoken import RefreshToken
from core.notification.model.Notification import Notification
from core.histories.model.history import History

# Add this TYPE_CHECKING block
if TYPE_CHECKING:
    from core.auth.model.password_reset_token import PasswordResetToken
    from core.auth.model.refreshtoken import RefreshToken
    from core.notification.model.Notification import Notification
    from core.histories.model.history import History
    from core.paystack.model.transaction import Transaction


class UserStatus(str, PyEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DELETED = "DELETED"

class Gender(str, PyEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"

class MembershipType(str, PyEnum):
    BASIC = "BASIC"
    PREMIUM = "PREMIUM"
    VIP = "VIP"
    STANDARD = "STANDARD"

class BooleanEnum(str, PyEnum):
    YES = "YES"
    NO = "NO"
    
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, primary_key=True)
    fullname: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String)
    
    # Personal Information
    nationality: Mapped[Optional[str]] = mapped_column(String(100))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    gender: Mapped[Optional[str]] = mapped_column(String, default=None)
    address: Mapped[Optional[str]] = mapped_column(String(300))
    location: Mapped[Optional[str]] = mapped_column(String(255))
    ghana_card: Mapped[Optional[str]] = mapped_column(String(100))
    profile_picture_url: Mapped[Optional[str]] = mapped_column(String(200))
    
    # Membership Information
    company: Mapped[Optional[str]] = mapped_column(String, default=None)
    current_branch: Mapped[Optional[str]] = mapped_column(String(100))
    staff_id: Mapped[Optional[str]] = mapped_column(String(50))
    
    # Professional Information
    occupation: Mapped[Optional[str]] = mapped_column(String(100))
    organization_workplace: Mapped[Optional[str]] = mapped_column(String(200))
    skills: Mapped[Optional[List[str]]] = mapped_column(JSON)
    experiences: Mapped[Optional[List[str]]] = mapped_column(JSON)
    
    # Social Media Profiles
    facebook_url: Mapped[Optional[str]] = mapped_column(String(200))
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(20))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(200))
    twitter_url: Mapped[Optional[str]] = mapped_column(String(200))
    instagram_url: Mapped[Optional[str]] = mapped_column(String(200))
    
    # Notification Preferences
    profile_sharing: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    in_app_notification: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    sms_notification: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    # NEW: Agents Configuration JSONB Field
    agents: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB, 
        default={},  # Empty dict by default
        server_default='{}'  # For database-level default
    )

    # Signup questionnaire answers, also indexed into Qdrant intelligence.
    onboarding_profile: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        default=None,
        nullable=True,
    )
    # Existing accounts default to completed so they are not forced through the wizard.
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Catalog / AI pricing currency. Ghana-first default.
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, default="GHS", server_default="GHS"
    )

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Stable invite code. Null until the first profile read or signup grant.
    referral_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, unique=True, index=True)
    referred_by_user_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    # Platform admin portal role: super_admin | admin | support. Null = merchant user.
    platform_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # When set, this user is a linked business managed from another login.
    # Password sign-in is blocked until detach (password reset) clears the FK.
    managed_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(20),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    
    status: Mapped[UserStatus] = mapped_column(String, nullable=False, default=UserStatus.ACTIVE)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    
    # Relationships
    password_reset_tokens: Mapped[List["PasswordResetToken"]] = relationship(
        "PasswordResetToken", 
        back_populates="user",
        cascade="all, delete-orphan"
    )
    
    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(
        "RefreshToken", 
        back_populates="user",
        cascade="all, delete-orphan"
    )

    # Notifications relationship (one-to-many)
    notifications: Mapped[List["Notification"]] = relationship(
        "Notification", 
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )
    
    # Financial Records relationship (one-to-many)
    financial_records: Mapped[List["History"]] = relationship(
        "History",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )
    
    # Transactions relationship (one-to-many)
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    def __repr__(self):
        return f"<User(id={self.id}, email={self.email})>"
    
    # ========== Agent Management Helper Methods ==========
    
    def get_agent(self, agent_name: str) -> Optional[Dict[str, Any]]:
        """Get a specific agent configuration by name"""
        return self.agents.get(agent_name) if self.agents else None
    
    def set_agent(self, agent_name: str, config: Dict[str, Any]) -> None:
        """Set or update an agent configuration"""
        if self.agents is None:
            self.agents = {}
        self.agents[agent_name] = config
        # Mark the agents attribute as modified for SQLAlchemy change tracking
        if object_session(self) is not None:
            flag_modified(self, "agents")
    
    def update_agent(self, agent_name: str, updates: Dict[str, Any]) -> None:
        """Update specific fields of an agent configuration"""
        if self.agents and agent_name in self.agents:
            self.agents[agent_name].update(updates)
            # Mark the agents attribute as modified for SQLAlchemy change tracking
            if object_session(self) is not None:
                flag_modified(self, "agents")
    
    def remove_agent(self, agent_name: str) -> None:
        """Remove an agent configuration"""
        if self.agents and agent_name in self.agents:
            del self.agents[agent_name]
            # Mark the agents attribute as modified for SQLAlchemy change tracking
            if object_session(self) is not None:
                flag_modified(self, "agents")
    
    def list_agents(self) -> List[str]:
        """Get list of all agent names for this user"""
        return list(self.agents.keys()) if self.agents else []
    
    def get_agent_status(self, agent_name: str) -> Optional[str]:
        """Get the status of a specific agent"""
        agent = self.get_agent(agent_name)
        return agent.get('status') if agent else None
    
    def activate_agent(self, agent_name: str) -> None:
        """Activate an agent"""
        self.update_agent(agent_name, {'status': 'active'})
    
    def deactivate_agent(self, agent_name: str) -> None:
        """Deactivate an agent"""
        self.update_agent(agent_name, {'status': 'inactive'})
    
    def get_agents_by_status(self, status: str) -> Dict[str, Any]:
        """Get all agents with a specific status"""
        if not self.agents:
            return {}
        return {
            name: config 
            for name, config in self.agents.items() 
            if config.get('status') == status
        }
    
    # ========== Property Methods ==========
    
    @property
    def password(self):
        return self.hashed_password

    @property
    def is_active(self):
        return self.enabled

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)