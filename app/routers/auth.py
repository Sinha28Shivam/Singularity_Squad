"""/api/auth/* routes: signup, login, logout, me."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Response

from app.auth import service
from app.auth.deps import bearer_token, require_user
from app.portal_schemas import AuthOut, LoginIn, SignupIn, UserPublic

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=AuthOut, status_code=201)
def signup(body: SignupIn) -> AuthOut:
    return service.signup_and_login(body)


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn) -> AuthOut:
    return service.login(body)


@router.post("/logout", status_code=204, response_class=Response)
def logout(_: UserPublic = Depends(require_user), authorization: Optional[str] = Header(None)) -> Response:
    service.logout(bearer_token(authorization) or "")
    return Response(status_code=204)


@router.get("/me", response_model=UserPublic)
def me(user: UserPublic = Depends(require_user)) -> UserPublic:
    return user
