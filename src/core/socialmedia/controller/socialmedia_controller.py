"""
Social Media Controller
API routes for social media account management and posting
"""

import asyncio
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from another_fastapi_jwt_auth import AuthJWT
from core.socialmedia.dto.socialmedia_dto import (
    SocialAccountResponse, SocialAccountsListResponse, DisconnectAccountRequest,
    PublishPostRequest, PublishPostResponse, RefreshAccountsRequest,
    RefreshAccountsResponse, OAuth2CallbackRequest, ErrorResponse,
    SocialPlatformEnum,
    DigitalMarketingAssetListResponse,
    DigitalMarketingAssetResponse,
    DigitalMarketingAssetDetailResponse,
    DigitalMarketingAssetCreate,
)
from core.socialmedia.service.socialmedia_service import SocialMediaService
from core.socialmedia.service.post_publishing_service import PostPublishingService
from core.socialmedia.service.blotato_api_service import (
    BlotatoAPIClient, BlotatoOAuthManager
)
from core.socialmedia.service.postiz_api_service import (
    PostizClient,
    PostizAPIError,
    apply_facebook_login_config_id,
    apply_tiktok_oauth_scopes,
    coerce_tiktok_privacy_for_unaudited_app,
    derive_postiz_password,
    normalize_postiz_integrations_list,
)
from core.socialmedia.service.tiktok_direct_post import (
    extract_publish_ids,
    load_tiktok_creator_info,
    load_tiktok_publish_status,
)
from core.socialmedia.service.facebook_page_review import (
    FacebookPageReviewError,
    load_facebook_page_activity,
)
from core.socialmedia.service.postiz_marketing_extract import (
    extract_marketing_text_and_links,
    normalize_digital_marketing_agent_name,
)
from core.socialmedia.service.digital_marketing_asset_service import DigitalMarketingAssetService
from core.socialmedia.service.postiz_org_service import PostizOrgService
from core.socialmedia.service.instagram_media_prepare import (
    InstagramMediaPrepareError,
    InstagramMediaPrepareService,
    parse_http_byte_range,
    payload_has_attached_media,
)
from core.socialmedia.model.PostizOrganization import PostizOrganization
from core.cloudstorage.service.storageservice import StorageFolder, StorageService
from core.chatwoot.controller.chatwoot_controller import resolve_internal_user_id
from core.user.model.User import User
from utilities.crypto import encrypt_secret
from utilities.dbconfig import get_db

logger = logging.getLogger(__name__)

# Initialize router
social_routes = APIRouter()

# Initialize Blotato API Client (with environment variables)
BLOTATO_API_KEY = os.getenv("BLOTATO_API_KEY", "")
BLOTATO_CLIENT_ID = os.getenv("BLOTATO_CLIENT_ID", "")
BLOTATO_CLIENT_SECRET = os.getenv("BLOTATO_CLIENT_SECRET", "")

if not all([BLOTATO_API_KEY, BLOTATO_CLIENT_ID, BLOTATO_CLIENT_SECRET]):
    logger.warning("[SOCIAL] Blotato credentials not fully configured in environment variables")

blotato_client = BlotatoAPIClient(
    api_key=BLOTATO_API_KEY,
    client_id=BLOTATO_CLIENT_ID,
    client_secret=BLOTATO_CLIENT_SECRET
)

def _resolve_postiz_api_key(user_id: str, db: Session) -> Optional[str]:
    """
    Resolve Postiz Public API key for proxy routes.
    Priority:
      1) user-specific key stored in postiz_organizations
      2) global fallback key from env (for manual Postiz setup)
    """
    user_scoped_key = PostizOrgService(db).get_public_api_key_for_user(user_id)
    if user_scoped_key:
        return user_scoped_key

    return (
        os.getenv("POSTIZ_PUBLIC_API_KEY", "").strip()
        or os.getenv("POSTIZ_GLOBAL_PUBLIC_API_KEY", "").strip()
        or None
    )


async def _ensure_postiz_api_key(user_id: str, db: Session) -> Optional[str]:
    """
    Ensure the user has a Postiz org mapping + public API key.
    Returns API key when available.
    """
    existing_key = _resolve_postiz_api_key(user_id, db)
    if existing_key:
        return existing_key

    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None

    company_name = (user.company or _postiz_username_for_user(user) or "Autobus Client").strip()
    postiz_password = derive_postiz_password(username=_postiz_username_for_user(user))
    client = PostizClient(base_url=postiz_base_url)
    postiz_org_id, postiz_api_key = await client.provision_org_and_get_public_api_key(
        email=user.email,
        company=company_name,
        password=postiz_password,
    )

    mapping = PostizOrganization(
        id=f"po_{str(uuid.uuid4())[:12]}",
        user_id=user.id,
        postiz_org_id=postiz_org_id,
        postiz_public_api_key_encrypted=encrypt_secret(postiz_api_key) or postiz_api_key,
    )
    db.add(mapping)
    db.commit()
    return postiz_api_key


