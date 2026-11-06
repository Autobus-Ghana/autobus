"""
Social Media Controller
API routes for social media account management and posting
"""

import asyncio
import html
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

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
from core.socialmedia.service.postiz_api_service import PostizAPIError, PostizClient
from core.socialmedia.service.postiz_native import (
    build_native_connect,
    classify_marketing_posts,
    delete_native_integration,
    list_native_integrations,
    login_credentials as postiz_login_credentials,
    postiz_base_url,
    publish_marketing_posts,
    resolve_postiz_api_key,
)
from core.socialmedia.service.tiktok_direct_post import (
    coerce_tiktok_privacy_for_unaudited_app,
    load_tiktok_creator_info,
    load_tiktok_publish_status,
)
from core.socialmedia.service.postiz_marketing_extract import (
    extract_marketing_text_and_links,
    normalize_digital_marketing_agent_name,
)
from core.socialmedia.service.digital_marketing_asset_service import DigitalMarketingAssetService
from core.socialmedia.service.instagram_media_prepare import (
    InstagramMediaPrepareError,
    InstagramMediaPrepareService,
    parse_http_byte_range,
    payload_has_attached_media,
)
from core.socialmedia.service.zernio_client import ZernioAPIError
from core.socialmedia.service.zernio_mapping import ZERNIO_CONNECT_DISABLED, ZERNIO_PLATFORMS
from core.socialmedia.service.zernio_service import (
    connect_authorization,
    disconnect_account as disconnect_zernio_account,
    list_accounts as list_zernio_accounts,
    posting_integrations,
    read_state,
    record_callback_account,
)
from core.cloudstorage.service.storageservice import StorageFolder, StorageService
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.user.model.User import User
from utilities.auth_subject import resolve_internal_user_id
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
    "twitter": "TWITTER",
    "x": "TWITTER",
    "linkedin": "LINKEDIN",
    "pinterest": "PINTEREST",
    "reddit": "REDDIT",
    "bluesky": "BLUESKY",
    "threads": "THREADS",
    "googlebusiness": "GOOGLE_BUSINESS",
    "snapchat": "SNAPCHAT",
    "telegram": "TELEGRAM",
    "discord": "DISCORD",
    "slack": "SLACK",
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



def _instagram_business_login_connect(
    internal_user_id: str,
    return_to: str = "web",
) -> Dict[str, Any]:
    """Instagram inbox and Digital Marketing posting use Autobus Business Login."""
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

        zernio_slug = (postiz_slug or platform.strip().lower()).replace("_", "")
        if zernio_slug in {"x", "instagram-standalone"}:
            zernio_slug = "twitter" if zernio_slug == "x" else "instagram"
        if zernio_slug == "google-business":
            zernio_slug = "googlebusiness"
        if zernio_slug in ZERNIO_CONNECT_DISABLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X and Snapchat are not available to link.",
            )
        is_whatsapp_status = platform.strip().lower().replace("_", "-") == "whatsapp-status"
        if platform_upper in {"TIKTOK", "YOUTUBE"} and postiz_slug:
            try:
                return await build_native_connect(
                    internal_user_id=internal_user_id,
                    db=db,
                    platform_upper=platform_upper,
                    postiz_slug=postiz_slug,
                )
            except PostizAPIError as exc:
                code = exc.status_code if exc.status_code in {400, 404, 502, 503} else 502
                raise HTTPException(status_code=code, detail=str(exc)) from exc
        if zernio_slug in ZERNIO_PLATFORMS and not is_whatsapp_status:
            user = db.query(User).filter(User.id == internal_user_id).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            try:
                return connect_authorization(
                    db,
                    user,
                    zernio_slug,
                    return_to=return_to,
                )
            except ZernioAPIError as exc:
                raise HTTPException(status_code=exc.status_code if exc.status_code < 500 else 502, detail=str(exc)) from exc

        if not is_whatsapp_status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "That platform is not available. "
                    "Digital Marketing links YouTube, TikTok, Facebook, and Instagram. "
                    "Inbox links Instagram and WhatsApp."
                ),
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
        CreditService(db).require_credits(
            internal_user_id,
            CreditType.SOCIAL_POST.value,
            float(max(1, len(request.account_ids))),
            "social_post",
        )
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
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SOCIAL] Error publishing post: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error publishing post: {str(e)}"
        )


