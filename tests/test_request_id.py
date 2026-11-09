"""X-Request-ID correlation middleware (app/middleware.py)."""
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import REQUEST_ID_HEADER, RequestIDMiddleware, get_request_id, normalize_request_id

UUID_HEX = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def api():
    from app.main import app
    return TestClient(app)  # no lifespan: keeps the per-test mongomock client open (see conftest)


@pytest.fixture
def probe():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/rid")
    def rid():
        return {"request_id": get_request_id()}

    return TestClient(app)


def test_generates_id_when_missing(api):
    r = api.get("/api/health")
    assert r.status_code == 200
    assert UUID_HEX.match(r.headers[REQUEST_ID_HEADER])


def test_ids_are_unique_per_request(api):
    ids = {api.get("/api/health").headers[REQUEST_ID_HEADER] for _ in range(3)}
    assert len(ids) == 3


@pytest.mark.parametrize("value", ["abc-123", "trace.ID_42", "a" * 64])
def test_valid_inbound_id_is_echoed(api, value):
    r = api.get("/api/health", headers={REQUEST_ID_HEADER: value})
    assert r.headers[REQUEST_ID_HEADER] == value


@pytest.mark.parametrize("value", ["a" * 65, "bad id", "semi;colon", "<script>", "a/b"])
def test_invalid_inbound_id_is_replaced(api, value):
    r = api.get("/api/health", headers={REQUEST_ID_HEADER: value})
    assert r.headers[REQUEST_ID_HEADER] != value
    assert UUID_HEX.match(r.headers[REQUEST_ID_HEADER])


def test_header_name_is_case_insensitive(api):
    r = api.get("/api/health", headers={"x-request-id": "lower-case-ok"})
    assert r.headers[REQUEST_ID_HEADER] == "lower-case-ok"


def test_error_responses_carry_id(api):
    r = api.get("/api/does-not-exist", headers={REQUEST_ID_HEADER: "err-1"})
    assert r.status_code == 404
    assert r.headers[REQUEST_ID_HEADER] == "err-1"


def test_cors_preflight_carries_id(api):
    r = api.options("/api/health", headers={"Origin": "http://localhost:5173",
                                              "Access-Control-Request-Method": "GET",
                                              REQUEST_ID_HEADER: "pre-1"})
    assert r.headers[REQUEST_ID_HEADER] == "pre-1"


def test_contextvar_matches_header_and_resets(probe):
    r = probe.get("/rid", headers={REQUEST_ID_HEADER: "ctx-1"})
    assert r.json() == {"request_id": "ctx-1"}
    assert r.headers[REQUEST_ID_HEADER] == "ctx-1"
    generated = probe.get("/rid")
    assert generated.json()["request_id"] == generated.headers[REQUEST_ID_HEADER]
    assert get_request_id() is None


def test_single_header_value(probe):
    r = probe.get("/rid", headers={REQUEST_ID_HEADER: "one"})
    assert r.headers.get_list(REQUEST_ID_HEADER) == ["one"]


@pytest.mark.parametrize("value", [None, "", "x" * 65, "\u00e9", "a b"])
def test_normalize_rejects_unsafe_values(value):
    assert UUID_HEX.match(normalize_request_id(value))


def test_normalize_keeps_safe_value():
    assert normalize_request_id("Req-1.2_3") == "Req-1.2_3"
