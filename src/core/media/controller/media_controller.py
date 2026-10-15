import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.agent.dto.media_generation_request import MediaGenerationRequest
from core.credits.credit_catalog import feature_cost
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.user.controller.usercontroller import get_db, validate_token
from another_fastapi_jwt_auth import AuthJWT
from core.agent.tools.google_image.google_image_service import (
    GoogleImageGenerationError,
    GoogleImageService,
    GoogleImageTimeoutError,
)
from core.agent.tools.google_veo.google_veo_service import (
    GoogleVeoGenerationError,
    GoogleVeoService,
    GoogleVeoTimeoutError,
)
from core.media.dto.automedia_dto import (
    AutomediaAssetDto,
    AutomediaAssetPatch,
    AutomediaCampaignDto,
    AutomediaCampaignUpsert,
)
from core.media.dto.video_concat_dto import VideoConcatRequest, VideoConcatResponse
from core.media.dto.media_generation_response import (
    ImageGenerationResponse,
    VideoGenerationResponse,
)
from core.media.service import automedia_library
from core.media.service.media_generation_options import (
    apply_kind_brief,
    media_kind_for,
    normalize_duration,
    normalize_image_aspect,
    normalize_resolution,
    normalize_video_aspect,
)
from core.media.service.media_rag_prompt import enrich_media_generation_prompt
from core.media.service.media_reference import (
    MediaReferenceError,
    is_video_mime,
    reference_prompt_prefix,
    resolve_generation_references,
)

logger = logging.getLogger(__name__)

media_routes = APIRouter()


