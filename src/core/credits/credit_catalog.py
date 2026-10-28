"""Unified credit wallet catalog.

1 Autobus credit = $0.20 USD list value so video generation is 4 credits
at the $0.80 customer charge. Feature costs follow the internal rate card.

iOS sells these packs as App Store consumables. Android and web sell them via
Paystack. Catalog prices are USD so both platforms quote the same dollar
amount; Paystack charges the live GHS equivalent at checkout.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# List value used to convert USD charges into credits.
CREDIT_USD_VALUE = 0.20

# New accounts receive this so App Review and first-run users can try features.
# Admin grant settings override this once the pricing table exists.
STARTER_CREDIT_GRANT = 15.0
DEFAULT_REFERRAL_REFERRER_CREDITS = 5.0
DEFAULT_REFERRAL_REFEREE_CREDITS = 5.0

# Credits deducted per 1.0 unit of the feature type.
# storage_mb amount is megabytes uploaded; server requests are not metered.
# Live values come from credit_pricing_rules. These are the seed defaults.
FEATURE_CREDIT_COSTS: Dict[str, float] = {
    "llm": 0.05,  # $0.01 per chat
    "image_gen": 1.25,  # $0.25 per image
    "video_gen": 4.0,  # $0.80 per video
    "email": 0.2,  # $0.04 per email
    "sms": 0.01,  # $0.002 per SMS
    "storage_mb": 0.02,  # 50 MB ≈ 1 credit ≈ $0.20
    "media_save": 0.25,  # $0.05 per saved file
    "social_post": 0.15,  # $0.03 per destination account
    "video_concat": 0.5,  # $0.10 per assembly
    "server": 0.0,
}

# verb shown to the user, noun, unit name.
CREDIT_ACTIVITIES: Dict[str, tuple] = {
    "llm": ("chat with AI", "An AI chat", "chat"),
    "image_gen": ("generate this image", "Image generation", "image"),
    "video_gen": ("generate this video", "Video generation", "video"),
    "email": ("send this email", "Email", "email"),
    "sms": ("send this SMS", "SMS", "message"),
    "storage_mb": ("upload this file", "File storage", "MB"),
    "media_save": ("save this media", "Saving media", "save"),
    "social_post": ("publish this post", "Social posting", "account"),
    "video_concat": ("assemble this video", "Video assembly", "assembly"),
    "server": ("complete this request", "A server request", "request"),
}

# Admin-editable pricing groups. price_usd is the list price used to monitor margin.
DEFAULT_PRICING_GROUPS: List[Dict[str, Any]] = [
    {
        "group_key": "llm",
        "label": "AI chats",
        "description": "Each assistant, copilot, agent, workflow, or customer-channel language-model call.",
        "unit_label": "chat",
        "credits_per_unit": 0.05,
        "price_usd": 0.01,
        "sort_order": 10,
    },
    {
        "group_key": "image_gen",
        "label": "Image generation",
        "description": "Each generated image, including an extra charge for each reference image.",
        "unit_label": "image",
        "credits_per_unit": 1.25,
        "price_usd": 0.25,
        "sort_order": 20,
    },
    {
        "group_key": "video_gen",
        "label": "Video generation",
        "description": "Each generated video, including an extra charge for each video reference.",
        "unit_label": "video",
        "credits_per_unit": 4.0,
        "price_usd": 0.80,
        "sort_order": 30,
    },
    {
        "group_key": "email",
        "label": "Email",
        "description": "Each outbound customer email.",
        "unit_label": "email",
        "credits_per_unit": 0.2,
        "price_usd": 0.04,
        "sort_order": 40,
    },
    {
        "group_key": "sms",
        "label": "SMS",
        "description": "Each outbound SMS.",
        "unit_label": "message",
        "credits_per_unit": 0.01,
        "price_usd": 0.002,
        "sort_order": 50,
    },
    {
        "group_key": "storage_mb",
        "label": "File storage",
        "description": "Each megabyte uploaded to storage.",
        "unit_label": "MB",
        "credits_per_unit": 0.02,
        "price_usd": 0.004,
        "sort_order": 60,
    },
    {
        "group_key": "media_save",
        "label": "Saving media",
        "description": "Each new image, video, character, scene, or campaign file saved to the library.",
        "unit_label": "save",
        "credits_per_unit": 0.25,
        "price_usd": 0.05,
        "sort_order": 70,
    },
    {
        "group_key": "social_post",
        "label": "Social posting",
        "description": "Each social account a post is published or scheduled to.",
        "unit_label": "account",
        "credits_per_unit": 0.15,
        "price_usd": 0.03,
        "sort_order": 80,
    },
    {
        "group_key": "video_concat",
        "label": "Video assembly",
        "description": "Stitching scene clips into one video.",
        "unit_label": "assembly",
        "credits_per_unit": 0.5,
        "price_usd": 0.10,
        "sort_order": 90,
    },
    {
        "group_key": "server",
        "label": "Server requests",
        "description": "Reserved. Ordinary reads stay free. This number is not charged on general API traffic.",
        "unit_label": "request",
        "credits_per_unit": 0.0,
        "price_usd": 0.0,
        "sort_order": 100,
    },
]

CREDIT_PACKS: List[Dict[str, Any]] = [
    {
        "id": "starter",
        "name": "Starter",
        "credits": 20.0,
        "price_usd": 4.99,
        "apple_product_id": "autobus.credits.20.v4",
        "google_play_product_id": "autobus.credits.20",
        "description": "20 credits",
        "details": "Add 20 credits to your wallet. That's about 5 videos or 16 images, plus AI chats, email and SMS. Credits stay until you use them.",
    },
    {
        "id": "plus",
        "name": "Plus",
        "credits": 50.0,
        "price_usd": 12.99,
        "apple_product_id": "autobus.credits.50.v4",
        "google_play_product_id": "autobus.credits.50",
        "description": "50 credits",
        "details": "Add 50 credits for weekly marketing. Roughly 12 videos or 40 images, with plenty left for customer chats and messages.",
    },
    {
        "id": "pro",
        "name": "Pro",
        "credits": 150.0,
        "price_usd": 34.99,
        "apple_product_id": "autobus.credits.150.v5",
        "google_play_product_id": "autobus.credits.150",
        "description": "150 credits",
        "details": "Add 150 credits for campaigns and a busy inbox. About 37 videos or 120 images, or a mix of generation, chats and messaging.",
    },
    {
        "id": "business",
        "name": "Business",
        "credits": 400.0,
        "price_usd": 79.99,
        "apple_product_id": "autobus.credits.400.v4",
        "google_play_product_id": "autobus.credits.400",
        "description": "400 credits",
        "details": "Add 400 credits at the best rate per credit. Built for daily use: videos, product photos, AI chats, email and SMS.",
    },
]

WALLET_CREDIT_TYPE = "wallet"


def feature_cost(
    credit_type: str,
    amount: float = 1.0,
    costs: Optional[Dict[str, float]] = None,
) -> float:
    table = costs if costs is not None else FEATURE_CREDIT_COSTS
    if credit_type in table:
        unit = float(table[credit_type])
    else:
        unit = float(FEATURE_CREDIT_COSTS.get(credit_type, 1.0))
    return max(0.0, unit * float(amount))


def format_credits(value: float) -> str:
    number = round(float(value), 4)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def get_pack(pack_id: str) -> Optional[Dict[str, Any]]:
    key = (pack_id or "").strip().lower()
    for pack in CREDIT_PACKS:
        if pack["id"] == key:
            return pack
    return None


def get_pack_by_apple_product(product_id: str) -> Optional[Dict[str, Any]]:
    return get_pack_by_store_product(product_id)


def get_pack_by_store_product(product_id: str) -> Optional[Dict[str, Any]]:
    pid = (product_id or "").strip()
    for pack in CREDIT_PACKS:
        if pack["apple_product_id"] == pid or pack.get("google_play_product_id") == pid:
            return pack
    return None


def apple_product_ids() -> List[str]:
    return [str(p["apple_product_id"]) for p in CREDIT_PACKS]


def store_product_ids() -> List[str]:
    ids: List[str] = []
    for pack in CREDIT_PACKS:
        for key in ("apple_product_id", "google_play_product_id"):
            value = str(pack.get(key) or "")
            if value and value not in ids:
                ids.append(value)
    return ids


def packs_public() -> List[Dict[str, Any]]:
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "credits": p["credits"],
            "price_usd": p["price_usd"],
            "paystack_amount": 0,
            "apple_product_id": p["apple_product_id"],
            "google_play_product_id": p.get("google_play_product_id") or p["apple_product_id"],
            "description": p["description"],
            "details": p.get("details") or p["description"],
        }
        for p in CREDIT_PACKS
    ]
