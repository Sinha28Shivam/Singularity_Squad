"""Safe LLM/retrieval usage telemetry (plan B3/D3).

Events hold operational metadata only (task, prompt version, model, tokenizer, token estimates vs provider
usage, latency, outcome, safe error category, cost estimate). Prompts, document text and secrets are never
recorded. Events are kept in a bounded in-process ring buffer (``ephemeral: true`` - reset on restart) and
emitted as a structured log line so a log pipeline can persist them.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.ai_security.redaction import redact_mapping

logger = logging.getLogger("workplaceops.ai_telemetry")

CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "ai_guardrails_config.json"
_lock = threading.Lock()
_events: deque = deque(maxlen=2000)


def load_guardrails_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def estimate_cost(model: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[dict]:
    """Cost from versioned configured rates; ``None`` when no rate is configured for the model."""
    rates = load_guardrails_config().get("cost_rates") or {}
    rate = (rates.get("per_million_tokens") or {}).get(model or "")
    if not rate or input_tokens is None:
        return None
    total = (Decimal(str(rate.get("input", 0))) * input_tokens
             + Decimal(str(rate.get("output", 0))) * (output_tokens or 0)) / Decimal(1_000_000)
    return {"amount": str(total.quantize(Decimal("0.000001"))), "currency": rates.get("currency", "USD"),
            "rates_version": rates.get("rates_version")}


def record(event: dict[str, Any]) -> dict:
    safe = redact_mapping(dict(event))
    safe.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with _lock:
        _events.append(safe)
    logger.info("ai_event %s", json.dumps(safe, sort_keys=True, default=str))
    return safe


def recent(limit: int = 100) -> list[dict]:
    with _lock:
        return list(_events)[-max(1, min(int(limit), len(_events) or 1)):] if _events else []


def summary() -> dict:
    with _lock:
        events = list(_events)
    by_task: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("kind") != "llm_call":
            continue
        t = by_task.setdefault(e.get("task_name") or "unknown", {
            "calls": 0, "outcomes": Counter(), "error_categories": Counter(), "estimated_input_tokens": 0,
            "actual_input_tokens": 0, "actual_output_tokens": 0, "latency_ms_total": 0.0, "evidence_reduced": 0})
        t["calls"] += 1
        t["outcomes"][e.get("outcome") or "unknown"] += 1
        if e.get("error_category"):
            t["error_categories"][e["error_category"]] += 1
        t["estimated_input_tokens"] += int(e.get("estimated_input_tokens") or 0)
        t["actual_input_tokens"] += int(e.get("actual_input_tokens") or 0)
        t["actual_output_tokens"] += int(e.get("actual_output_tokens") or 0)
        t["latency_ms_total"] += float(e.get("latency_ms") or 0)
        t["evidence_reduced"] += 1 if e.get("evidence_reduced") else 0
    for t in by_task.values():
        t["outcomes"], t["error_categories"] = dict(t["outcomes"]), dict(t["error_categories"])
        t["avg_latency_ms"] = round(t.pop("latency_ms_total") / t["calls"], 2) if t["calls"] else None
    retrieval = Counter(e.get("retrieval_mode") for e in events if e.get("kind") == "retrieval")
    injection = sum(1 for e in events if e.get("kind") == "injection_signal")
    return {"ephemeral": True, "window_events": len(events), "llm_tasks": by_task,
            "retrieval_modes": dict(retrieval), "injection_signals": injection}


def reset() -> None:
    with _lock:
        _events.clear()