# ==================== Connected-account routes ====================

@social_routes.get("/zernio/accounts")
async def zernio_list_accounts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Connected Zernio accounts for this customer. WhatsApp is listed from /whatsapp/accounts."""
    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    return [
        {
            "id": row.id,
            "platform": row.platform,
            "username": row.username,
            "name": row.display_name or row.username,
            "zernio_account_id": row.zernio_account_id,
            "is_active": row.is_active,
        }
        for row in list_zernio_accounts(db, internal_user_id)
        if row.platform != "whatsapp"
    ]


@social_routes.get("/zernio/callback")
async def zernio_oauth_callback(
    db: Session = Depends(get_db),
    state: Optional[str] = Query(None),
    accountId: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    profileId: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_message: Optional[str] = Query(None),
):
    """Browser return from Zernio OAuth. The account.connected webhook is the other path."""
    from fastapi.responses import HTMLResponse

    from core.instagram.controller.instagram_controller import _error_html, _result_html

    parsed = read_state(state or "")
    return_to = (parsed or {}).get("return_to") or "web"
    if error:
        return HTMLResponse(
            _error_html(error_message or error, return_to=return_to),
            status_code=400,
        )
    if not parsed:
        return HTMLResponse(
            _error_html("This connect link expired. Start again from Autobus.", return_to=return_to),
            status_code=400,
        )
    remote_id = (accountId or "").strip()
    if not remote_id:
        return HTMLResponse(
            _error_html(
                "Zernio did not return an account. Finish the connect screen and try again.",
                return_to=return_to,
            ),
            status_code=400,
        )
    try:
        account = record_callback_account(
            db,
            user_id=parsed["user_id"],
            platform=parsed["platform"],
            account_id=remote_id,
            username=(username or "").strip(),
            profile_id=(profileId or "").strip(),
        )
    except ZernioAPIError as exc:
        return HTMLResponse(_error_html(str(exc), return_to=return_to), status_code=400)

    label = html.escape(account.username or account.display_name or account.platform)
    platform_js = json.dumps(account.platform)
    account_js = json.dumps(account.id)
    return HTMLResponse(
        _result_html(
            title=f"{account.platform.title()} linked",
            heading=f"{account.platform.title()} linked",
            body_html=(
                f"<p>Connected <strong>{label}</strong>. "
                "Inbox messages and posts for this account go through Zernio.</p>"
            ),
            return_to=return_to,
            error=False,
            extra_script=(
                "try{var msg={type:'AUTOBUS_ZERNIO_LINKED',platform:"
                f"{platform_js},account_id:{account_js}"
                "};if(window.opener){window.opener.postMessage(msg,'*');}"
                "if(" + platform_js + "==='instagram'&&window.opener){"
                "window.opener.postMessage({type:'AUTOBUS_INSTAGRAM_LINKED',ig_user_id:"
                f"{account_js}"
                "},'*');}}catch(e){}"
            ),
        )
    )


@social_routes.get("/postiz/integrations")
async def postiz_list_integrations(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Connected marketing accounts. Facebook and Instagram come from Zernio. TikTok and YouTube come from Postiz."""
    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    return posting_integrations(db, internal_user_id) + list_native_integrations(internal_user_id, db)


@social_routes.get("/postiz/tiktok/creator-info")
async def postiz_tiktok_creator_info(
    integration_id: str = Query(..., min_length=1),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Latest TikTok creator_info from TikTok's API for the Post to TikTok screen."""
    base_url = postiz_base_url()
    if not base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Link TikTok again from Marketing.",
        )

    user = _get_user_for_jwt_subject(db, jwt_subject)
    email, password = postiz_login_credentials(user)
    try:
        return await load_tiktok_creator_info(
            client=PostizClient(base_url),
            api_key=api_key,
            integration_id=integration_id,
            postiz_email=email,
            postiz_password=password,
        )
    except PostizAPIError as exc:
        status_code = exc.status_code if exc.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

