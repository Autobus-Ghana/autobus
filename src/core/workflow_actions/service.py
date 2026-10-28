"""Run existing Autobus agents on behalf of a merchant, for an n8n workflow."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.intelligence.service.owner_copilot_service import OwnerCopilotService
from core.nlu.config import SYSTEM_PROMPTS
from core.nlu.nlu import get_nlu_system
from core.socialmedia.service.digital_marketing_asset_service import DigitalMarketingAssetService
from core.socialmedia.service.instagram_media_prepare import (
    InstagramMediaPrepareError,
    InstagramMediaPrepareService,
)
from core.socialmedia.service.postiz_api_service import (
    PostizAPIError,
    PostizClient,
    coerce_tiktok_privacy_for_unaudited_app,
    normalize_postiz_integrations_list,
)
from core.socialmedia.service.postiz_marketing_extract import extract_marketing_text_and_links
from core.socialmedia.service.tiktok_direct_post import extract_publish_ids
from core.user.model.User import User
from core.workflow_actions.dto import (
    CustomerServiceActionRequest,
    IntelligenceActionRequest,
    MarketingContentActionRequest,
    SocialPostActionRequest,
)
from utilities.plain_text import strip_markdown_formatting

logger = logging.getLogger(__name__)


def resolve_business_user(db: Session, identifier: str) -> User:
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="businessUserId is required.")
    user = (
        db.query(User)
        .filter(
            or_(
                User.id == ident,
                User.email.ilike(ident),
                User.phone == ident,
            )
        )
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No business user matches '{ident}'.",
        )
    return user


def _charge_llm(db: Session, user_id: str, operation: str) -> None:
    CreditService(db).require_credits(user_id, CreditType.LLM.value, 1.0, operation)


class WorkflowActionService:
    def __init__(self, db: Session):
        self.db = db

    def intelligence(self, payload: IntelligenceActionRequest) -> Dict[str, Any]:
        user = resolve_business_user(self.db, payload.resolved_business_user_id())
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required.")
        _charge_llm(self.db, user.id, "workflow_intelligence")
        result = OwnerCopilotService(self.db).chat(user, message)
        return {
            "module": "intelligence",
            "businessUserId": user.id,
            "answer": result.message,
            "sources": result.sources,
            "snapshot": result.snapshot or {},
        }

    def customer_service(self, payload: CustomerServiceActionRequest) -> Dict[str, Any]:
        user = resolve_business_user(self.db, payload.resolved_business_user_id())
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required.")
        _charge_llm(self.db, user.id, "workflow_customer_service")
        reply = get_nlu_system().process_message(user.id, message)
        return {
            "module": "customer_service",
            "businessUserId": user.id,
            "reply": reply or "",
        }

    def marketing_content(self, payload: MarketingContentActionRequest) -> Dict[str, Any]:
        from core.nlu.service.llmclient import LLMClient

        user = resolve_business_user(self.db, payload.resolved_business_user_id())
        prompt = payload.resolved_prompt()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required.")
        _charge_llm(self.db, user.id, "workflow_marketing_content")
        response = LLMClient().chat_completion(
            system_prompt=SYSTEM_PROMPTS["marketing"],
            user_message=prompt,
            conversation_history=None,
            temperature=0.8,
            max_tokens=800,
        )
        content = strip_markdown_formatting(response or "")
        if not content:
            content = "Sorry, I could not generate marketing text right now. Please try again."
        return {
            "module": "digital_marketing",
            "businessUserId": user.id,
            "content": content,
        }

    async def list_social_accounts(self, business_user_id: str) -> Dict[str, Any]:
        user = resolve_business_user(self.db, business_user_id)
        rows = await self._load_integrations(user.id)
        return {
            "module": "multi_account_posting",
            "businessUserId": user.id,
            "accounts": rows,
        }

    async def social_post(self, payload: SocialPostActionRequest) -> Dict[str, Any]:
        user = resolve_business_user(self.db, payload.resolved_business_user_id())
        content = (payload.content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required.")

        wanted = payload.resolved_integration_ids()
        accounts = await self._load_integrations(user.id)
        if not accounts:
            raise HTTPException(
                status_code=404,
                detail="This business has no connected social accounts in Postiz.",
            )
        if not wanted:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "integrationIds is required. Choose accounts from this business.",
                    "accounts": accounts,
                },
            )

        by_id = {row["id"]: row for row in accounts}
        missing = [item for item in wanted if item not in by_id]
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "One or more integration ids are not connected for this business.",
                    "missing": missing,
                    "accounts": accounts,
                },
            )

        selected = [by_id[item] for item in wanted]
        CreditService(self.db).require_credits(
            user.id,
            CreditType.SOCIAL_POST.value,
            float(max(1, len(selected))),
            "workflow_social_post",
        )
        body = self._postiz_payload(payload, content, selected)
        if any(row["identifier"] == "tiktok" for row in selected):
            body = coerce_tiktok_privacy_for_unaudited_app(body)

        media_urls = payload.resolved_media_urls()
        if media_urls:
            try:
                import asyncio

                body = await asyncio.to_thread(
                    InstagramMediaPrepareService().prepare_postiz_payload,
                    body,
                )
            except InstagramMediaPrepareError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        api_key = self._postiz_key(user.id)
        client = PostizClient(self._postiz_base_url())
        try:
            result = await client.create_post(api_key, body)
        except PostizAPIError as exc:
            status_code = (
                exc.status_code
                if getattr(exc, "status_code", 0) in {400, 404, 409, 422, 429}
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

        if isinstance(result, dict):
            result = {**result, "tiktok_publish_ids": extract_publish_ids(result)}
        else:
            result = {"value": result, "tiktok_publish_ids": extract_publish_ids(result)}

        try:
            text, links = extract_marketing_text_and_links(body)
            DigitalMarketingAssetService(self.db).create_from_postiz(
                user_internal_id=str(user.id),
                agent_name="digital_marketing",
                marketing_text=text or content,
                content_links=links,
                postiz_response=result if isinstance(result, dict) else {"value": result},
            )
        except Exception:
            logger.warning("[WORKFLOW_ACTION] Could not archive social post", exc_info=True)

        return {
            "module": "multi_account_posting",
            "businessUserId": user.id,
            "postedTo": [row["id"] for row in selected],
            "postiz": result,
        }

    async def _load_integrations(self, user_id: str) -> List[Dict[str, str]]:
        api_key = self._postiz_key(user_id)
        client = PostizClient(self._postiz_base_url())
        try:
            raw = await client.list_integrations(api_key)
        except PostizAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        rows: List[Dict[str, str]] = []
        for item in normalize_postiz_integrations_list(raw):
            if item.get("disabled") is True:
                continue
            integration_id = str(item.get("id") or "").strip()
            if not integration_id:
                continue
            identifier = str(
                item.get("identifier") or item.get("providerIdentifier") or item.get("provider") or ""
            ).strip().lower()
            name = str(item.get("name") or item.get("profile") or identifier or integration_id)
            rows.append(
                {
                    "id": integration_id,
                    "name": name,
                    "identifier": identifier,
                }
            )
        return rows

    def _postiz_base_url(self) -> str:
        base = os.getenv("POSTIZ_BASE_URL", "").strip()
        if not base:
            raise HTTPException(status_code=400, detail="POSTIZ_BASE_URL is not configured.")
        return base

    def _postiz_key(self, user_id: str) -> str:
        from core.socialmedia.service.postiz_org_service import PostizOrgService

        key = PostizOrgService(self.db).get_public_api_key_for_user(user_id)
        if not key:
            key = (
                os.getenv("POSTIZ_PUBLIC_API_KEY", "").strip()
                or os.getenv("POSTIZ_GLOBAL_PUBLIC_API_KEY", "").strip()
            )
        if not key:
            raise HTTPException(
                status_code=404,
                detail="No Postiz API key found for this business.",
            )
        return key

    def _postiz_payload(
        self,
        payload: SocialPostActionRequest,
        content: str,
        accounts: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        media_urls = payload.resolved_media_urls()
        image_blocks = [{"id": f"media_{index}", "path": url} for index, url in enumerate(media_urls)]
        publish_now = payload.resolved_publish_now()
        scheduled = payload.resolved_scheduled_at()
        if not publish_now and not scheduled:
            raise HTTPException(
                status_code=400,
                detail="scheduledAt is required when publishNow is false.",
            )
        when = scheduled if scheduled and not publish_now else datetime.now(timezone.utc).isoformat()
        privacy = payload.resolved_tiktok_privacy()
        posts = []
        for account in accounts:
            posts.append(
                {
                    "integration": {"id": account["id"]},
                    "value": [{"content": content, "image": image_blocks}],
                    "settings": _settings_for(account["identifier"], content, privacy),
                }
            )
        return {
            "type": "now" if publish_now else "schedule",
            "date": when,
            "shortLink": False,
            "tags": [],
            "posts": posts,
        }


def _settings_for(identifier: str, content: str, tiktok_privacy: str) -> Dict[str, Any]:
    kind = (identifier or "").lower()
    title = content.strip().splitlines()[0][:100] if content.strip() else "Marketing post"
    if kind in {"instagram", "instagram-standalone"}:
        return {
            "__type": kind,
            "post_type": "post",
            "is_trial_reel": False,
            "collaborators": [],
        }
    if kind == "x":
        return {"__type": "x", "who_can_reply_post": "everyone", "community": ""}
    if kind == "youtube":
        return {
            "__type": "youtube",
            "title": title[:100],
            "type": "public",
            "selfDeclaredMadeForKids": "no",
            "tags": [],
        }
    if kind == "tiktok":
        allowed = {
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "FOLLOWER_OF_CREATOR",
            "SELF_ONLY",
        }
        privacy = tiktok_privacy if tiktok_privacy in allowed else "PUBLIC_TO_EVERYONE"
        return {
            "__type": "tiktok",
            "title": title[:90],
            "privacy_level": privacy,
            "duet": False,
            "stitch": False,
            "comment": False,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": False,
            "content_posting_method": "DIRECT_POST",
        }
    if kind == "facebook":
        return {"__type": "facebook"}
    return {"__type": kind or "instagram"}
