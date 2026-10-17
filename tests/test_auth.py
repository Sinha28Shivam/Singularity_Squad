"""Auth: signup/login/logout/me, sessions, admin seeding, role dependencies."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.deps import require_admin
from app.auth.passwords import hash_password, verify_password
from app.auth.service import INVALID_LOGIN_MSG, seed_admin_from_env
from app.db import ensure_indexes, get_database
from app.main import app
from app.portal_schemas import UserPublic

ADMIN_USER, ADMIN_PASS = "admin", "admin-pass-123"
SECRET_KEYS = ("password_hash", "salt", "token_hash")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASS)


def _signup(client, username="alice", email=None, password="s3cret-pass", **extra):
    body = {"username": username, "name": username.title(), "email": email or f"{username}@example.com",
            "password": password, **extra}
    return client.post("/api/auth/signup", json=body)


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _no_secrets(resp):
    for key in SECRET_KEYS:
        assert key not in resp.text


def test_signup_returns_token_usable_on_me(client):
    r = _signup(client, username="Alice", email="Alice@Example.com")
    assert r.status_code == 201
    body = r.json()
    assert body["user"] == {"username": "alice", "name": "Alice", "email": "alice@example.com", "role": "user"}
    assert body["token"] and body["expires_at"]
    _no_secrets(r)
    me = client.get("/api/auth/me", headers=_hdr(body["token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    _no_secrets(me)


def test_duplicate_username_and_email_conflict_case_insensitive(client):
    assert _signup(client, "bob", "bob@example.com").status_code == 201
    r1 = _signup(client, "BOB", "other@example.com")
    r2 = _signup(client, "robert", "BOB@EXAMPLE.COM")
    assert r1.status_code == 409 and r2.status_code == 409
    assert r1.json()["detail"] == "Username or email already registered."


@pytest.mark.parametrize("override", [
    {"password": "short"},
    {"email": "not-an-email"},
    {"username": "ab"},
    {"username": "bad name!"},
])
def test_signup_validation_422(client, override):
    body = {"username": "carol", "name": "Carol", "email": "carol@example.com", "password": "s3cret-pass"}
    body.update(override)
    assert client.post("/api/auth/signup", json=body).status_code == 422


def test_signup_ignores_role_field(client):
    r = _signup(client, "mallory", role="admin")
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "user"
    assert get_database().users.find_one({"username": "mallory"})["role"] == "user"


def test_login_ok_and_generic_failures(client):
    _signup(client, "dave", password="dave-pass-1")
    ok = _login(client, "DAVE", "dave-pass-1")
    assert ok.status_code == 200
    assert ok.json()["user"]["username"] == "dave"
    _no_secrets(ok)
    wrong = _login(client, "dave", "nope-nope")
    unknown = _login(client, "nobody", "nope-nope")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": INVALID_LOGIN_MSG}


def test_missing_garbage_and_expired_tokens_401(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated."}
    assert r.headers.get("www-authenticate") == "Bearer"
    assert client.get("/api/auth/me", headers=_hdr("garbage")).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401

    token = _signup(client, "erin").json()["token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"bearer {token}"}).status_code == 200
    get_database().sessions.update_many({}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401


def test_sessions_store_only_token_hash(client):
    token = _signup(client, "frank").json()["token"]
    sessions = list(get_database().sessions.find({}, {"_id": 0}))
    assert len(sessions) == 1
    assert token not in str(sessions)
    assert set(sessions[0]) == {"token_hash", "user", "role", "created_at", "expires_at"}


def test_logout_invalidates_token(client):
    token = _signup(client, "gina").json()["token"]
    r = client.post("/api/auth/logout", headers=_hdr(token))
    assert r.status_code == 204
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 401


def test_admin_seeding_and_login(client, admin_env):
    assert seed_admin_from_env() is True
    doc = get_database().users.find_one({"username": ADMIN_USER})
    assert doc["role"] == "admin"
    r = _login(client, ADMIN_USER, ADMIN_PASS)
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"


def test_reseed_updates_password(client, admin_env, monkeypatch):
    assert seed_admin_from_env()
    monkeypatch.setenv("ADMIN_PASSWORD", "new-admin-pass-456")
    assert seed_admin_from_env()
    assert _login(client, ADMIN_USER, ADMIN_PASS).status_code == 401
    assert _login(client, ADMIN_USER, "new-admin-pass-456").status_code == 200
    assert get_database().users.count_documents({"username": ADMIN_USER}) == 1


def test_seeding_does_not_escalate_existing_user(client, admin_env):
    get_database().users.delete_many({})  # startup may already have seeded an admin from .env
    assert _signup(client, ADMIN_USER, password="user-pass-999").status_code == 201
    assert seed_admin_from_env() is False
    assert get_database().users.find_one({"username": ADMIN_USER})["role"] == "user"
    assert _login(client, ADMIN_USER, ADMIN_PASS).status_code == 401


def test_seeding_without_env_returns_false(monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert seed_admin_from_env() is False
    assert get_database().users.count_documents({}) == 0


def test_password_hashing():
    h1, s1 = hash_password("same-password")
    h2, s2 = hash_password("same-password")
    assert h1 != "same-password" and (h1, s1) != (h2, s2)
    assert verify_password("same-password", h1, s1)
    assert not verify_password("other-password", h1, s1)


def test_stored_password_is_hashed(client):
    _signup(client, "hank", password="hank-pass-1")
    doc = get_database().users.find_one({"username": "hank"})
    assert doc["password_hash"] != "hank-pass-1"
    assert verify_password("hank-pass-1", doc["password_hash"], doc["salt"])


def test_indexes_created():
    ensure_indexes()
    db = get_database()
    assert {"username_unique", "email_unique"} <= set(db.users.index_information())
    assert {"token_hash_unique", "expires_at_ttl"} <= set(db.sessions.index_information())


def test_require_admin_dependency(client, admin_env):
    from app.main import _auth_error_handler
    from app.auth.errors import AuthError

    router = APIRouter()

    @router.get("/admin-only")
    def admin_only(user: UserPublic = Depends(require_admin)):
        return {"ok": True, "user": user.username}

    mini = FastAPI()
    mini.add_exception_handler(AuthError, _auth_error_handler)
    mini.include_router(router)

    user_token = _signup(client, "ivan").json()["token"]
    seed_admin_from_env()
    admin_token = _login(client, ADMIN_USER, ADMIN_PASS).json()["token"]
    with TestClient(mini) as mc:
        assert mc.get("/admin-only").status_code == 401
        r = mc.get("/admin-only", headers=_hdr(user_token))
        assert r.status_code == 403 and r.json() == {"detail": "Admin access required."}
        r = mc.get("/admin-only", headers=_hdr(admin_token))
        assert r.status_code == 200 and r.json()["user"] == ADMIN_USER
