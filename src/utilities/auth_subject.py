"""Resolve a JWT subject to the Autobus user id."""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.user.model.User import User


def resolve_internal_user_id(db: Session, jwt_subject: str) -> str:
    """
    JWT `sub` is the user's email at login (`AuthService.signin`).
    Some routes may use the internal id as `sub`; support both.
    """
    user = db.query(User).filter(User.email == jwt_subject).first()
    if user:
        return user.id
    user = db.query(User).filter(User.id == jwt_subject).first()
    if user:
        return user.id
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authenticated subject does not match a user",
    )
