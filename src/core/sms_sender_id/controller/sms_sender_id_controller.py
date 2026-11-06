"""SMS Sender ID registration — users submit, Autobus team approves later."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from another_fastapi_jwt_auth import AuthJWT
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth.dependencies import require_admin
from core.socialmedia.service.zernio_client import ZernioAPIError, ZernioClient
from utilities.auth_subject import resolve_internal_user_id
from core.sms_sender_id.model.SmsSenderIdRegistration import (
    SmsSenderIdRegistration,
    SmsSenderIdStatus,
)
from core.user.model.User import User
from utilities.dbconfig import get_db

sms_sender_id_routes = APIRouter()


def validate_token(authjwt: AuthJWT = Depends()) -> str:
    try:
        authjwt.jwt_required()
        return authjwt.get_jwt_subject()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


def _new_id() -> str:
    return f"ssid_{uuid.uuid4().hex[:20]}"


def _normalize_sender_id(value: str) -> str:
    return " ".join(value.strip().split())


class SmsSenderIdCreateRequest(BaseModel):
    sender_id: str = Field(..., min_length=3, max_length=32)
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("sender_id")
    @classmethod
    def validate_sender_id(cls, v: str) -> str:
        cleaned = _normalize_sender_id(v)
        if not cleaned or len(cleaned) < 3 or len(cleaned) > 32:
            raise ValueError("Sender ID must be 3–32 characters.")
        # Alphanumeric sender IDs are typical; allow light punctuation for brand names.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,30}[A-Za-z0-9]", cleaned) and not re.fullmatch(
            r"[A-Za-z0-9]{3,32}", cleaned
        ):
            raise ValueError(
                "Sender ID may only contain letters, numbers, spaces, dots, hyphens, or underscores."
            )
        return cleaned

    @field_validator("notes")
    @classmethod
    def strip_optional(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        t = v.strip()
        return t or None


def _ensure_zernio_column(db: Session) -> None:
    """Existing databases were created before Zernio stored a sender id."""
    try:
        db.execute(
            text(
                "ALTER TABLE sms_sender_id_registrations "
                "ADD COLUMN IF NOT EXISTS zernio_sender_id VARCHAR(64)"
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def _register_with_zernio(sender_id: str) -> tuple[str, bool]:
    try:
        created = ZernioClient().create_sms_sender_id(sender_id)
    except ZernioAPIError as exc:
        code = exc.status_code if exc.status_code < 500 else 502
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    remote_id = str(created.get("id") or "").strip()
    if not remote_id:
        raise HTTPException(status_code=502, detail="Zernio did not return a sender ID.")
    active = created.get("isActive") is not False
    return remote_id, active


def _delete_from_zernio(remote_id: Optional[str]) -> None:
    iid = (remote_id or "").strip()
    if not iid:
        return
    try:
        ZernioClient().delete_sms_sender_id(iid)
    except ZernioAPIError:
        return


def _company_name_from_user(db: Session, user_id: str) -> Optional[str]:
    """Use the signup business name so Sender IDs cannot spoof another company."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return None
    name = (user.company or "").strip()
    return name or None


class SmsSenderIdRejectRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class SmsSenderIdResponse(BaseModel):
    id: str
    sender_id: str
    company_name: Optional[str] = None
    notes: Optional[str] = None
    status: str
    rejection_reason: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


def _to_response(row: SmsSenderIdRegistration) -> SmsSenderIdResponse:
    status_val = row.status.value if isinstance(row.status, SmsSenderIdStatus) else str(row.status)
    return SmsSenderIdResponse(
        id=row.id,
        sender_id=row.sender_id,
        company_name=row.company_name,
        notes=row.notes,
        status=status_val,
        rejection_reason=row.rejection_reason,
        is_active=row.is_active,
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
    )


