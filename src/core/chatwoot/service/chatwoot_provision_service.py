import asyncio
import logging
import os
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from core.chatwoot.model.ChatwootAccount import ChatwootAccount
from core.chatwoot.service.chatwoot_api_service import (
    ChatwootAPIError,
    ChatwootClient,
    chatwoot_enabled,
    derive_chatwoot_password,
)
from core.chatwoot.service.chatwoot_org_service import ChatwootOrgService
from core.user.model.User import User
from utilities.crypto import encrypt_secret

logger = logging.getLogger(__name__)


async def ensure_chatwoot_provisioned(db: Session, user_id: str) -> Optional[ChatwootAccount]:
    """
    Ensure the user has a Chatwoot workspace mapping (mirrors Postiz `_ensure_postiz_api_key`).

    Called on first Chatwoot access. Inbox replies are charged from the credit wallet,
    so every account gets a workspace without a subscription.
    """
    org = ChatwootOrgService(db)
    existing = org.get_for_user(user_id)
    if existing:
        return existing

    if not chatwoot_enabled():
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.email:
        return None

    display_name = (
        getattr(user, "fullname", None) or getattr(user, "name", None) or user.email or ""
    ).strip()
    account_name = (
        (user.company or user.organization_workplace or display_name or "Autobus Client").strip()
        or "Autobus Client"
    )
    base_url = os.getenv("CHATWOOT_BASE_URL", "").strip()
    token = os.getenv("CHATWOOT_PLATFORM_API_TOKEN", "").strip()
    chatwoot_password = derive_chatwoot_password(username=display_name or user.email)
    client = ChatwootClient(base_url=base_url, platform_api_token=token)

    try:
        logger.info("[CHATWOOT] Lazy provisioning workspace for user %s", user_id)
        cw_account_id, cw_user_id, cw_access_token = await client.provision_account_and_user(
            account_name=account_name,
            email=user.email,
            name=display_name or user.email,
            password=chatwoot_password,
            support_email=user.email,
        )
    except ChatwootAPIError as e:
        logger.warning("[CHATWOOT] Lazy provisioning failed for user %s: %s", user_id, e)
        return None
    except Exception as e:
        # e.g. httpx.ConnectError when Chatwoot FORCE_SSL redirects http→https inside Docker
        logger.warning("[CHATWOOT] Lazy provisioning error for user %s: %s", user_id, e)
        return None

    mapping = ChatwootAccount(
        id=f"cw_{str(uuid.uuid4())[:12]}",
        user_id=user_id,
        chatwoot_account_id=int(cw_account_id),
        chatwoot_user_id=int(cw_user_id),
        chatwoot_user_access_token_encrypted=encrypt_secret(cw_access_token) or cw_access_token,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping
