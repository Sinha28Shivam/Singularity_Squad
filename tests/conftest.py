import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import os

import pytest


@pytest.fixture(autouse=True)
def _offline_mongomock(request, monkeypatch, tmp_path_factory):
    """Run offline and give each test a fresh in-memory MongoDB (unless marked real_mongo).

    Policy RAG is disabled by default (POLICY_RAG_ENABLED=0, POLICY_RAG_SEED=0) so pre-RAG behaviour is
    unchanged; policy tests opt in with monkeypatch.setenv("POLICY_RAG_ENABLED", "1"). The Phase 3 rule engine
    is disabled too (POLICY_RULES_ENABLED=0, POLICY_RULES_BOOTSTRAP=0) unless a test opts in. PDFs are stored
    in a per-test temporary directory."""
    os.environ["WORKPLACEOPS_OFFLINE"] = "1"
    monkeypatch.setenv("POLICY_RAG_ENABLED", "0")
    monkeypatch.setenv("POLICY_RAG_SEED", "0")
    monkeypatch.setenv("POLICY_RULES_ENABLED", "0")
    monkeypatch.setenv("POLICY_RULES_BOOTSTRAP", "0")
    monkeypatch.setenv("POLICY_STORE_DIR", str(tmp_path_factory.mktemp("policy_store")))
    if request.node.get_closest_marker("real_mongo"):
        yield
        return
    try:
        import mongomock
        from app.db.mongo import close_client, set_client
    except ImportError:
        yield
        return
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "required")
    set_client(mongomock.MongoClient(tz_aware=True))
    try:
        yield
    finally:
        close_client()


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-pass-123"
USER_PASSWORD = "user-pass-123"


def _auth_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)  # no lifespan: keeps the per-test mongomock client open


@pytest.fixture
def admin_headers(monkeypatch):
    """Seed the admin from env (as the app does at startup) and return bearer headers."""
    from app.auth.service import seed_admin_from_env

    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    assert seed_admin_from_env() is True
    r = _auth_client().post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def user_headers_factory():
    """Callable(username="alice") -> bearer headers for a freshly signed-up normal user."""
    c = _auth_client()

    def make(username: str = "alice") -> dict:
        r = c.post("/api/auth/signup", json={"username": username, "name": username.title(),
                                             "email": f"{username}@example.com", "password": USER_PASSWORD})
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    return make


APPROVER_PASSWORD = "approver-pass-123"  # test-only credential


@pytest.fixture
def approver_headers_factory(monkeypatch):
    """Callable(username="carol") -> bearer headers for a server-seeded approver (APPROVER_ACCOUNTS).

    Every call re-seeds all approvers requested so far, like the app does at startup."""
    from app.auth.service import seed_approvers_from_env

    c = _auth_client()
    names: list[str] = []

    def make(username: str = "carol") -> dict:
        if username not in names:
            names.append(username)
        monkeypatch.setenv("APPROVER_ACCOUNTS", ",".join(f"{n}:{APPROVER_PASSWORD}" for n in names))
        assert seed_approvers_from_env() == len(names)
        r = c.post("/api/auth/login", json={"username": username, "password": APPROVER_PASSWORD})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "approver"
        return {"Authorization": f"Bearer {r.json()['token']}"}

    return make


@pytest.fixture
def client():
    """TestClient with app lifespan (startup indexes / admin seeding). Modules may override it."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
