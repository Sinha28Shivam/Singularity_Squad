"""Streamlit UI tests for loading saved tickets (HTTP calls are mocked)."""
import json
import os
import pathlib
from datetime import date
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit.testing.v1")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = str(ROOT / "ui" / "streamlit_app.py")
TICKET_ID = "REQ-20261017-AAAAAAAA"
HEALTH = {"status": "ok", "llm_enabled": False, "model": "gpt-4o-mini",
          "database": "connected", "persistence": "required"}
TICKETS = {"tickets": [{
    "ticket_id": TICKET_ID, "status": "pass", "final_status": "pass",
    "created_at": "2026-10-17T04:00:00+00:00", "updated_at": "2026-10-17T04:00:01+00:00",
}]}


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("No JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _workflow_result(persisted: bool) -> dict:
    os.environ["WORKPLACEOPS_OFFLINE"] = "1"
    from app.orchestrator import run_workflow
    samples = json.loads((ROOT / "data" / "sample_requests.json").read_text(encoding="utf-8"))
    wr = run_workflow(samples["happy_path"], date(2026, 10, 17), "INV-5001",
                      request_id=TICKET_ID).model_dump(mode="json")
    wr["ticket_id"] = TICKET_ID
    wr["persisted"] = persisted
    return wr


def _ticket(persisted: bool) -> dict:
    return {"ticket_id": TICKET_ID, "status": "pass", "schema_version": 1,
            "created_at": "2026-10-17T04:00:00+00:00", "updated_at": "2026-10-17T04:00:01+00:00",
            "workflow_result": _workflow_result(persisted)}


def make_get(ticket_response: FakeResponse):
    samples = json.loads((ROOT / "data" / "sample_requests.json").read_text(encoding="utf-8"))

    def fake_get(url, params=None, timeout=None, **kwargs):
        path = url.split("://", 1)[-1]
        path = path[path.find("/"):] if "/" in path else "/"
        if path == "/api/health":
            return FakeResponse(200, HEALTH)
        if path == "/api/samples":
            return FakeResponse(200, samples)
        if path == "/api/invoices":
            return FakeResponse(200, [])
        if path == "/api/tickets":
            return FakeResponse(200, TICKETS)
        if path == f"/api/tickets/{TICKET_ID}":
            return ticket_response
        return FakeResponse(404, {"detail": "Not found"})

    return fake_get


def fake_post(*args, **kwargs):
    raise AssertionError("requests.post should not be called in these tests")


@pytest.fixture(autouse=True)
def _clear_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _run_load(ticket_response: FakeResponse) -> AppTest:
    with patch("requests.get", side_effect=make_get(ticket_response)), \
            patch("requests.post", side_effect=fake_post):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        assert not at.exception
        at.text_input(key="ticket_id_input").input(TICKET_ID)
        at.run()
        load_btn = next(b for b in at.button if b.label == "📂 Load ticket")
        load_btn.click()
        at.run()
    return at


def _all_texts(at: AppTest) -> list[str]:
    out = []
    for coll in (at.error, at.warning, at.success, at.info, at.markdown, at.caption):
        out.extend(str(e.value) for e in coll)
    return out


def test_load_persisted_ticket():
    at = _run_load(FakeResponse(200, _ticket(persisted=True)))
    assert not at.exception
    result = at.session_state["result"]
    assert result["persisted"] is True
    assert result["ticket_id"] == TICKET_ID
    successes = [str(s.value) for s in at.success]
    assert any("Loaded ticket" in s for s in successes)
    assert any("Loaded from database" in s for s in successes)


def test_load_unpersisted_ticket_keeps_stored_flag():
    at = _run_load(FakeResponse(200, _ticket(persisted=False)))
    assert not at.exception
    assert at.session_state["result"]["persisted"] is False
    assert at.session_state["result"]["ticket_id"] == TICKET_ID


def test_load_ticket_404_shows_not_found():
    at = _run_load(FakeResponse(404, {"detail": f"Ticket {TICKET_ID} not found."}))
    assert not at.exception
    errors = [str(e.value) for e in at.error]
    assert any("not found" in e.lower() for e in errors)
    assert "result" not in at.session_state


def test_load_ticket_503_shows_detail_without_traceback():
    detail = "Database unavailable. Please try again later."
    at = _run_load(FakeResponse(503, {"detail": detail}))
    assert not at.exception
    errors = [str(e.value) for e in at.error]
    assert any(detail in e for e in errors)
    assert not any("Traceback" in t for t in _all_texts(at))


def test_recent_tickets_selectbox_without_raw_text():
    with patch("requests.get", side_effect=make_get(FakeResponse(200, _ticket(True)))), \
            patch("requests.post", side_effect=fake_post):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        assert not at.exception
        sb = at.selectbox(key="recent_ticket")
        assert any(TICKET_ID in str(o) for o in sb.options)
        sb.select(TICKET_ID)
        at.run()
        assert not at.exception
        assert at.text_input(key="ticket_id_input").value == TICKET_ID
