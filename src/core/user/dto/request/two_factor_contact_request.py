from typing import Literal

from pydantic import BaseModel, Field


class TwoFactorContactRequest(BaseModel):
    channel: Literal["phone", "email"]
    value: str = Field(..., min_length=1, max_length=255)
    otp: str = Field(default="", max_length=8)
