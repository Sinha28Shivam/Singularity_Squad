"""Signup / login / logout / session lookup and admin seeding.

Never logs or returns passwords, password hashes, salts, tokens or token hashes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.auth.errors import AuthError
from app.auth.passwords import hash_password, verify_password
from app.portal_schemas import AuthOut, LoginIn, SignupIn, UserPublic
from app.repositories import session_repository, user_repository

logger = logging.getLogger(__name__)

DUPLICATE_MSG = "Username or email already registered."
INVALID_LOGIN_MSG = "Invalid username or password."
DEFAULT_ADMIN_NAME = "Operations Admin"
DEFAULT_ADMIN_EMAIL = "admin@workplaceops.local"
_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")

# Used to spend comparable time when the username is unknown (reduces user enumeration via timing).
_DUMMY_HASH, _DUMMY_SALT = hash_password(secrets.token_urlsafe(16))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_ttl() -> timedelta:
    try:
        hours = float(os.getenv("SESSION_TTL_HOURS", "8"))
    except ValueError:
        hours = 8.0
    if hours <= 0:
        hours = 8.0
    return timedelta(hours=hours)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _public(doc: dict) -> UserPublic:
    return UserPublic(username=doc["username"], name=doc.get("name") or doc["username"],
                      email=doc.get("email") or "", role=doc.get("role", "user"))


def _start_session(doc: dict) -> AuthOut:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + _session_ttl()
    session_repository.create_session(_token_hash(token), doc["username"], doc.get("role", "user"), expires_at)
    return AuthOut(token=token, expires_at=expires_at, user=_public(doc))


def signup(data: SignupIn) -> UserPublic:
    """Create a normal user (role is always "user")."""
    username = data.username.strip().lower()
    email = data.email.strip().lower()
    if user_repository.get_user_by_username(username) or user_repository.get_user_by_email(email):
        raise AuthError(409, DUPLICATE_MSG)
    password_hash, salt = hash_password(data.password)
    doc = {"username": username, "email": email, "name": data.name, "role": "user",
           "password_hash": password_hash, "salt": salt}
    if not user_repository.create_user(doc):
        raise AuthError(409, DUPLICATE_MSG)
    return _public(doc)


def signup_and_login(data: SignupIn) -> AuthOut:
    signup(data)
    return login(LoginIn(username=data.username, password=data.password))


def login(data: LoginIn) -> AuthOut:
    doc = user_repository.get_user_by_username(data.username)
    if doc is None:
        verify_password(data.password, _DUMMY_HASH, _DUMMY_SALT)
        raise AuthError(401, INVALID_LOGIN_MSG)
    if not verify_password(data.password, doc.get("password_hash", ""), doc.get("salt", "")):
        raise AuthError(401, INVALID_LOGIN_MSG)
    return _start_session(doc)


def logout(token: str) -> None:
    if token:
        session_repository.delete_session(_token_hash(token))


def user_for_token(token: Optional[str]) -> Optional[UserPublic]:
    if not token:
        return None
    token_hash = _token_hash(token)
    session = session_repository.get_session(token_hash)
    if session is None:
        return None
    expires_at = session.get("expires_at")
    if not isinstance(expires_at, datetime) or _as_utc(expires_at) <= _now():
        session_repository.delete_session(token_hash)
        return None
    doc = user_repository.get_user_by_username(session.get("user", ""))
    if doc is None:
        return None
    return _public(doc)


def seed_admin_from_env() -> bool:
    """Create/refresh the admin from ADMIN_USERNAME / ADMIN_PASSWORD. Returns True if seeded."""
    username = (os.getenv("ADMIN_USERNAME") or "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if not username or not password:
        logger.warning("ADMIN_USERNAME/ADMIN_PASSWORD not set; no admin account was created.")
        return False
    name = (os.getenv("ADMIN_NAME") or "").strip() or DEFAULT_ADMIN_NAME
    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower() or DEFAULT_ADMIN_EMAIL

    existing = user_repository.get_user_by_username(username)
    if existing is not None and existing.get("role") != "admin":
        logger.warning("ADMIN_USERNAME matches an existing non-admin user; refusing to escalate it.")
        return False
    by_email = user_repository.get_user_by_email(email)
    if by_email is not None and by_email.get("username") != username:
        logger.warning("ADMIN_EMAIL is already used by another account; admin was not seeded.")
        return False

    password_hash, salt = hash_password(password)
    user_repository.upsert_admin(username, name, email, password_hash, salt)
    logger.info("Admin account seeded/refreshed from environment.")
    return True


def _parse_approver_accounts(raw: str) -> list[tuple[str, str]]:
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        username, sep, password = entry.partition(":")
        username = username.strip().lower()
        if not sep or not username or not password or not _USERNAME_RE.match(username):
            logger.warning("Ignoring a malformed APPROVER_ACCOUNTS entry.")
            continue
        out.append((username, password))
    return out


def seed_approvers_from_env() -> int:
    """Create/refresh approver accounts from APPROVER_ACCOUNTS="user:pass[,user2:pass2]".

    Demo-grade, server-side configuration only (signup can never create an approver). Existing user or
    admin accounts are never escalated or changed; conflicting entries are skipped with a warning.
    Returns the number of approver accounts seeded/refreshed.
    """
    raw = os.getenv("APPROVER_ACCOUNTS") or ""
    count = 0
    for username, password in _parse_approver_accounts(raw):
        existing = user_repository.get_user_by_username(username)
        if existing is not None and existing.get("role") != "approver":
            logger.warning("An APPROVER_ACCOUNTS username matches an existing non-approver account; "
                           "refusing to change its role.")
            continue
        email = f"{username}@approvers.workplaceops.local"
        by_email = user_repository.get_user_by_email(email)
        if by_email is not None and by_email.get("username") != username:
            logger.warning("An APPROVER_ACCOUNTS email is already used by another account; skipped.")
            continue
        name = (existing or {}).get("name") or f"Approver {username}"
        password_hash, salt = hash_password(password)
        if user_repository.upsert_approver(username, name, email, password_hash, salt):
            count += 1
        else:
            logger.warning("An approver account could not be seeded (conflicting account); skipped.")
    if count:
        logger.info("%d approver account(s) seeded/refreshed from environment.", count)
    return count
