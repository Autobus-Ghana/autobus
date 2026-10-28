from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class SocialPlatformEnum(str, Enum):
    """Supported platforms"""
    TWITTER = "TWITTER"
    LINKEDIN = "LINKEDIN"
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"
    WHATSAPP = "WHATSAPP"
    TIKTOK = "TIKTOK"
    YOUTUBE = "YOUTUBE"
    THREADS = "THREADS"
    BLUESKY = "BLUESKY"
    MASTODON = "MASTODON"
    PINTEREST = "PINTEREST"
    REDDIT = "REDDIT"
    GOOGLE_BUSINESS = "GOOGLE_BUSINESS"
    SNAPCHAT = "SNAPCHAT"
    TELEGRAM = "TELEGRAM"
    DISCORD = "DISCORD"
    SLACK = "SLACK"


# OAuth and Account Connection DTOs
class OAuth2CallbackRequest(BaseModel):
    """OAuth callback request from Blotato"""
    code: str = Field(..., description="Authorization code from Blotato")
    state: str = Field(..., description="State parameter for CSRF protection")
    error: Optional[str] = Field(None, description="Error message if authorization failed")
    error_description: Optional[str] = Field(None, description="Error description")

    class Config:
        json_schema_extra = {
            "example": {
                "code": "auth_code_from_blotato",
                "state": "random_state_string",
            }
        }


class SocialAccountResponse(BaseModel):
    """Response model for a connected social account"""
    id: str
    platform: str
    account_id: str
    account_name: str
    platform_user_id: Optional[str] = None
    is_active: bool
    connected_at: datetime
    last_used_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "sa_123456",
                "platform": "TWITTER",
                "account_id": "blotato_acc_789",
                "account_name": "john_doe",
                "platform_user_id": "1234567890",
                "is_active": True,
                "connected_at": "2024-01-15T10:30:00Z",
                "last_used_at": "2024-01-16T14:22:00Z"
            }
        }


class SocialAccountsListResponse(BaseModel):
    """Response model for list of social accounts"""
    accounts: List[SocialAccountResponse]
    total: int

    class Config:
        json_schema_extra = {
            "example": {
                "accounts": [
                    {
                        "id": "sa_123456",
                        "platform": "TWITTER",
                        "account_id": "blotato_acc_789",
                        "account_name": "john_doe",
                        "platform_user_id": "1234567890",
                        "is_active": True,
                        "connected_at": "2024-01-15T10:30:00Z",
                        "last_used_at": "2024-01-16T14:22:00Z"
                    }
                ],
                "total": 1
            }
        }


class DisconnectAccountRequest(BaseModel):
    """Request to disconnect a social account"""
    disconnect_from_blotato: bool = Field(
        False,
        description="Whether to also disconnect from Blotato"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "disconnect_from_blotato": False
            }
        }


# Post Publishing DTOs
class PublishMediaItem(BaseModel):
    """Media item for publishing"""
    url: str = Field(..., description="URL or media ID")
    type: Optional[str] = Field(None, description="Media type: image, video, gif, etc.")

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://example.com/image.jpg",
                "type": "image"
            }
        }


class PublishPostRequest(BaseModel):
    """Request to publish post to social media"""
    account_ids: List[str] = Field(
        ...,
        description="List of social account IDs to post to",
        min_items=1
    )
    content: str = Field(
        ...,
        description="Post content/text",
        min_length=1,
        max_length=5000
    )
    media_urls: Optional[List[PublishMediaItem]] = Field(
        None,
        description="Optional list of media URLs to attach"
    )
    schedule_time: Optional[str] = Field(
        None,
        description="Optional ISO 8601 datetime for scheduled posts"
    )
    hashtags: Optional[List[str]] = Field(None, description="Optional hashtags")

    class Config:
        json_schema_extra = {
            "example": {
                "account_ids": ["sa_123456", "sa_789012"],
                "content": "Check out this amazing new feature! 🚀",
                "media_urls": [
                    {
                        "url": "https://example.com/image1.jpg",
                        "type": "image"
                    }
                ],
                "schedule_time": None,
                "hashtags": ["innovation", "tech"]
            }
        }


class PlatformPublishResult(BaseModel):
    """Result of publishing to a specific platform"""
    account_id: str
    platform: str
    success: bool
    post_id: Optional[str] = None
    error: Optional[str] = None
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "account_id": "sa_123456",
                "platform": "TWITTER",
                "success": True,
                "post_id": "blotato_post_123",
                "error": None,
                "message": "Posted successfully"
            }
        }


class PublishPostResponse(BaseModel):
    """Response after publishing post"""
    success: bool
    total_platforms: int
    successful_posts: int
    failed_posts: int
    results: List[PlatformPublishResult]
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "total_platforms": 2,
                "successful_posts": 2,
                "failed_posts": 0,
                "results": [
                    {
                        "account_id": "sa_123456",
                        "platform": "TWITTER",
                        "success": True,
                        "post_id": "blotato_post_123",
                        "error": None,
                        "message": "Posted successfully"
                    },
                    {
                        "account_id": "sa_789012",
                        "platform": "LINKEDIN",
                        "success": True,
                        "post_id": "blotato_post_124",
                        "error": None,
                        "message": "Posted successfully"
                    }
                ],
                "message": "All posts published successfully"
            }
        }


# Refresh/Status DTOs
class RefreshAccountsRequest(BaseModel):
    """Request to refresh connected accounts from Blotato"""
    platforms: Optional[List[str]] = Field(
        None,
        description="Optional list of platforms to refresh. If None, refreshes all."
    )

    class Config:
        json_schema_extra = {
            "example": {
                "platforms": None
            }
        }


class RefreshAccountsResponse(BaseModel):
    """Response from refreshing accounts"""
    success: bool
    refreshed_count: int
    accounts: List[SocialAccountResponse]
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "refreshed_count": 1,
                "accounts": [],
                "message": "Accounts refreshed successfully"
            }
        }


# Error Response DTOs
class ErrorResponse(BaseModel):
    """Standard error response"""
    success: bool = False
    error: str
    detail: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "success": False,
                "error": "Invalid account ID",
                "detail": "The specified account does not belong to this user"
            }
        }


# Blotato API Response Models (internal)
class BlotatoAccountInfo(BaseModel):
    """Account info returned from Blotato"""
    account_id: str
    account_name: str
    platform: str
    platform_user_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expires_in: Optional[int] = None

    class Config:
        from_attributes = True


class BlotatoPostResponse(BaseModel):
    """Post response from Blotato"""
    success: bool
    post_id: Optional[str] = None
    platform: str
    account_id: str
    error: Optional[str] = None

    class Config:
        from_attributes = True


class BlotatoMediaUploadResponse(BaseModel):
    """Media upload response from Blotato"""
    success: bool
    media_id: Optional[str] = None
    media_url: Optional[str] = None
    error: Optional[str] = None

    class Config:
        from_attributes = True


# --- Digital marketing (Postiz) archived payloads ---


class DigitalMarketingAssetResponse(BaseModel):
    """Marketing text + media URLs saved after a successful Postiz post (list view)."""

    id: str
    agent_name: str
    marketing_text: Optional[str] = None
    content_links: List[str] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


class DigitalMarketingAssetDetailResponse(DigitalMarketingAssetResponse):
    """Same as list row plus archived chat (conversation + media) when present."""

    postiz_response: Optional[Dict[str, Any]] = None
    conversation: List[Dict[str, Any]] = Field(default_factory=list)
    contents: List[Dict[str, Any]] = Field(default_factory=list)

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def _lift_chat_payload(cls, data: Any) -> Any:
        payload = None
        if isinstance(data, dict):
            payload = data.get("postiz_response")
            out = dict(data)
        else:
            payload = getattr(data, "postiz_response", None)
            out = {
                "id": getattr(data, "id", None),
                "agent_name": getattr(data, "agent_name", None),
                "marketing_text": getattr(data, "marketing_text", None),
                "content_links": getattr(data, "content_links", None) or [],
                "created_at": getattr(data, "created_at", None),
                "postiz_response": payload,
            }
        chat = payload if isinstance(payload, dict) else {}
        if "conversation" not in out or out.get("conversation") is None:
            conv = chat.get("conversation")
            out["conversation"] = conv if isinstance(conv, list) else []
        if "contents" not in out or out.get("contents") is None:
            contents = chat.get("contents")
            out["contents"] = contents if isinstance(contents, list) else []
        return out


class DigitalMarketingAssetListResponse(BaseModel):
    items: List[DigitalMarketingAssetResponse]
    total: int


class DigitalMarketingAssetCreate(BaseModel):
    """Archive a chat campaign (conversation + media) without posting to Postiz."""

    marketing_text: Optional[str] = None
    content_links: List[str] = Field(default_factory=list)
    conversation: Optional[List[Dict[str, Any]]] = None
    contents: Optional[List[Dict[str, Any]]] = None
    agent_name: str = "digital_marketing"