def _resolve_credit_user(
    db: Session,
    authjwt: AuthJWT | None,
    req_user_id: str | None,
) -> str:
    credit_service = CreditService(db)
    user_id = None
    if authjwt:
        user_id = credit_service.resolve_user_id(authjwt.get_jwt_subject())
    if not user_id and req_user_id:
        user_id = credit_service.resolve_user_id(req_user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for media generation.")
    return user_id


def _deduct_generation_credits(
    db: Session,
    credit_type: str,
    operation: str,
    authjwt: AuthJWT | None,
    req_user_id: str | None,
    references: list[tuple[str, str]],
) -> None:
    """Charge the generation plus one extra feature unit per attached reference."""
    credit_service = CreditService(db)
    user_id = _resolve_credit_user(db, authjwt, req_user_id)

    charges: list[tuple[str, str]] = [(credit_type, operation)]
    for _, mime in references:
        if is_video_mime(mime):
            charges.append((CreditType.VIDEO_GEN.value, "generation_reference"))
        else:
            charges.append((CreditType.IMAGE_GEN.value, "generation_reference"))

    needed = sum(feature_cost(kind, 1.0) for kind, _ in charges)
    remaining = credit_service.get_remaining(user_id, "wallet")
    if needed > 0 and remaining < needed:
        raise HTTPException(
            status_code=402,
            detail={
                "message": (
                    "Insufficient credits for this generation"
                    + (" and its references" if references else "")
                    + ". Buy more credits to continue."
                ),
                "credit_type": credit_type,
                "remaining": remaining,
                "required": needed,
            },
        )

    for kind, op in charges:
        credit_service.require_credits(user_id, kind, 1.0, op)


def _jwt_subject(authjwt: AuthJWT | None) -> str | None:
    if not authjwt:
        return None
    try:
        subject = authjwt.get_jwt_subject()
    except Exception:
        return None
    return str(subject).strip() if subject else None


def _grounded_media_prompt(
    req: MediaGenerationRequest,
    db: Session,
    authjwt: AuthJWT | None,
    media_kind: str,
) -> str:
    return enrich_media_generation_prompt(
        req.prompt,
        db,
        jwt_subject=_jwt_subject(authjwt),
        req_user_id=req.user_id,
        media_kind=media_kind,
    )


def _store_generated_image(b64: str, mime_type: str, user_id: str | None) -> str | None:
    try:
        from core.intelligence.service.owner_agent_campaign import upload_image_bytes

        return upload_image_bytes(user_id or "automedia", b64, mime_type)
    except Exception as e:
        logger.warning("Could not store generated image: %s", e)
        return None


@media_routes.post("/generate-image", response_model=ImageGenerationResponse)
async def generate_image(
    req: MediaGenerationRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """
    Generate an image via Google Generative Language API (Nana Banana / Gemini image model).
    Uses GOOGLE_API_KEY, NANA_BANANA_BASE_URL, and NANA_BANANA_MODEL from the environment.
    The user prompt is grounded with RAG-indexed business documents, website knowledge,
    and the merchant product catalog before it is sent to the image model.
    Accepts Flow-style references, aspect_ratio, and kind (image|character).
    """
    try:
        references = await resolve_generation_references(req)
    except MediaReferenceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _deduct_generation_credits(
        db, CreditType.IMAGE_GEN.value, "image_generation", authjwt, req.user_id, references
    )
    try:
        service = GoogleImageService()
        kind = media_kind_for(req.kind, "image")
        grounded_prompt = _grounded_media_prompt(req, db, authjwt, kind)
        grounded_prompt = apply_kind_brief(grounded_prompt, req.kind)
        if references:
            grounded_prompt = reference_prompt_prefix(references) + grounded_prompt
        aspect = normalize_image_aspect(req.aspect_ratio)
        b64 = await service.generate_image_base64(
            grounded_prompt,
            user_id=req.user_id,
            references=references,
            aspect_ratio=aspect,
        )
        mime_type = service.last_mime_type or "image/png"
        image_url = _store_generated_image(b64, mime_type, req.user_id)
        return ImageGenerationResponse(
            prompt=req.prompt,
            image_base64=b64,
            mime_type=mime_type,
            image_url=image_url,
            aspect_ratio=aspect,
            kind=req.kind,
        )
    except GoogleImageTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except GoogleImageGenerationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Image generation failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Image generation failed")


@media_routes.post("/generate-video", response_model=VideoGenerationResponse)
async def generate_video(
    req: MediaGenerationRequest,
    store: bool = Query(
        False,
        description="When true, download the Google video and upload to Contabo; stored_url is set.",
    ),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """
    Generate a video via Google Veo (Generative Language API).
    By default returns the direct Google video URL. Set store=true to also persist on Contabo.
    The user prompt is grounded with RAG-indexed business documents, website knowledge,
    and the merchant product catalog before it is sent to Veo.
    Accepts Flow-style image references, aspect_ratio, duration_seconds, resolution, and kind (video|scene).
    """
    try:
        references = await resolve_generation_references(req)
    except MediaReferenceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _deduct_generation_credits(
        db, CreditType.VIDEO_GEN.value, "video_generation", authjwt, req.user_id, references
    )
    try:
        service = GoogleVeoService()
        kind = media_kind_for(req.kind, "video")
        grounded_prompt = _grounded_media_prompt(req, db, authjwt, kind)
        grounded_prompt = apply_kind_brief(grounded_prompt, req.kind)
        if references:
            grounded_prompt = reference_prompt_prefix(references) + grounded_prompt
        aspect = normalize_video_aspect(req.aspect_ratio)
        duration = normalize_duration(req.duration_seconds)
        resolution = normalize_resolution(req.resolution)
        if store:
            stored_url = await service.generate_video_and_store(
                grounded_prompt,
                user_id=req.user_id,
                references=references,
                aspect_ratio=aspect,
                duration_seconds=duration,
                resolution=resolution,
            )
            return VideoGenerationResponse(
                prompt=req.prompt,
                video_url=stored_url,
                stored_url=stored_url,
                aspect_ratio=aspect,
                duration_seconds=duration,
                resolution=resolution,
                kind=req.kind,
            )
        video_url = await service.generate_video_url(
            grounded_prompt,
            user_id=req.user_id,
            references=references,
            aspect_ratio=aspect,
            duration_seconds=duration,
            resolution=resolution,
        )
        return VideoGenerationResponse(
            prompt=req.prompt,
            video_url=video_url,
            stored_url=None,
            aspect_ratio=aspect,
            duration_seconds=duration,
            resolution=resolution,
            kind=req.kind,
        )
    except GoogleVeoTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except GoogleVeoGenerationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Video generation failed: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Video generation failed")


@media_routes.post("/automedia/concat", response_model=VideoConcatResponse)
async def concat_automedia_videos(
    payload: VideoConcatRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Stitch generated scene clips into one MP4 for preview and social posting."""
    automedia_library.resolve_library_user_id(db, authjwt)
    from core.media.service.video_concat import VideoConcatError, concat_video_urls

    try:
        url = await concat_video_urls(payload.urls, payload.aspect_ratio)
    except VideoConcatError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.error("Scene concat failed: %s", error, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not assemble the scene video.") from error
    return VideoConcatResponse(video_url=url, clip_count=len([item for item in payload.urls if item.strip()]))


@media_routes.get("/automedia/campaigns", response_model=list[AutomediaCampaignDto])
def list_automedia_campaigns(
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.list_campaigns(db, user_id)


@media_routes.get("/automedia/campaigns/{campaign_id}", response_model=AutomediaCampaignDto)
def get_automedia_campaign(
    campaign_id: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.get_campaign(db, user_id, campaign_id)


@media_routes.put("/automedia/campaigns/{campaign_id}", response_model=AutomediaCampaignDto)
def upsert_automedia_campaign(
    campaign_id: str,
    payload: AutomediaCampaignUpsert,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.upsert_campaign(db, user_id, campaign_id, payload)


@media_routes.post(
    "/automedia/campaigns/{campaign_id}/assets",
    response_model=AutomediaAssetDto,
)
def create_automedia_asset(
    campaign_id: str,
    payload: AutomediaAssetDto,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.create_asset(db, user_id, campaign_id, payload)


@media_routes.patch("/automedia/assets/{asset_id}", response_model=AutomediaAssetDto)
def patch_automedia_asset(
    asset_id: str,
    payload: AutomediaAssetPatch,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.patch_asset(db, user_id, asset_id, payload)


@media_routes.delete("/automedia/campaigns/{campaign_id}")
def delete_automedia_campaign(
    campaign_id: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    automedia_library.delete_campaign(db, user_id, campaign_id)
    return {"ok": True}


@media_routes.delete("/automedia/assets/{asset_id}")
def delete_automedia_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    automedia_library.delete_asset(db, user_id, asset_id)
    return {"ok": True}


@media_routes.get("/automedia/characters", response_model=list[AutomediaAssetDto])
def list_automedia_characters(
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    user_id = automedia_library.resolve_library_user_id(db, authjwt)
    return automedia_library.list_characters(db, user_id)