@sms_sender_id_routes.post("", response_model=SmsSenderIdResponse, status_code=status.HTTP_201_CREATED)
@sms_sender_id_routes.post("/", response_model=SmsSenderIdResponse, status_code=status.HTTP_201_CREATED)
def register_sms_sender_id(
    body: SmsSenderIdCreateRequest,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    sender_id = body.sender_id
    company_name = _company_name_from_user(db, user_id)
    _ensure_zernio_column(db)

    existing = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.user_id == user_id,
            SmsSenderIdRegistration.sender_id == sender_id,
            SmsSenderIdRegistration.is_active.is_(True),
        )
        .first()
    )
    if existing:
        if existing.status == SmsSenderIdStatus.REJECTED:
            remote_id, active = _register_with_zernio(sender_id)
            existing.status = SmsSenderIdStatus.APPROVED if active else SmsSenderIdStatus.PENDING
            existing.zernio_sender_id = remote_id
            existing.company_name = company_name
            existing.notes = body.notes
            existing.rejection_reason = None
            existing.reviewed_by = None
            existing.reviewed_at = None
            db.commit()
            db.refresh(existing)
            return _to_response(existing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sender ID '{sender_id}' is already registered ({existing.status.value if isinstance(existing.status, SmsSenderIdStatus) else existing.status}).",
        )

    # Block registering a sender ID another user already has approved.
    conflict = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.sender_id == sender_id,
            SmsSenderIdRegistration.status == SmsSenderIdStatus.APPROVED,
            SmsSenderIdRegistration.is_active.is_(True),
            SmsSenderIdRegistration.user_id != user_id,
        )
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Sender ID is already approved for another account.",
        )

    remote_id, active = _register_with_zernio(sender_id)
    row = SmsSenderIdRegistration(
        id=_new_id(),
        user_id=user_id,
        sender_id=sender_id,
        zernio_sender_id=remote_id,
        company_name=company_name,
        notes=body.notes,
        status=SmsSenderIdStatus.APPROVED if active else SmsSenderIdStatus.PENDING,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_response(row)


@sms_sender_id_routes.get("", response_model=List[SmsSenderIdResponse])
@sms_sender_id_routes.get("/", response_model=List[SmsSenderIdResponse])
def list_my_sms_sender_ids(
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    rows = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.user_id == user_id,
            SmsSenderIdRegistration.is_active.is_(True),
        )
        .order_by(SmsSenderIdRegistration.created_at.desc())
        .all()
    )
    return [_to_response(r) for r in rows]


@sms_sender_id_routes.get("/admin/all", response_model=List[SmsSenderIdResponse])
def list_all_sms_sender_ids_admin(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin
    rows = (
        db.query(SmsSenderIdRegistration)
        .filter(SmsSenderIdRegistration.is_active.is_(True))
        .order_by(SmsSenderIdRegistration.created_at.desc())
        .all()
    )
    return [_to_response(r) for r in rows]


@sms_sender_id_routes.delete("/{registration_id}", response_model=SmsSenderIdResponse)
def deactivate_sms_sender_id(
    registration_id: str,
    jwt_subject: str = Depends(validate_token),
    db: Session = Depends(get_db),
):
    user_id = resolve_internal_user_id(db, jwt_subject)
    row = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.id == registration_id,
            SmsSenderIdRegistration.user_id == user_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Sender ID registration not found")
    _delete_from_zernio(row.zernio_sender_id)
    row.is_active = False
    db.commit()
    db.refresh(row)
    return _to_response(row)


@sms_sender_id_routes.post("/{registration_id}/approve", response_model=SmsSenderIdResponse)
def approve_sms_sender_id(
    registration_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.id == registration_id,
            SmsSenderIdRegistration.is_active.is_(True),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Sender ID registration not found")
    if not (row.zernio_sender_id or "").strip():
        remote_id, active = _register_with_zernio(row.sender_id)
        row.zernio_sender_id = remote_id
        if not active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Zernio has not activated this sender ID yet.",
            )
    row.status = SmsSenderIdStatus.APPROVED
    row.rejection_reason = None
    row.reviewed_by = admin.id
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_response(row)


@sms_sender_id_routes.post("/{registration_id}/reject", response_model=SmsSenderIdResponse)
def reject_sms_sender_id(
    registration_id: str,
    body: SmsSenderIdRejectRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = (
        db.query(SmsSenderIdRegistration)
        .filter(
            SmsSenderIdRegistration.id == registration_id,
            SmsSenderIdRegistration.is_active.is_(True),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Sender ID registration not found")
    _delete_from_zernio(row.zernio_sender_id)
    row.zernio_sender_id = None
    row.status = SmsSenderIdStatus.REJECTED
    row.rejection_reason = (body.reason or "").strip() or None
    row.reviewed_by = admin.id
    row.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_response(row)