# Dependency for token validation
def validate_token(authjwt: AuthJWT = Depends()) -> str:
    """Validate JWT and return JWT subject (email at sign-in; may be internal id)."""
    try:
        authjwt.jwt_required()
        return authjwt.get_jwt_subject()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )


def _postiz_username_for_user(user: User) -> str:
    """Stable identifier for Postiz LOCAL password derivation (User.fullname)."""
    for attr in ("fullname", "uname", "username"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    email = getattr(user, "email", None)
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0].strip() or user.id
    return str(user.id)


def _get_user_for_jwt_subject(db: Session, jwt_subject: str) -> User:
    """Resolve Autobus user from JWT `sub` (email or internal id)."""
    user = db.query(User).filter(User.email == jwt_subject).first()
    if not user:
        user = db.query(User).filter(User.id == jwt_subject).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# Postiz Public API `GET /api/public/v1/social/{slug}` (direct provider OAuth).
_POSTIZ_OAUTH_SLUG_BY_PLATFORM: Dict[str, str] = {
    "FACEBOOK": "facebook",
    "INSTAGRAM": "instagram",
    "WHATSAPP": "whatsapp",
    "TIKTOK": "tiktok",
    "YOUTUBE": "youtube",
}

_CONNECT_PATH_TO_PLATFORM: Dict[str, str] = {
    "facebook": "FACEBOOK",
    "instagram": "INSTAGRAM",
    "instagram-standalone": "INSTAGRAM",
    "whatsapp": "WHATSAPP",
    "whatsapp-status": "WHATSAPP",
    "tiktok": "TIKTOK",
    "youtube": "YOUTUBE",
}

# If the primary Postiz social slug fails, try these (Instagram Graph vs Business Login).
_POSTIZ_OAUTH_SLUG_FALLBACKS: Dict[str, List[str]] = {
    "instagram": ["instagram-standalone"],
    "instagram-standalone": ["instagram"],
}


def _resolve_connect_platform(platform: str) -> tuple[str, Optional[str]]:
    """Map URL path segment to (PLATFORM_UPPER, postiz_oauth_slug)."""
    path_key = platform.strip().lower().replace(" ", "-").replace("_", "-")
    platform_upper = _CONNECT_PATH_TO_PLATFORM.get(path_key) or platform.strip().upper().replace(
        " ", "_"
    )
    if path_key == "instagram-standalone":
        postiz_slug = "instagram-standalone"
    elif path_key == "whatsapp-status":
        postiz_slug = "whatsapp"
    else:
        postiz_slug = _POSTIZ_OAUTH_SLUG_BY_PLATFORM.get(platform_upper)
    return platform_upper, postiz_slug


