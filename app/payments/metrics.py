"""In-process counters for the payment SIMULATOR (no money is ever transferred).

Process-local by design (demo observability only); every snapshot carries ``mode="SIMULATION"``.
"""
from __future__ import annotations

import threading
from collections import Counter

_LOCK = threading.Lock()
_COUNTERS: Counter = Counter()
_BLOCKED: Counter = Counter()
_LATENCY = {"count": 0, "sum_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0}

REQUESTS = "payment_simulation_requests_total"
FAILURES = "payment_simulation_failures_total"
REPLAYS = "payment_simulation_idempotent_replays_total"
RECONCILIATION_FAILURES = "payment_simulation_audit_reconciliation_failures_total"
CREATED = "payment_simulation_created_total"
RETRIES = "payment_simulation_retries_total"


def inc(name: str, n: int = 1) -> None:
    with _LOCK:
        _COUNTERS[name] += n


def inc_blocked(reason_codes) -> None:
    with _LOCK:
        for code in reason_codes or ["UNKNOWN"]:
            _BLOCKED[str(code)] += 1


def observe_latency(ms: float) -> None:
    with _LOCK:
        _LATENCY["count"] += 1
        _LATENCY["sum_ms"] += float(ms)
        _LATENCY["last_ms"] = float(ms)
        _LATENCY["max_ms"] = max(_LATENCY["max_ms"], float(ms))


def snapshot() -> dict:
    with _LOCK:
        count = _LATENCY["count"]
        return {
            "mode": "SIMULATION",
            "money_transferred": False,
            REQUESTS: _COUNTERS[REQUESTS],
            CREATED: _COUNTERS[CREATED],
            RETRIES: _COUNTERS[RETRIES],
            FAILURES: _COUNTERS[FAILURES],
            REPLAYS: _COUNTERS[REPLAYS],
            RECONCILIATION_FAILURES: _COUNTERS[RECONCILIATION_FAILURES],
            "payment_simulation_blocked_total": dict(sorted(_BLOCKED.items())),
            "payment_simulation_latency_ms": {
                "count": count,
                "avg": round(_LATENCY["sum_ms"] / count, 3) if count else 0.0,
                "max": round(_LATENCY["max_ms"], 3),
                "last": round(_LATENCY["last_ms"], 3),
            },
        }


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _BLOCKED.clear()
        _LATENCY.update({"count": 0, "sum_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0})
