"""/api/admin/ai/* - admin-only AI operations view (plan D3).

Telemetry is the redacted, EPHEMERAL in-process ring buffer of ``app.ai_security.telemetry`` (reset on restart;
the durable copy is the ``ai_event`` structured log line). Task listings expose contract metadata only - never
system prompts, prompt text or output schemas' internals.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.ai_security import telemetry
from app.ai_security.contracts import TASKS
from app.ai_security.redaction import redact_mapping
from app.auth.deps import require_admin
from app.portal_schemas import UserPublic

router = APIRouter(prefix="/api/admin/ai", tags=["admin ai ops"])


@router.get("/telemetry/summary")
def telemetry_summary(_: UserPublic = Depends(require_admin)) -> dict:
    return telemetry.summary()


@router.get("/telemetry/events")
def telemetry_events(limit: int = Query(50, ge=1, le=200), _: UserPublic = Depends(require_admin)) -> dict:
    # Events are redacted at record time; redact again defensively before leaving the process.
    events = [redact_mapping(dict(e)) for e in telemetry.recent(limit)]
    return {"ephemeral": True, "count": len(events), "events": events}


@router.get("/tasks")
def ai_tasks(_: UserPublic = Depends(require_admin)) -> dict:
    tasks = [{"name": c.name, "version": c.version, "prompt_version": c.prompt_version, "mode": c.mode,
              "enabled": c.enabled, "max_input_tokens": c.max_input_tokens,
              "max_output_tokens": c.max_output_tokens, "sensitive_content_allowed": c.sensitive_content_allowed,
              "fallback": c.fallback}
             for c in sorted(TASKS.values(), key=lambda c: c.name)]
    return {"tasks": tasks}