async def _build_postiz_platform_connect(
    *,
    internal_user_id: str,
    db: Session,
    platform_upper: str,
    postiz_slug: str,
) -> Dict[str, Any]:
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    try:
        api_key = await _ensure_postiz_api_key(internal_user_id, db)
    except Exception as postiz_error:
        logger.warning(
            f"[SOCIAL] Postiz provisioning failed for user {internal_user_id}: {postiz_error}"
        )
        api_key = _resolve_postiz_api_key(internal_user_id, db)

    browser_postiz_url = (os.getenv("POSTIZ_PUBLIC_URL", "").strip() or postiz_base_url).rstrip(
        "/"
    )
    # Guard against misconfigured public URLs that would open Chatwoot (or another app).
    if "chatwoot" in browser_postiz_url.lower():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "POSTIZ_PUBLIC_URL points at Chatwoot. Set it to your Postiz URL "
                "(e.g. https://postiz.useautobus.com)."
            ),
        )

    provider_label = postiz_slug.replace("-", " ").title()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No Postiz workspace for this account yet. "
                f"Subscribe or provision Postiz, then link {provider_label}."
            ),
        )

    slugs_to_try: List[str] = [postiz_slug]
    for extra in _POSTIZ_OAUTH_SLUG_FALLBACKS.get(postiz_slug, []):
        if extra not in slugs_to_try:
            slugs_to_try.append(extra)

    authorization_url: Optional[str] = None
    last_oauth_error: Optional[Exception] = None
    used_slug = postiz_slug
    postiz_client = PostizClient(base_url=postiz_base_url)
    for slug in slugs_to_try:
        try:
            authorization_url = await postiz_client.get_social_connect_url(api_key, slug)
            used_slug = slug
            break
        except Exception as oauth_error:
            last_oauth_error = oauth_error
            logger.warning(
                f"[SOCIAL] Postiz direct OAuth for {slug} failed "
                f"(user {internal_user_id}): {oauth_error}"
            )

    if not authorization_url:
        redirect_hints = ", ".join(
            f"https://postiz.useautobus.com/integrations/social/{slug}"
            for slug in slugs_to_try
        )
        timeout_hint = (
            last_oauth_error is not None
            and "timed out" in str(last_oauth_error).lower()
        )
        if timeout_hint:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"{provider_label} connect is busy right now. "
                    "Wait a few seconds and try again."
                ),
            ) from last_oauth_error
        cred_hint = (
            "Meta/Instagram"
            if used_slug.startswith("instagram") or postiz_slug.startswith("instagram")
            else used_slug.upper()
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Could not start {provider_label} OAuth. "
                f"Configure {cred_hint} client credentials on Postiz "
                f"(redirect URI {redirect_hints}) "
                f"and restart Postiz. Details: {last_oauth_error}"
            ),
        ) from last_oauth_error

    authorization_url = apply_facebook_login_config_id(
        authorization_url,
        slug=used_slug,
    )
    authorization_url = apply_tiktok_oauth_scopes(
        authorization_url,
        slug=used_slug,
    )
    if used_slug.strip().lower() == "facebook":
        logger.info("[SOCIAL] Facebook Login for Business URL ready (config_id applied)")

    provider_label = used_slug.replace("-", " ").title()

    # Optional Postiz LOCAL login payload (not used for direct provider OAuth).
    postiz_login_ready = False
    postiz_login_payload: Dict[str, Any] = {}
    user = db.query(User).filter(User.id == internal_user_id).first()
    if user and user.email:
        postiz_password = derive_postiz_password(username=_postiz_username_for_user(user))
        try:
            await PostizClient(base_url=postiz_base_url).login_local(
                email=user.email,
                password=postiz_password,
                timeout_s=2.0,
            )
            postiz_login_ready = True
            postiz_login_payload = {
                "login_page_url": f"{browser_postiz_url}/auth",
                "body": {
                    "email": user.email,
                    "password": postiz_password,
                    "providerToken": "",
                    "provider": "LOCAL",
                },
            }
        except Exception as login_error:
            logger.warning(
                f"[SOCIAL] Postiz auto-login failed for user {internal_user_id}: {login_error}"
            )

    return {
        "authorization_url": authorization_url,
        "platform": platform_upper,
        "provider": "POSTIZ",
        "postiz_ready": True,
        "postiz_login_ready": postiz_login_ready,
        "postiz_login": postiz_login_payload,
        "direct_oauth": True,
        "message": f"Redirect the user to authorize {provider_label} via Postiz.",
    }


def _instagram_business_login_connect(
    internal_user_id: str,
    return_to: str = "web",
) -> Dict[str, Any]:
    """Instagram inbox + Digital Marketing posting use Autobus Business Login, not Postiz."""
    from core.instagram.service.instagram_oauth_service import (
        InstagramOAuthService,
        InstagramOAuthState,
    )

    svc = InstagramOAuthService()
    try:
        svc.require_config()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    state = InstagramOAuthState.create(internal_user_id, return_to=return_to)
    return {
        "authorization_url": svc.build_authorize_url(state),
        "platform": "INSTAGRAM",
        "provider": "INSTAGRAM",
        "direct_oauth": True,
        "state": state,
        "redirect_uri": svc.redirect_uri,
        "message": (
            "Open authorization_url to link Instagram via Business Login. "
            "This enables inbox messaging and Digital Marketing posting."
        ),
    }


# ==================== OAuth Flow Routes ====================

