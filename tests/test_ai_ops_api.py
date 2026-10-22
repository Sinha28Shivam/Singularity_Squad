"""Admin-only AI ops endpoints (plan D3): telemetry summary/events and the task-contract listing."""
import json

import pytest
from fastapi.testclient import TestClient

from app.ai_security import telemetry
from app.ai_security.contracts import TASKS
from app.main import app

ENDPOINTS = ("/api/admin/ai/telemetry/summary", "/api/admin/ai/telemetry/events?limit=10", "/api/admin/ai/tasks")
FAKE_KEY = "sk-abcdefghijklmnop12345"


@pytest.fixture
def api():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    telemetry.reset()
    yield
    telemetry.reset()


@pytest.mark.parametrize("path", ENDPOINTS)
def test_admin_can_read(api, admin_headers, path):
    r = api.get(path, headers=admin_headers)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ENDPOINTS)
def test_anonymous_is_401(api, path):
    assert api.get(path).status_code == 401


@pytest.mark.parametrize("path", ENDPOINTS)
def test_normal_user_is_403(api, user_headers_factory, path):
    assert api.get(path, headers=user_headers_factory("alice")).status_code == 403


@pytest.mark.parametrize("path", ENDPOINTS)
def test_approver_is_403(api, approver_headers_factory, path):
    assert api.get(path, headers=approver_headers_factory("carol")).status_code == 403


def test_events_limit_validated(api, admin_headers):
    assert api.get("/api/admin/ai/telemetry/events?limit=0", headers=admin_headers).status_code == 422
    assert api.get("/api/admin/ai/telemetry/events?limit=201", headers=admin_headers).status_code == 422
    assert api.get("/api/admin/ai/telemetry/events?limit=200", headers=admin_headers).status_code == 200


def test_summary_and_events_are_redacted(api, admin_headers):
    telemetry.record({"kind": "llm_call", "task_name": "policy_result_summary", "outcome": "error",
                      "error_category": f"auth failed for key {FAKE_KEY}", "latency_ms": 12,
                      "prompt": "SYSTEM PROMPT: secret instructions", "text": "document body"})
    telemetry.record({"kind": "retrieval", "retrieval_mode": "hybrid_local_hash"})
    summary = api.get("/api/admin/ai/telemetry/summary", headers=admin_headers)
    events = api.get("/api/admin/ai/telemetry/events?limit=50", headers=admin_headers)
    assert summary.status_code == events.status_code == 200
    body = summary.text + events.text
    assert FAKE_KEY not in body and "abcdefghijklmnop" not in body
    assert "secret instructions" not in body and "document body" not in body
    data = events.json()
    assert data["ephemeral"] is True and data["count"] == 2
    s = summary.json()
    assert s["ephemeral"] is True and s["retrieval_modes"] == {"hybrid_local_hash": 1}
    assert s["llm_tasks"]["policy_result_summary"]["calls"] == 1


def test_tasks_list_has_metadata_but_no_prompts(api, admin_headers):
    r = api.get("/api/admin/ai/tasks", headers=admin_headers)
    tasks = r.json()["tasks"]
    assert {t["name"] for t in tasks} == set(TASKS)
    expected_keys = {"name", "version", "prompt_version", "mode", "enabled", "max_input_tokens",
                     "max_output_tokens", "sensitive_content_allowed", "fallback"}
    assert all(set(t) == expected_keys for t in tasks)
    text = json.dumps(tasks).lower()
    assert "system_prompt" not in text and "you are" not in text and "output_model" not in text
