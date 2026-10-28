"""Credit resource types. Wallet is the single spendable balance."""

from enum import Enum
from typing import Dict


class CreditType(str, Enum):
    WALLET = "wallet"
    LLM = "llm"
    IMAGE_GEN = "image_gen"
    VIDEO_GEN = "video_gen"
    EMAIL = "email"
    SMS = "sms"
    STORAGE_MB = "storage_mb"
    MEDIA_SAVE = "media_save"
    SOCIAL_POST = "social_post"
    VIDEO_CONCAT = "video_concat"
    SERVER = "server"


CREDIT_TYPE_LABELS: Dict[str, str] = {
    CreditType.WALLET.value: "Credits",
    CreditType.LLM.value: "LLM Chats",
    CreditType.IMAGE_GEN.value: "Image Gen",
    CreditType.VIDEO_GEN.value: "Video Gen",
    CreditType.EMAIL.value: "Email",
    CreditType.SMS.value: "SMS",
    CreditType.STORAGE_MB.value: "Storage (MB)",
    CreditType.MEDIA_SAVE.value: "Saving Media",
    CreditType.SOCIAL_POST.value: "Social Posts",
    CreditType.VIDEO_CONCAT.value: "Video Assembly",
    CreditType.SERVER.value: "Server Requests",
}

# Kept for existing-plan migration only. New accounts use the unified wallet.
PLAN_CREDIT_DEFAULTS: Dict[str, Dict[str, float]] = {
    "free": {
        CreditType.LLM.value: 25,
        CreditType.IMAGE_GEN.value: 3,
        CreditType.VIDEO_GEN.value: 1,
        CreditType.EMAIL.value: 5,
        CreditType.SMS.value: 5,
        CreditType.STORAGE_MB.value: 250,
        CreditType.SERVER.value: 1000,
    },
}

ALL_CREDIT_TYPES = [ct.value for ct in CreditType if ct != CreditType.WALLET]
