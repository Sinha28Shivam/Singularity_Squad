"""Backend support for the React UI: explicit CORS allow-list and a non-secret payments feature flag."""
from fastapi.testclient import TestClient

from app.main import _cors_origins, app


def test_cors_allows_configured_dev_origin_only():
    c = TestClient(app)
    ok = c.options("/api/auth/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type,authorization"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in ok.headers
    bad = c.options("/api/auth/login", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers
    assert c.get("/api/health", headers={"Origin": "http://evil.example"}).headers.get("access-control-allow-origin") is None


def test_cors_wildcard_is_ignored(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*, https://ui.example.com/ ,")
    assert _cors_origins() == ["https://ui.example.com"]


def test_health_exposes_payments_flag_only(monkeypatch):
    c = TestClient(app)
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    assert c.get("/api/health").json()["payments_simulation_enabled"] is False
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    body = c.get("/api/health").json()
    assert body["payments_simulation_enabled"] is True
    assert not any(k.startswith("PAYMENT_") or "key" in k.lower() for k in body)
