"""FastAPI dependencies for bearer-token authentication and role checks."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header

from app.auth import service
from app.auth.errors import AuthError
from app.portal_schemas import UserPublic

NOT_AUTHENTICATED = "Not authenticated."
ADMIN_REQUIRED = "Admin access required."
APPROVER_REQUIRED = "Approver access required."
APPROVER_OR_ADMIN_REQUIRED = "Approver or admin access required."


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def current_user(authorization: Optional[str] = Header(None)) -> UserPublic:
    user = service.user_for_token(bearer_token(authorization))
    if user is None:
        raise AuthError(401, NOT_AUTHENTICATED)
    return user


def require_admin(user: UserPublic = Depends(current_user)) -> UserPublic:
    if user.role != "admin":
        raise AuthError(403, ADMIN_REQUIRED)
    return user


def require_approver(user: UserPublic = Depends(current_user)) -> UserPublic:
    if user.role != "approver":
        raise AuthError(403, APPROVER_REQUIRED)
    return user


def require_approver_or_admin(user: UserPublic = Depends(current_user)) -> UserPublic:
    if user.role not in ("approver", "admin"):
        raise AuthError(403, APPROVER_OR_ADMIN_REQUIRED)
    return user


def require_user(user: UserPublic = Depends(current_user)) -> UserPublic:
    return user
