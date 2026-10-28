from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.media.dto.automedia_dto import (
    AutomediaAssetDto,
    AutomediaAssetPatch,
    AutomediaCampaignDto,
    AutomediaCampaignUpsert,
    AutomediaClipDto,
)
from core.media.model.automedia import AutomediaAsset, AutomediaCampaign
from another_fastapi_jwt_auth import AuthJWT


ALLOWED_KINDS = frozenset({"image", "video", "character", "scene", "collection"})
ALLOWED_TABS = frozenset({"workspace", "character", "scenes"})


def resolve_library_user_id(db: Session, authjwt: AuthJWT) -> str:
    subject = authjwt.get_jwt_subject()
    user_id = CreditService(db).resolve_user_id(str(subject).strip() if subject else None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required for Automedia library.",
        )
    return user_id


def _ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _clips_payload(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    clips: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        clip_id = str(item.get("id") or "").strip()
        if not url or not clip_id:
            continue
        duration = item.get("duration_sec")
        clips.append(
            {
                "id": clip_id[:50],
                "url": url,
                "prompt": str(item.get("prompt") or ""),
                "duration_sec": int(duration) if isinstance(duration, int) else None,
            }
        )
    return clips


def asset_to_dto(row: AutomediaAsset) -> AutomediaAssetDto:
    clips = [AutomediaClipDto.model_validate(item) for item in _clips_payload(row.clips)]
    return AutomediaAssetDto(
        id=row.id,
        campaign_id=row.campaign_id,
        kind=row.kind,
        url=row.url,
        prompt=row.prompt or "",
        name=row.name,
        favorite=bool(row.favorite),
        source=row.source or "generated",
        aspect=row.aspect or "16:9",
        duration_sec=row.duration_sec,
        resolution=row.resolution,
        clips=clips,
        created_at=_ms(row.created_at),
    )


def campaign_to_dto(row: AutomediaCampaign, assets: list[AutomediaAsset]) -> AutomediaCampaignDto:
    return AutomediaCampaignDto(
        id=row.id,
        name=row.name,
        tab=row.tab if row.tab in ALLOWED_TABS else "workspace",
        prompt_mode=row.prompt_mode if row.prompt_mode in {"image", "video"} else "image",
        filters=row.filters or {},
        gen_defaults=row.gen_defaults or {},
        assets=[asset_to_dto(item) for item in assets],
        created_at=_ms(row.created_at),
        updated_at=_ms(row.updated_at),
    )


def _get_campaign(db: Session, campaign_id: str, user_id: str) -> AutomediaCampaign | None:
    return (
        db.query(AutomediaCampaign)
        .filter(AutomediaCampaign.id == campaign_id, AutomediaCampaign.user_id == user_id)
        .first()
    )


def _require_campaign(db: Session, campaign_id: str, user_id: str) -> AutomediaCampaign:
    row = _get_campaign(db, campaign_id, user_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return row


def list_campaigns(db: Session, user_id: str) -> list[AutomediaCampaignDto]:
    rows = (
        db.query(AutomediaCampaign)
        .filter(AutomediaCampaign.user_id == user_id)
        .order_by(AutomediaCampaign.updated_at.desc())
        .all()
    )
    return [campaign_to_dto(row, []) for row in rows]


def get_campaign(db: Session, user_id: str, campaign_id: str) -> AutomediaCampaignDto:
    row = _require_campaign(db, campaign_id, user_id)
    assets = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.campaign_id == campaign_id, AutomediaAsset.user_id == user_id)
        .order_by(AutomediaAsset.created_at.desc())
        .all()
    )
    return campaign_to_dto(row, assets)


def upsert_campaign(
    db: Session, user_id: str, campaign_id: str, payload: AutomediaCampaignUpsert
) -> AutomediaCampaignDto:
    row = _get_campaign(db, campaign_id, user_id)
    tab = payload.tab if payload.tab in ALLOWED_TABS else "workspace"
    prompt_mode = payload.prompt_mode if payload.prompt_mode in {"image", "video"} else "image"
    name = (payload.name or "").strip() or "Untitled campaign"
    if row is None:
        row = AutomediaCampaign(
            id=campaign_id[:50],
            user_id=user_id,
            name=name[:255],
            tab=tab,
            prompt_mode=prompt_mode,
            filters=payload.filters or {},
            gen_defaults=payload.gen_defaults or {},
        )
        db.add(row)
    else:
        row.name = name[:255]
        row.tab = tab
        row.prompt_mode = prompt_mode
        row.filters = payload.filters or {}
        row.gen_defaults = payload.gen_defaults or {}
        row.updated_at = datetime.now(timezone.utc)

    if payload.assets:
        for asset in payload.assets:
            _upsert_asset_row(db, user_id, campaign_id, asset)

    db.commit()
    db.refresh(row)
    return get_campaign(db, user_id, row.id)