@social_routes.get("/connect/{platform}")
async def initiate_oauth_flow(
    platform: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    return_to: str = Query(
        "web",
        description="After OAuth, send users to the mobile app (`app`) or website (`web`). Instagram only.",
    ),
):
    """
    Initiate OAuth flow for connecting a social media account
    
    Args:
        platform: Social media platform (twitter, linkedin, facebook, instagram, tiktok, etc.)
        user_id: Authenticated user ID
        
    Returns:
        Redirect URL to Blotato OAuth endpoint
    """
    try:
        internal_user_id = resolve_internal_user_id(db, jwt_subject)

        platform_upper, postiz_slug = _resolve_connect_platform(platform)
        valid_platforms = [p.value for p in SocialPlatformEnum]
        if platform_upper not in valid_platforms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported platform. Supported: {', '.join(valid_platforms)}",
            )

        if platform_upper == "INSTAGRAM" or (postiz_slug or "").startswith("instagram"):
            return _instagram_business_login_connect(
                internal_user_id,
                return_to=return_to,
            )

        postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
        if postiz_slug and postiz_base_url:
            return await _build_postiz_platform_connect(
                internal_user_id=internal_user_id,
                db=db,
                platform_upper=platform_upper,
                postiz_slug=postiz_slug,
            )

        # Legacy Blotato OAuth flow
        # Create OAuth state for CSRF protection
        state = BlotatoOAuthManager.create_state(internal_user_id, platform_upper)
        
        # Generate OAuth URL
        callback_url = f"{os.getenv('BASE_FRONTEND_URL', 'http://localhost:3000')}/api/social/callback"
        auth_url, _ = await blotato_client.generate_oauth_url(
            redirect_uri=callback_url,
            state=state
        )
        
        logger.info(f"[SOCIAL] OAuth flow initiated for user {internal_user_id}, platform {platform_upper}")
        
        return {
            "authorization_url": auth_url,
            "platform": platform_upper,
            "message": "Redirect user to this URL to authorize account connection"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SOCIAL] Error initiating OAuth: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error initiating OAuth: {str(e)}"
        )