@social_routes.get("/postiz/tiktok/publish-status")
async def postiz_tiktok_publish_status(
    integration_id: str = Query(..., min_length=1),
    publish_id: Optional[str] = Query(None),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Poll TikTok's API for Direct Post processing status."""
    base_url = postiz_base_url()
    if not base_url:
        raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL not configured")

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    api_key = resolve_postiz_api_key(internal_user_id, db)
    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="No Postiz API key found. Link TikTok again from Marketing.",
        )

    user = _get_user_for_jwt_subject(db, jwt_subject)
    email, password = postiz_login_credentials(user)
    try:
        return await load_tiktok_publish_status(
            client=PostizClient(base_url),
            api_key=api_key,
            integration_id=integration_id,
            publish_id=(publish_id or "").strip(),
            postiz_email=email,
            postiz_password=password,
        )
    except PostizAPIError as exc:
        status_code = exc.status_code if exc.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

@social_routes.get("/postiz/posts")
async def postiz_list_posts(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """Recent posts. Publishing now goes through Zernio, which has no list endpoint here."""
    return []

@social_routes.delete("/postiz/integrations/{integration_id}")
async def postiz_delete_integration(
    integration_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Unlink a connected social account for the current customer.
    """
    iid = (integration_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="integration_id is required")
    if iid.startswith("autobus-ig-"):
        raise HTTPException(
            status_code=400,
            detail="Use DELETE /api/v1/instagram/accounts/{id} for Autobus Instagram accounts.",
        )

    internal_user_id = resolve_internal_user_id(db, jwt_subject)
    if disconnect_zernio_account(db, internal_user_id, iid):
        logger.info("[SOCIAL] Zernio account unlinked: %s by user %s", iid, internal_user_id)
        return {"status": "ok", "message": "Account unlinked", "provider": "zernio"}
    try:
        if await delete_native_integration(internal_user_id, db, iid):
            logger.info("[SOCIAL] Postiz integration unlinked: %s by user %s", iid, internal_user_id)
            return {"status": "ok", "message": "Account unlinked", "provider": "postiz"}
    except PostizAPIError as exc:
        status_code = exc.status_code if exc.status_code in {400, 401, 404} else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    raise HTTPException(
        status_code=404,
        detail="That account is not connected. Link it again from Marketing.",
    )

def _get_storage_service() -> StorageService:
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service


@social_routes.api_route("/media/public/{file_name}", methods=["GET", "HEAD"])
async def public_instagram_media(file_name: str, request: Request):
    """
    Unauthenticated JPEG/MP4 for social publishing.

    Filenames are unguessable UUIDs written only by InstagramMediaPrepareService.
    TikTok file upload requires HEAD plus ``Accept-Ranges`` / HTTP 206.
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
            "marketing text and media URLs from the request body are stored after the post is accepted."
        ),
    ),
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Create or schedule a post on the connected social accounts.

    For the digital marketing agent, pass `?agent_name=digital_marketing` so caption and media
    links are archived for later download via `/digital-marketing/assets`.
    """
    internal_user_id = resolve_internal_user_id(db, jwt_subject)

    _, media_links = extract_marketing_text_and_links(payload)
    if media_links or payload_has_attached_media(payload):
        try:
            payload = await asyncio.to_thread(
                InstagramMediaPrepareService().prepare_postiz_payload,
                payload,
            )
        except InstagramMediaPrepareError as exc:
            logger.warning("[SOCIAL] media prepare rejected: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("[SOCIAL] Failed to prepare media for posting")
            raise HTTPException(
                status_code=502,
                detail="Could not prepare media. Check that the image or video URL is publicly reachable.",
            ) from exc

    payload = coerce_tiktok_privacy_for_unaudited_app(payload)
    zernio_targets, native_posts, unknown_posts = classify_marketing_posts(
        db, internal_user_id, payload
    )
    if unknown_posts:
        raise HTTPException(
            status_code=400,
            detail="One or more selected accounts are not connected. Link them again from Marketing.",
        )
    if not zernio_targets and not native_posts:
        raise HTTPException(status_code=400, detail="Choose at least one account to post to.")
    CreditService(db).require_credits(
        internal_user_id,
        CreditType.SOCIAL_POST.value,
        float(len(zernio_targets) + len(native_posts)),
        "social_post",
    )
    try:
        result = await publish_marketing_posts(db, internal_user_id, payload)
    except (ZernioAPIError, PostizAPIError) as exc:
        status_code = exc.status_code if exc.status_code in {400, 404, 409, 422, 429, 502, 503} else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

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
                "[DIGITAL_MARKETING] Failed to archive marketing payload: %s",
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
    CreditService(db).require_credits(str(user.id), CreditType.MEDIA_SAVE.value, 1.0, "media_save")
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