def _upsert_asset_row(
    db: Session, user_id: str, campaign_id: str, payload: AutomediaAssetDto
) -> AutomediaAsset:
    kind = (payload.kind or "").strip().lower()
    if kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid asset kind")
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Asset url is required")

    clips = [item.model_dump() for item in payload.clips]
    created_at = _from_ms(payload.created_at) or datetime.now(timezone.utc)
    row = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.id == payload.id, AutomediaAsset.user_id == user_id)
        .first()
    )
    if row is None:
        row = AutomediaAsset(
            id=payload.id[:50],
            campaign_id=campaign_id[:50],
            user_id=user_id,
            kind=kind,
            url=url,
            prompt=payload.prompt or "",
            name=(payload.name or None) and payload.name[:255],
            favorite=bool(payload.favorite),
            source=(payload.source or "generated")[:32],
            aspect=(payload.aspect or "16:9")[:16],
            duration_sec=payload.duration_sec,
            resolution=payload.resolution,
            clips=clips,
            created_at=created_at,
        )
        db.add(row)
        return row

    if row.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    row.campaign_id = campaign_id[:50]
    row.kind = kind
    row.url = url
    row.prompt = payload.prompt or ""
    row.name = (payload.name or None) and payload.name[:255]
    row.favorite = bool(payload.favorite)
    row.source = (payload.source or "generated")[:32]
    row.aspect = (payload.aspect or "16:9")[:16]
    row.duration_sec = payload.duration_sec
    row.resolution = payload.resolution
    row.clips = clips
    row.updated_at = datetime.now(timezone.utc)
    return row


def create_asset(
    db: Session, user_id: str, campaign_id: str, payload: AutomediaAssetDto
) -> AutomediaAssetDto:
    existing = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.id == payload.id, AutomediaAsset.user_id == user_id)
        .first()
    )
    if existing is None:
        CreditService(db).require_credits(user_id, CreditType.MEDIA_SAVE.value, 1.0, "media_save")
    row = _get_campaign(db, campaign_id, user_id)
    if row is None:
        db.add(
            AutomediaCampaign(
                id=campaign_id[:50],
                user_id=user_id,
                name="Untitled campaign",
                tab="workspace",
                prompt_mode="image",
                filters={},
                gen_defaults={},
            )
        )
        db.flush()
    row = _upsert_asset_row(db, user_id, campaign_id, payload)
    db.commit()
    db.refresh(row)
    return asset_to_dto(row)


def patch_asset(db: Session, user_id: str, asset_id: str, payload: AutomediaAssetPatch) -> AutomediaAssetDto:
    row = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.id == asset_id, AutomediaAsset.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")

    data = payload.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] is not None:
        kind = str(data["kind"]).strip().lower()
        if kind not in ALLOWED_KINDS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid asset kind")
        row.kind = kind
    if "url" in data and data["url"] is not None:
        url = str(data["url"]).strip()
        if not url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Asset url is required")
        row.url = url
    if "prompt" in data and data["prompt"] is not None:
        row.prompt = data["prompt"]
    if "name" in data:
        name = data["name"]
        row.name = name[:255] if isinstance(name, str) and name.strip() else None
    if "favorite" in data and data["favorite"] is not None:
        row.favorite = bool(data["favorite"])
    if "source" in data and data["source"] is not None:
        row.source = str(data["source"])[:32]
    if "aspect" in data and data["aspect"] is not None:
        row.aspect = str(data["aspect"])[:16]
    if "duration_sec" in data:
        row.duration_sec = data["duration_sec"]
    if "resolution" in data:
        row.resolution = data["resolution"]
    if "clips" in data and data["clips"] is not None:
        row.clips = [item.model_dump() if hasattr(item, "model_dump") else item for item in payload.clips or []]
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return asset_to_dto(row)


def delete_campaign(db: Session, user_id: str, campaign_id: str) -> None:
    row = _get_campaign(db, campaign_id, user_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.campaign_id == campaign_id, AutomediaAsset.user_id == user_id)
        .delete(synchronize_session=False)
    )
    db.delete(row)
    db.commit()


def delete_asset(db: Session, user_id: str, asset_id: str) -> None:
    row = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.id == asset_id, AutomediaAsset.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    db.delete(row)
    db.commit()


def list_characters(db: Session, user_id: str) -> list[AutomediaAssetDto]:
    rows = (
        db.query(AutomediaAsset)
        .filter(AutomediaAsset.user_id == user_id, AutomediaAsset.kind == "character")
        .order_by(AutomediaAsset.updated_at.desc())
        .all()
    )
    return [asset_to_dto(row) for row in rows]
