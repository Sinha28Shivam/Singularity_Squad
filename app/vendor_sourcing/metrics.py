"""In-process, thread-safe counters for vendor sourcing operability (plan P2.16).

Counters are process-local (reset on restart) and are intended for admin diagnostics only.
Known names: candidates_submitted, decisions_by_type, rfqs_created, quotes_by_type,
verifications, ineligible_reasons, comparisons, persistence_failures.
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[str, int]] = {}
_TOTAL = "_total"


def inc(name: str, label: str | None = None) -> None:
    """Increment counter ``name`` (and its ``label`` bucket when given). Never raises."""
    try:
        key = str(name)
        with _LOCK:
            bucket = _COUNTERS.setdefault(key, {})
            bucket[_TOTAL] = bucket.get(_TOTAL, 0) + 1
            if label is not None:
                lab = str(label)
                bucket[lab] = bucket.get(lab, 0) + 1
    except Exception:  # pragma: no cover - metrics must never break callers
        pass


def snapshot() -> dict[str, Any]:
    """Return ``{name: {"total": n, "by_label": {label: n}}}`` (a deep copy)."""
    with _LOCK:
        out: dict[str, Any] = {}
        for name, bucket in _COUNTERS.items():
            out[name] = {
                "total": bucket.get(_TOTAL, 0),
                "by_label": {k: v for k, v in bucket.items() if k != _TOTAL},
            }
        return out


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