@social_routes.get("/callback")
async def oauth_callback(
    code: str,
    state: str,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Handle OAuth callback from Blotato
    
    Args:
        code: Authorization code from Blotato
        state: State parameter for CSRF validation
        error: Error code if user denied authorization
        error_description: Error description
        
    Returns:
        Account connection status
    """
    try:
        # Check for errors
        if error:
            logger.warning(f"[SOCIAL] OAuth error: {error} - {error_description}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Authorization failed: {error_description or error}"
            )
        
        # Validate state
        state_data = BlotatoOAuthManager.validate_state(state)
        if not state_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired state parameter"
            )
        
        user_id = state_data["user_id"]
        platform = state_data["platform"]
        
        # Exchange code for account info
        callback_url = f"{os.getenv('BASE_FRONTEND_URL', 'http://localhost:3000')}/api/social/callback"
        token_data = await blotato_client.exchange_auth_code(code, callback_url)
        
        if not token_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to exchange authorization code"
            )
        
        # Get accounts from Blotato
        access_token = token_data.get("access_token")
        accounts = await blotato_client.get_accounts(access_token)
        
        if not accounts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve accounts from Blotato"
            )
        
        # Find account for this platform
        platform_account = next(
            (acc for acc in accounts if acc.get("platform", "").upper() == platform),
            None
        )
        
        if not platform_account:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No {platform} account found in Blotato"
            )
        
        # Store account in database
        social_service = SocialMediaService(db, blotato_client)
        success, account_obj, message = await social_service.connect_account(
            user_id=user_id,
            platform=platform,
            blotato_account_info=platform_account
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=message
            )
        
        logger.info(f"[SOCIAL] OAuth callback successful: {user_id} - {platform}")
        
        return {
            "success": True,
            "message": f"Account connected successfully",
            "platform": platform,
            "account_name": account_obj.account_name if account_obj else None,
            "redirect_url": f"{os.getenv('BASE_FRONTEND_URL', 'http://localhost:3000')}/social/accounts?connected=true"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SOCIAL] Error in OAuth callback: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Callback processing error: {str(e)}"
        )


# ==================== Account Management Routes ====================

@social_routes.get("/accounts", response_model=SocialAccountsListResponse)
async def get_user_accounts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """
    Get all connected social media accounts for the user
    
    Returns:
        List of connected social accounts
    """
    try:
        internal_user_id = resolve_internal_user_id(db, jwt_subject)
        social_service = SocialMediaService(db, blotato_client)
        accounts = social_service.get_user_accounts(internal_user_id)
        
        logger.info(f"[SOCIAL] Retrieved {len(accounts)} accounts for user {internal_user_id}")
        
        account_responses = [
            SocialAccountResponse.from_orm(acc) for acc in accounts
        ]
        
        return SocialAccountsListResponse(
            accounts=account_responses,
            total=len(account_responses)
        )
        
    except Exception as e:
        logger.error(f"[SOCIAL] Error getting accounts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving accounts: {str(e)}"
        )


@social_routes.delete("/accounts/{account_id}")
async def disconnect_account(
    account_id: str,
    request: DisconnectAccountRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """
    Disconnect a social media account
    
    Args:
        account_id: ID of account to disconnect
        request: Request body with disconnect options
        user_id: Authenticated user ID
        
    Returns:
        Disconnection status
    """
    try:
        internal_user_id = resolve_internal_user_id(db, jwt_subject)
        social_service = SocialMediaService(db, blotato_client)
        success, message = await social_service.disconnect_account(account_id, internal_user_id)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message
            )
        
        logger.info(f"[SOCIAL] Account disconnected: {account_id} by user {internal_user_id}")
        
        return {
            "success": True,
            "message": message,
            "account_id": account_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SOCIAL] Error disconnecting account: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error disconnecting account: {str(e)}"
        )


@social_routes.post("/refresh", response_model=RefreshAccountsResponse)
async def refresh_accounts(
    request: RefreshAccountsRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """
    Refresh user's connected accounts from Blotato
    
    Args:
        request: Refresh request with optional platform filter
        user_id: Authenticated user ID
        
    Returns:
        Updated list of connected accounts
    """
    try:
        internal_user_id = resolve_internal_user_id(db, jwt_subject)
        # Get user's existing accounts to retrieve access token
        social_service = SocialMediaService(db, blotato_client)
        user_accounts = social_service.get_user_accounts(internal_user_id)
        
        # Get access token from first account (or find from Blotato)
        # In production, store access token securely for the user
        if not user_accounts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No connected accounts found. Please connect an account first."
            )
        
        # Use first account's token as fallback
        access_token = user_accounts[0].access_token
        
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No valid access token found. Please reconnect your accounts."
            )
        
        # Refresh accounts
        success, accounts, message = await social_service.refresh_accounts(
            user_id=internal_user_id,
            access_token=access_token,
            platforms=request.platforms
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=message
            )
        
        logger.info(f"[SOCIAL] Refreshed accounts for user {internal_user_id}: {message}")
        
        account_responses = [
            SocialAccountResponse.from_orm(acc) for acc in accounts
        ]
        
        return RefreshAccountsResponse(
            success=True,
            refreshed_count=len(account_responses),
            accounts=account_responses,
            message=message
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SOCIAL] Error refreshing accounts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error refreshing accounts: {str(e)}"
        )


# ==================== Post Publishing Routes ====================

@social_routes.post("/post", response_model=PublishPostResponse)
async def publish_post(
    request: PublishPostRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """
    Publish content to one or more connected social media accounts
    
    Args:
        request: Post content and target accounts
        user_id: Authenticated user ID
        
    Returns:
        Publishing results for each platform
    """
    try:
        internal_user_id = resolve_internal_user_id(db, jwt_subject)
        # Get user's access token for media uploads
        social_service = SocialMediaService(db, blotato_client)
        user_accounts = social_service.get_user_accounts(internal_user_id)
        
        access_token = None
        if user_accounts:
            access_token = user_accounts[0].access_token
        
        # Publish post
        publishing_service = PostPublishingService(db, blotato_client)
        response = await publishing_service.publish_post(
            user_id=internal_user_id,
            publish_request=request,
            access_token=access_token
        )
        
        logger.info(
            f"[SOCIAL] Post published by user {internal_user_id}: "
            f"{response.successful_posts}/{response.total_platforms} successful"
        )
        
        return response
        
    except Exception as e:
        logger.error(f"[SOCIAL] Error publishing post: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error publishing post: {str(e)}"
        )


# ==================== Postiz Public API Proxy Routes ====================

@social_routes.get("/postiz/integrations")
async def postiz_list_integrations(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    List Postiz integrations (connected channels) for the current user-business.
    Uses a user-scoped Postiz API key if available, or a global fallback key from env.
    """
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    try:
        client = PostizClient(postiz_base_url)
        raw = await client.list_integrations(api_key)
        return normalize_postiz_integrations_list(raw)
    except PostizAPIError as e:
        if e.status_code == 504:
            logger.warning(
                "[SOCIAL] Postiz list integrations timed out for user %s",
                internal_user_id,
            )
            return []
        raise HTTPException(status_code=502, detail=str(e))


def _postiz_login_credentials(user: User) -> Tuple[str, str]:
    email = (getattr(user, "email", None) or "").strip()
    password = derive_postiz_password(username=_postiz_username_for_user(user))
    return email, password


@social_routes.get("/postiz/tiktok/creator-info")
async def postiz_tiktok_creator_info(
    integration_id: str = Query(..., min_length=1),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Latest TikTok creator_info for the Post to TikTok screen."""
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    user = _get_user_for_jwt_subject(db, jwt_subject)
    email, password = _postiz_login_credentials(user)
    try:
        client = PostizClient(postiz_base_url)
        return await load_tiktok_creator_info(
            client=client,
            api_key=api_key,
            integration_id=integration_id,
            postiz_email=email,
            postiz_password=password,
        )
    except PostizAPIError as e:
        status_code = e.status_code if e.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(e)) from e


@social_routes.get("/postiz/tiktok/publish-status")
async def postiz_tiktok_publish_status(
    integration_id: str = Query(..., min_length=1),
    publish_id: Optional[str] = Query(None),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Poll TikTok / Postiz for Direct Post processing status."""
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    user = _get_user_for_jwt_subject(db, jwt_subject)
    email, password = _postiz_login_credentials(user)
    try:
        client = PostizClient(postiz_base_url)
        return await load_tiktok_publish_status(
            client=client,
            api_key=api_key,
            integration_id=integration_id,
            publish_id=(publish_id or "").strip(),
            postiz_email=email,
            postiz_password=password,
        )
    except PostizAPIError as e:
        status_code = e.status_code if e.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(e)) from e


@social_routes.get("/facebook/page-activity")
async def facebook_page_activity(
    integration_id: str = Query("", description="One Postiz Facebook integration. Empty loads every linked Page."),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Meta businesses and recent Page posts for the linked Facebook Page.

    Used by Marketing → Facebook Page so a person can see which business owns
    the Page and how people responded to recent posts.
    """
    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    org = PostizOrgService(db).get_for_user(internal_user_id)
    if not org or not (org.postiz_org_id or "").strip():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Postiz workspace for this account yet. Link Facebook Page from Marketing first.",
        )
    try:
        return await load_facebook_page_activity(org.postiz_org_id, integration_id.strip())
    except FacebookPageReviewError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc


@social_routes.get("/postiz/posts")
async def postiz_list_posts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """List recent Postiz posts so the app can show publish status."""
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (start_date or "").strip() or (
        (now - timedelta(days=14)).isoformat().replace("+00:00", "Z")
    )
    end = (end_date or "").strip() or (
        (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    )
    try:
        client = PostizClient(postiz_base_url)
        return await client.list_posts(api_key, start_date=start, end_date=end)
    except PostizAPIError as e:
        status_code = e.status_code if e.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(e)) from e


@social_routes.delete("/postiz/integrations/{integration_id}")
async def postiz_delete_integration(
    integration_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Unlink a Postiz channel for the current user-business.
    Proxies Postiz `DELETE /api/public/v1/integrations/{id}`.
    """
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    iid = (integration_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="integration_id is required")
    if iid.startswith("autobus-ig-"):
        raise HTTPException(
            status_code=400,
            detail="Use DELETE /api/v1/instagram/accounts/{id} for Autobus Instagram accounts.",
        )

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    try:
        client = PostizClient(postiz_base_url)
        result = await client.delete_integration(api_key, iid)
        logger.info(
            f"[SOCIAL] Postiz integration deleted: {iid} by user {internal_user_id}"
        )
        return {"status": "ok", "message": "Account unlinked", "result": result}
    except PostizAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@social_routes.post("/postiz/auto-login")
async def postiz_auto_login(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Build a Postiz LOCAL login payload for the current Autobus user and
    return the integrations URL so frontend can perform browser login + redirect.
    """
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    user = _get_user_for_jwt_subject(db, jwt_subject)
    if not user.email:
        raise HTTPException(status_code=404, detail="User/email not found")

    internal_user_id = user.id

    # Ensure org + API key exists for this user before auto-login redirect.
    try:
        _ = await _ensure_postiz_api_key(internal_user_id, db)
    except Exception as ensure_error:
        logger.warning(
            f"[SOCIAL] Postiz provisioning check failed for user {internal_user_id}: {ensure_error}"
        )

    browser_postiz_url = (os.getenv("POSTIZ_PUBLIC_URL", "").strip() or postiz_base_url).rstrip("/")
    postiz_password = derive_postiz_password(username=_postiz_username_for_user(user))

    login_payload = {
        "email": user.email,
        "password": postiz_password,
        "providerToken": "",
        "provider": "LOCAL",
    }

    # Optional server-side validation so caller can detect potential mismatch
    # (e.g. legacy users provisioned with old random password logic).
    postiz_login_ready = False
    try:
        await PostizClient(base_url=postiz_base_url).login_local(
            email=user.email,
            password=postiz_password,
        )
        postiz_login_ready = True
    except Exception as login_error:
        logger.warning(
            f"[SOCIAL] Postiz auto-login validation failed for user {internal_user_id}: {login_error}"
        )

    return {
        "postiz_login_ready": postiz_login_ready,
        "postiz_login": {
            "login_page_url": f"{browser_postiz_url}/auth",
            "body": login_payload,
        },
        "authorization_url": f"{browser_postiz_url}/integrations",
        "message": (
            "Open login_page_url in a WebView, sign in with your Postiz account, "
            "then navigate to authorization_url to link channels."
        ),
    }


_storage_service: Optional[StorageService] = None
_PUBLIC_MEDIA_FILE = re.compile(r"^[0-9a-fA-F-]{36}\.(jpg|mp4)$")


def _get_storage_service() -> StorageService:
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service


@social_routes.api_route("/media/public/{file_name}", methods=["GET", "HEAD"])
async def public_instagram_media(file_name: str, request: Request):
    """
    Unauthenticated JPEG/MP4 for Meta and Postiz.

    Filenames are unguessable UUIDs written only by InstagramMediaPrepareService.
    Postiz TikTok FILE_UPLOAD requires HEAD + ``Accept-Ranges`` / HTTP 206.
    """
    safe_name = os.path.basename(file_name)
    if not _PUBLIC_MEDIA_FILE.fullmatch(safe_name):
        raise HTTPException(status_code=404, detail="Media not found")
    storage = _get_storage_service()
    try:
        content_type, total_length = storage.head_object_meta(
            safe_name,
            folder=StorageFolder.instagram_publish,
        )
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code") or "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=404, detail="Media not found") from exc
        logger.error("[SOCIAL] public media fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Media unavailable") from exc
    except Exception as exc:
        logger.error("[SOCIAL] public media fetch failed: %s", exc)
        raise HTTPException(status_code=404, detail="Media not found") from exc

    media_type = "image/jpeg" if safe_name.endswith(".jpg") else "video/mp4"
    if content_type and content_type != "application/octet-stream":
        media_type = content_type

    headers = {
        "Cache-Control": "public, max-age=86400",
        "Accept-Ranges": "bytes",
        "Content-Encoding": "identity",
    }
    range_header = request.headers.get("range") or request.headers.get("Range") or ""
    byte_range = parse_http_byte_range(range_header, total_length)
    if range_header.strip() and byte_range is None:
        headers["Content-Range"] = f"bytes */{total_length}"
        return Response(status_code=416, headers=headers)

    start = end = None
    status_code = 200
    content_length = total_length
    if byte_range is not None:
        start, end = byte_range
        content_length = end - start + 1
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total_length}"
    headers["Content-Length"] = str(content_length)

    if request.method == "HEAD":
        return Response(status_code=status_code, media_type=media_type, headers=headers)

    try:
        _, _, chunks = storage.iter_object_chunks(
            safe_name,
            folder=StorageFolder.instagram_publish,
            start=start,
            end=end,
        )
    except ClientError as exc:
        logger.error("[SOCIAL] public media fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Media unavailable") from exc

    return StreamingResponse(
        chunks,
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


@social_routes.post("/postiz/posts")
async def postiz_create_post(
    payload: Dict[str, Any],
    agent_name: Optional[str] = Query(
        None,
        description=(
            "When set to digital_marketing (aliases: digital_margeting, digital-marketing), "
            "marketing text and media URLs from the request body are stored after Postiz accepts the post."
        ),
    ),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Create/schedule a post in Postiz using the raw Postiz Public API payload.

    The payload is passed through to `POST /api/public/v1/posts` on your Postiz instance.

    For the digital marketing agent, pass `?agent_name=digital_marketing` so caption and media
    links are archived for later download via `/digital-marketing/assets`.
    """
    postiz_base_url = os.getenv("POSTIZ_BASE_URL", "").strip()
    if not postiz_base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = _resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Configure mapping or set POSTIZ_PUBLIC_API_KEY.",
        )

    _, media_links = extract_marketing_text_and_links(payload)
    if media_links or payload_has_attached_media(payload):
        try:
            payload = await asyncio.to_thread(
                InstagramMediaPrepareService().prepare_postiz_payload,
                payload,
            )
        except InstagramMediaPrepareError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("[SOCIAL] Failed to prepare media for Postiz/Instagram")
            raise HTTPException(
                status_code=502,
                detail="Could not prepare media for Instagram. Check that the image or video URL is publicly reachable.",
            ) from exc

    payload = coerce_tiktok_privacy_for_unaudited_app(payload)

    try:
        client = PostizClient(postiz_base_url)
        result = await client.create_post(api_key, payload)
        publish_ids = extract_publish_ids(result)
        if isinstance(result, dict):
            result = {**result, "tiktok_publish_ids": publish_ids}
        else:
            result = {"value": result, "tiktok_publish_ids": publish_ids}
    except PostizAPIError as e:
        logger.warning("[SOCIAL] Postiz create post failed: %s", e)
        # Postiz 400s are client/payload problems (wrong media type, settings, etc.).
        # Keep 502 for upstream/auth/server failures.
        status_code = (
            e.status_code
            if e.status_code in {400, 404, 409, 422, 429}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=status_code, detail=str(e)) from e

    canonical_agent = normalize_digital_marketing_agent_name(agent_name)
    if canonical_agent:
        try:
            user = _get_user_for_jwt_subject(db, jwt_subject)
            if user:
                text, links = extract_marketing_text_and_links(payload)
                DigitalMarketingAssetService(db).create_from_postiz(
                    user_internal_id=str(user.id),
                    agent_name=canonical_agent,
                    marketing_text=text,
                    content_links=links,
                    postiz_response=result if isinstance(result, dict) else {"value": result},
                )
        except Exception as arch_exc:
            logger.warning(
                "[DIGITAL_MARKETING] Failed to archive Postiz marketing payload: %s",
                arch_exc,
                exc_info=True,
            )

    return result


@social_routes.post(
    "/digital-marketing/assets",
    response_model=DigitalMarketingAssetDetailResponse,
)
async def create_digital_marketing_asset(
    body: DigitalMarketingAssetCreate,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Archive a chat campaign (text transcript + media URLs) as a recent campaign."""
    user = _get_user_for_jwt_subject(db, jwt_subject)
    canonical_agent = (
        normalize_digital_marketing_agent_name(body.agent_name)
        or "digital_marketing"
    )
    row = DigitalMarketingAssetService(db).create_campaign(
        user_internal_id=str(user.id),
        agent_name=canonical_agent,
        marketing_text=(body.marketing_text or "").strip(),
        content_links=list(body.content_links or []),
        conversation=body.conversation,
        contents=body.contents,
    )
    return DigitalMarketingAssetDetailResponse.model_validate(row)


@social_routes.get(
    "/digital-marketing/assets",
    response_model=DigitalMarketingAssetListResponse,
)
async def list_digital_marketing_assets(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    user = _get_user_for_jwt_subject(db, jwt_subject)

    svc = DigitalMarketingAssetService(db)
    rows = svc.list_for_user(str(user.id), limit=limit, offset=offset)
    total = svc.count_for_user(str(user.id))
    items = [DigitalMarketingAssetResponse.model_validate(r) for r in rows]
    return DigitalMarketingAssetListResponse(items=items, total=total)


@social_routes.get(
    "/digital-marketing/assets/{asset_id}",
    response_model=DigitalMarketingAssetDetailResponse,
)
async def get_digital_marketing_asset(
    asset_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user = _get_user_for_jwt_subject(db, jwt_subject)

    row = DigitalMarketingAssetService(db).get_for_user(str(user.id), asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    return DigitalMarketingAssetDetailResponse.model_validate(row)


@social_routes.get("/digital-marketing/assets/{asset_id}/download")
async def download_digital_marketing_asset(
    asset_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user = _get_user_for_jwt_subject(db, jwt_subject)

    row = DigitalMarketingAssetService(db).get_for_user(str(user.id), asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    body: Dict[str, Any] = {
        "id": row.id,
        "agent_name": row.agent_name,
        "marketing_text": row.marketing_text,
        "content_links": row.content_links or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "postiz_response": row.postiz_response,
    }
    safe_name = asset_id.replace("/", "_").replace("\\", "_")[:80]
    return JSONResponse(
        content=body,
        headers={
            "Content-Disposition": (
                f'attachment; filename="digital-marketing-{safe_name}.json"'
            )
        },
    )
