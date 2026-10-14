from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class BusinessActionRequest(BaseModel):
    """A workflow step that runs as one merchant."""

    model_config = ConfigDict(populate_by_name=True)

    business_user_id: str = Field(alias="businessUserId")

    def resolved_business_user_id(self) -> str:
        return (self.business_user_id or "").strip()


class IntelligenceActionRequest(BusinessActionRequest):
    message: str = ""


class CustomerServiceActionRequest(BusinessActionRequest):
    message: str = ""


class MarketingContentActionRequest(BusinessActionRequest):
    prompt: str = ""
    message: str = ""

    def resolved_prompt(self) -> str:
        return (self.prompt or self.message or "").strip()


class SocialPostActionRequest(BusinessActionRequest):
    content: str = ""
    integration_ids: List[str] = Field(default_factory=list, alias="integrationIds")
    media_urls: List[str] = Field(default_factory=list, alias="mediaUrls")
    publish_now: bool = Field(default=True, alias="publishNow")
    scheduled_at: Optional[str] = Field(default=None, alias="scheduledAt")
    tiktok_privacy: Optional[str] = Field(default=None, alias="tiktokPrivacy")

    def resolved_integration_ids(self) -> List[str]:
        return [str(item).strip() for item in self.integration_ids if str(item).strip()]

    def resolved_media_urls(self) -> List[str]:
        return [str(item).strip() for item in self.media_urls if str(item).strip()]

    def resolved_publish_now(self) -> bool:
        return bool(self.publish_now)

    def resolved_scheduled_at(self) -> Optional[str]:
        value = (self.scheduled_at or "").strip()
        return value or None

    def resolved_tiktok_privacy(self) -> str:
        return (self.tiktok_privacy or "").strip() or "PUBLIC_TO_EVERYONE"
