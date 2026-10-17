"""Reviewed, versioned rule registry with an active policy-set pointer (Phase 3).

Lifecycle: ``candidate``/``needs_review`` -> ``validated`` -> ``approved`` (policy reviewer) | ``rejected``;
approving a newer version marks the previous approved version ``superseded``. Only rules that are approved,
inside their effective window and listed in the active ``PolicySetVersion`` are active.

Activation (``approve``/``reject`` of an approved rule) is a compare-and-set transaction:
1. authorize, 2. validate schema + evidence + conflicts, 3. CAS the rule status, 4. supersede older versions,
5. insert an immutable policy-set version and CAS the ``policy_set_pointer``, 6. write the audit events.
If any later step (including the audit write) fails, earlier steps are compensated and the caller gets a
retryable error: success is never reported with partially applied state.

Roles: a policy reviewer is an ``admin`` or ``approver``. Requesters (``user``) are refused everywhere. The
creator of a ``manual`` candidate cannot approve it. String actors are trusted service identities;
``system:extractor`` can never approve and ``system:bootstrap`` can only approve ``legacy_config`` rules.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from pydantic import ValidationError

from app.db.errors import PersistenceError
from app.policy_rag.errors import PolicyRagError
from app.policy_rules import evaluator
from app.policy_rules.facts import canonical_hash
from app.policy_rules.schemas import (EvidenceRef, PolicyRule, PolicyRuleIn, PolicySetVersion)
from app.portal_schemas import UserPublic
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo
from app.repositories import policy_rule_repository as repo

logger = logging.getLogger(__name__)

REVIEWER_ROLES = frozenset({"admin", "approver"})
BOOTSTRAP_ACTOR = "system:bootstrap"
EXTRACTOR_ACTOR = "system:extractor"
EDITABLE_STATUSES = ("candidate", "needs_review", "validated")
TERMINAL_STATUSES = ("rejected", "superseded", "expired")
MAX_CAS_ATTEMPTS = 3
REPLAY_WAIT_SECONDS = 3.0
ACTIVATION_WAIT_SECONDS = 5.0
ORPHAN_SET_SECONDS = 30.0
RULE_ID_PATTERN = r"^RULE-[A-Z0-9-]{3,48}$"
_RULE_REF = re.compile(r"^(RULE-[A-Z0-9-]{3,48})@(\d{1,6})$")
_WS = re.compile(r"\s+")

MSG_REVIEWER_REQUIRED = "Policy reviewer (approver or admin) access required."
MSG_SEPARATION = "The creator of a manual rule candidate cannot approve it; another reviewer must approve."
MSG_NOT_FOUND = "Policy rule version not found."
MSG_STALE_SET = "The active policy set changed since you loaded it; reload and review again."
MSG_CONCURRENT = "Another policy activation happened at the same time; please retry."
MSG_AUDIT_FAILED = "Policy activation could not be recorded in the audit log; nothing was activated. Retry later."
MSG_COMMENT_REQUIRED = "A comment is required to reject a rule."


class RuleRegistryError(PolicyRagError):
    """Client-safe registry error carrying optional validation errors / conflicts."""

    def __init__(self, status_code: int, detail: str, *, errors: Optional[list[str]] = None,
                 conflicts: Optional[list[str]] = None) -> None:
        super().__init__(status_code, detail)
        self.errors = list(errors or [])
        self.conflicts = list(conflicts or [])

    def body(self) -> dict:
        out: dict[str, Any] = {"detail": self.detail}
        if self.errors:
            out["errors"] = self.errors
        if self.conflicts:
            out["conflicts"] = self.conflicts
        return out


# ------------------------------------------------------------------------------------------------ helpers
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _today() -> dt.date:
    return _now().date()


def actor_name(actor: Union[UserPublic, str, None]) -> str:
    if isinstance(actor, UserPublic):
        return actor.username
    return str(actor or "system")


def _require_reviewer(actor: Union[UserPublic, str, None]) -> str:
    if isinstance(actor, UserPublic):
        if actor.role not in REVIEWER_ROLES:
            raise RuleRegistryError(403, MSG_REVIEWER_REQUIRED)
        return actor.username
    name = actor_name(actor)
    if not name.strip():
        raise RuleRegistryError(403, MSG_REVIEWER_REQUIRED)
    return name


def rule_ref(rule: Union[PolicyRule, dict]) -> str:
    if isinstance(rule, dict):
        return f"{rule['rule_id']}@{int(rule['rule_version'])}"
    return f"{rule.rule_id}@{rule.rule_version}"


def parse_ref(ref: str) -> Optional[tuple[str, int]]:
    m = _RULE_REF.match(ref or "")
    return (m.group(1), int(m.group(2))) if m else None


def to_model(doc: Union[dict, PolicyRule]) -> PolicyRule:
    if isinstance(doc, PolicyRule):
        return doc
    return PolicyRule.model_validate({k: v for k, v in doc.items() if k in PolicyRule.model_fields})


def _dump(rule: PolicyRule) -> dict:
    return rule.model_dump(by_alias=True)


def semantic_hash(rule: Union[PolicyRule, dict]) -> str:
    """Hash of what the rule *does* (scope + condition + decision + reason code), ignoring wording/provenance."""
    m = to_model(rule)
    data = m.model_dump(mode="json", by_alias=True, include={"applies_when", "condition"})
    data["decision"] = m.outcome.decision
    data["reason_code"] = m.outcome.reason_code
    return canonical_hash(data)


def _scope_hash(rule: PolicyRule) -> str:
    return canonical_hash(rule.model_dump(mode="json", by_alias=True, include={"applies_when", "condition"}))


def _norm(text: Optional[str]) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def _windows_overlap(a: PolicyRule, b: PolicyRule) -> bool:
    a_from, a_to = a.effective_from or dt.date.min, a.effective_until or dt.date.max
    b_from, b_to = b.effective_from or dt.date.min, b.effective_until or dt.date.max
    return a_from <= b_to and b_from <= a_to


def _money_currencies(expr: Any, out: dict[str, set[str]]) -> None:
    if expr is None:
        return
    data = expr if isinstance(expr, dict) else expr.model_dump(by_alias=True)
    if "field" in data:
        if data.get("value_type") == "money" and data.get("currency"):
            out.setdefault(data["field"], set()).add(data["currency"])
        return
    for key in ("all", "any"):
        for child in data.get(key) or []:
            _money_currencies(child, out)
    if data.get("not") is not None:
        _money_currencies(data["not"], out)


def is_effective(rule: PolicyRule, as_of: dt.date) -> bool:
    if rule.effective_from and as_of < rule.effective_from:
        return False
    if rule.effective_until and as_of > rule.effective_until:
        return False
    return True


# ------------------------------------------------------------------------------------- evidence validation
def evidence_ref_errors(ref: Union[EvidenceRef, dict], index: int = 0) -> list[str]:
    """Server-side provenance check: the ref must point to a real chunk of a ready (authoritative) document."""
    if not isinstance(ref, EvidenceRef):
        try:
            ref = EvidenceRef.model_validate(ref)
        except ValidationError:
            return [f"evidence[{index}]: malformed evidence reference"]
    where = f"evidence[{index}]"
    doc = doc_repo.get_document(ref.document_id)
    if doc is None:
        return [f"{where}: document {ref.document_id} not found"]
    errs: list[str] = []
    status = doc.get("status")
    if status != "ready":
        errs.append(f"{where}: document {ref.document_id} is '{status}' and not authoritative")
    if doc.get("content_hash") != ref.content_hash:
        errs.append(f"{where}: content hash does not match document {ref.document_id}")
    if int(doc.get("version") or 0) != int(ref.document_version):
        errs.append(f"{where}: document version {ref.document_version} does not match ({doc.get('version')})")
    if ref.chunk_id:
        chunk = chunk_repo.get_chunk(ref.chunk_id)
        if chunk is None or chunk.get("document_id") != ref.document_id:
            errs.append(f"{where}: chunk {ref.chunk_id} not found in document {ref.document_id}")
            return errs
        if chunk.get("content_hash") != ref.content_hash:
            errs.append(f"{where}: chunk content hash mismatch")
        if ref.page_number is not None and int(chunk.get("page_number") or 0) != ref.page_number:
            errs.append(f"{where}: page {ref.page_number} does not match chunk page {chunk.get('page_number')}")
        if ref.excerpt and _norm(ref.excerpt) not in _norm(chunk.get("text")):
            errs.append(f"{where}: excerpt does not match the source chunk text")
    elif ref.page_number is not None:
        pages = {int(c.get("page_number") or 0)
                 for c in chunk_repo.list_chunks(document_ids=[ref.document_id], include_embeddings=False)}
        if ref.page_number not in pages:
            errs.append(f"{where}: page {ref.page_number} has no extracted text in document {ref.document_id}")
    return errs


def evidence_errors(rule: PolicyRule) -> list[str]:
    errs: list[str] = []
    if not rule.evidence_refs and rule.source != "legacy_config":
        errs.append("at least one valid evidence reference to a ready policy document is required")
    for i, ref in enumerate(rule.evidence_refs):
        errs.extend(evidence_ref_errors(ref, i))
    return errs


# ---------------------------------------------------------------------------------------------- conflicts
def detect_conflicts(rule: Union[PolicyRule, dict], others: Iterable[Union[PolicyRule, dict]]) -> list[str]:
    """Generic conflict detection of ``rule`` against ``others`` (any statuses; contradictions use approved).

    Returns messages ``"<ref>: <reason>"``:
    - a newer approved version of the same rule_id overlaps this version's effective window;
    - an approved rule with an identical scope/condition but a different decision (contradictory outcome);
    - an approved rule with the same reason_code but a contradictory decision over the identical condition;
    - money comparisons on the same fact in different currencies;
    - ``supersedes`` pointing to a rule version that does not exist.
    """
    me = to_model(rule)
    my_ref = rule_ref(me)
    my_scope = _scope_hash(me)
    my_money: dict[str, set[str]] = {}
    _money_currencies(me.applies_when, my_money)
    _money_currencies(me.condition, my_money)
    known_refs: set[str] = set()
    out: list[str] = []
    for other in others:
        try:
            o = to_model(other)
        except ValidationError:
            continue
        ref = rule_ref(o)
        known_refs.add(ref)
        if ref == my_ref or o.status != "approved":
            continue
        if not _windows_overlap(me, o):
            continue
        if o.rule_id == me.rule_id:
            if o.rule_version > me.rule_version:
                out.append(f"{ref}: a newer approved version of {me.rule_id} overlaps this effective period")
            continue
        if me.supersedes == ref:
            continue
        same_scope = _scope_hash(o) == my_scope
        if same_scope and o.outcome.decision != me.outcome.decision:
            if o.outcome.reason_code == me.outcome.reason_code:
                out.append(f"{ref}: same reason_code {me.outcome.reason_code} with a contradictory decision "
                           f"({o.outcome.decision} vs {me.outcome.decision}) over the identical condition")
            else:
                out.append(f"{ref}: contradictory decision ({o.outcome.decision} vs {me.outcome.decision}) "
                           f"over the identical condition")
        other_money: dict[str, set[str]] = {}
        _money_currencies(o.applies_when, other_money)
        _money_currencies(o.condition, other_money)
        for field, currencies in my_money.items():
            theirs = other_money.get(field)
            if theirs and theirs != currencies:
                out.append(f"{ref}: money comparison on {field} uses currency {sorted(theirs)} "
                           f"but this rule uses {sorted(currencies)}")
    if me.supersedes:
        target = parse_ref(me.supersedes)
        if target is None:
            out.append(f"{me.supersedes}: supersedes reference is malformed (expected RULE-ID@version)")
        elif me.supersedes not in known_refs and repo.get_rule(*target) is None:
            out.append(f"{me.supersedes}: supersedes a rule version that does not exist")
        elif me.supersedes == my_ref:
            out.append(f"{me.supersedes}: a rule cannot supersede itself")
    return sorted(set(out))


def _all_rules_for_conflicts() -> list[dict]:
    return repo.list_rules(statuses=["approved", "candidate", "needs_review", "validated", "superseded"],
                           limit=repo.MAX_RULE_LIMIT)


def check_rule(rule: PolicyRule) -> tuple[list[str], list[str]]:
    """(validation errors, conflicts) for a stored rule. Expired windows are validation errors."""
    errors = list(evaluator.validate_rule(rule))
    errors.extend(evidence_errors(rule))
    if rule.effective_until and rule.effective_until < _today():
        errors.append(f"effective_until {rule.effective_until.isoformat()} is in the past (rule expired)")
    conflicts = detect_conflicts(rule, _all_rules_for_conflicts())
    return sorted(set(errors), key=errors.index), conflicts


# ---------------------------------------------------------------------------------------------- reads
def get_rule(rule_id: str, version: int) -> Optional[dict]:
    return repo.get_rule(rule_id, int(version))


def list_rules(status: Optional[str] = None, rule_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    return repo.list_rules(status=status, rule_id=rule_id, limit=limit)


def count_rules_by_status() -> dict[str, int]:
    return repo.count_rules_by_status()


def find_duplicate(rule: Union[PolicyRule, dict]) -> Optional[dict]:
    """An existing non-rejected rule doing exactly the same thing (same semantic hash), if any."""
    for doc in repo.list_rules(semantic_hash=semantic_hash(rule), limit=50):
        if doc.get("status") not in ("rejected",):
            return doc
    return None


def active_version() -> int:
    return int(repo.get_pointer()["version"])


def active_policy_set() -> Optional[dict]:
    pointer = repo.get_pointer()
    if not pointer["version"]:
        return None
    return repo.get_policy_set(pointer["version"], set_id=pointer.get("set_id"))


def active_rules(as_of: Optional[dt.date] = None) -> list[PolicyRule]:
    """Approved rules of the active policy set that are effective on ``as_of`` (UTC date; default today)."""
    pset = active_policy_set()
    if not pset:
        return []
    as_of = as_of or _today()
    refs = set(pset.get("rule_refs") or [])
    out: list[PolicyRule] = []
    for doc in repo.list_rules(status="approved", limit=repo.MAX_RULE_LIMIT):
        if rule_ref(doc) not in refs:
            continue
        try:
            rule = to_model(doc)
        except ValidationError:
            logger.warning("Skipping unreadable approved rule %s.", rule_ref(doc))
            continue
        if is_effective(rule, as_of):
            out.append(rule)
    return sorted(out, key=lambda r: (r.rule_id, r.rule_version))


# ---------------------------------------------------------------------------------------------- create
_SERVER_FIELDS = ("status", "validation_errors", "conflict_refs", "created_by", "created_at", "reviewed_by",
                  "reviewed_at", "review_comment", "approved_by", "approved_at", "rule_version")


def _new_rule_id() -> str:
    return f"RULE-{uuid.uuid4().hex[:12].upper()}"


def create_candidate(rule_dict_or_model: Union[PolicyRuleIn, PolicyRule, dict], actor: Union[UserPublic, str],
                     *, extra: Optional[dict] = None) -> dict:
    """Create a new candidate version. Never activates anything.

    ``PolicyRuleIn`` -> ``source="manual"`` created by the actor (reviewer). Dicts/``PolicyRule`` may carry
    ``source``/extraction metadata (used by the extractor and bootstrap). Status is ``candidate`` or
    ``needs_review`` (validation errors or conflicts). Manual candidates with invalid evidence -> 422.
    """
    if isinstance(actor, UserPublic):
        _require_reviewer(actor)
    creator = actor_name(actor)
    if isinstance(rule_dict_or_model, PolicyRuleIn):
        data = rule_dict_or_model.model_dump(by_alias=True, exclude_none=False)
        data["source"] = "manual"
    elif isinstance(rule_dict_or_model, PolicyRule):
        data = rule_dict_or_model.model_dump(by_alias=True)
    elif isinstance(rule_dict_or_model, dict):
        data = dict(rule_dict_or_model)
        data.setdefault("source", "manual")
    else:
        raise RuleRegistryError(422, "Unsupported rule payload.")
    warnings = list(data.pop("warnings", []) or [])
    for key in _SERVER_FIELDS:
        data.pop(key, None)
    rule_id = data.get("rule_id") or _new_rule_id()
    data.update({"rule_id": rule_id, "rule_version": repo.latest_rule_version(rule_id) + 1, "status": "candidate",
                 "created_by": creator, "created_at": _now()})
    if data.get("supersedes") is None:
        prev = repo.list_rules(rule_id=rule_id, status="approved", limit=1)
        if prev:
            data["supersedes"] = rule_ref(prev[0])
    try:
        rule = PolicyRule.model_validate(data)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors()[:10]]
        raise RuleRegistryError(422, "Invalid policy rule.", errors=msgs) from None
    errors, conflicts = check_rule(rule)
    if rule.source == "manual":
        bad_evidence = [e for e in errors if e.startswith("evidence[") or e.startswith("at least one valid")]
        if bad_evidence:
            raise RuleRegistryError(422, "Evidence references must point to real chunks of ready policy "
                                         "documents.", errors=bad_evidence)
    status = "needs_review" if (errors or conflicts) else "candidate"
    rule = rule.model_copy(update={"status": status, "validation_errors": errors, "conflict_refs": conflicts})
    doc = {**_dump(rule), "rule_hash": evaluator.canonical_rule_hash(rule), "semantic_hash": semantic_hash(rule),
           "warnings": warnings, "duplicate_links": [], **(extra or {})}
    stored = repo.insert_rule(doc)
    try:
        repo.record_event("rule_candidate_created", creator, rule_id=rule.rule_id, rule_version=rule.rule_version,
                          source=rule.source, status=status, rule_hash=doc["rule_hash"],
                          document_ids=sorted({r.document_id for r in rule.evidence_refs}),
                          validation_error_count=len(errors), conflict_count=len(conflicts))
    except PersistenceError:
        _best_effort_delete_rule(rule.rule_id, rule.rule_version)
        raise
    return stored


def _best_effort_delete_rule(rule_id: str, version: int) -> None:
    try:
        repo.delete_rule(rule_id, version)
    except Exception:  # noqa: BLE001
        logger.error("Could not roll back rule %s@%s after an audit failure.", rule_id, version)


def link_duplicate(existing: dict, *, document_id: Optional[str], evidence_refs: list[EvidenceRef],
                   actor: str, extraction_model: Optional[str] = None) -> None:
    repo.add_duplicate_link(existing["rule_id"], existing["rule_version"], {
        "document_id": document_id, "linked_at": _now(), "linked_by": actor,
        "extraction_model": extraction_model,
        "evidence_refs": [r.model_dump(mode="json") for r in evidence_refs]})
    repo.record_event("rule_duplicate_linked", actor, rule_id=existing["rule_id"],
                      rule_version=existing["rule_version"], document_id=document_id)


# ---------------------------------------------------------------------------------------------- validate
def _load(rule_id: str, version: int) -> dict:
    doc = repo.get_rule(rule_id, int(version))
    if doc is None:
        raise RuleRegistryError(404, MSG_NOT_FOUND)
    return doc


def validate(rule_id: str, version: int, actor: Union[UserPublic, str]) -> dict:
    """Re-run schema/evidence/conflict checks. Editable rules move to validated or needs_review."""
    name = _require_reviewer(actor)
    doc = _load(rule_id, version)
    rule = to_model(doc)
    errors, conflicts = check_rule(rule)
    if doc.get("status") in EDITABLE_STATUSES:
        status = "needs_review" if (errors or conflicts) else "validated"
        updated = repo.update_rule(rule_id, version, {"status": status, "validation_errors": errors,
                                                      "conflict_refs": conflicts},
                                   expected_statuses=EDITABLE_STATUSES)
        if updated is None:
            raise RuleRegistryError(409, "The rule changed while validating; reload and retry.")
        try:
            repo.record_event("rule_validated" if status == "validated" else "rule_validation_failed", name,
                              rule_id=rule_id, rule_version=int(version), status=status,
                              validation_error_count=len(errors), conflict_count=len(conflicts))
        except PersistenceError:
            repo.update_rule(rule_id, version, {"status": doc.get("status"),
                                                "validation_errors": doc.get("validation_errors", []),
                                                "conflict_refs": doc.get("conflict_refs", [])})
            raise
        doc = updated
    return {"rule": doc, "errors": errors, "conflicts": conflicts}


# --------------------------------------------------------------------------------------- policy-set build
def _set_candidates(today: dt.date) -> list[PolicyRule]:
    out = []
    for doc in repo.list_rules(status="approved", limit=repo.MAX_RULE_LIMIT):
        try:
            rule = to_model(doc)
        except ValidationError:
            continue
        if rule.effective_until and rule.effective_until < today:
            continue
        out.append(rule)
    return out


def _activate(actor: str, reason: str) -> tuple[dict, dict]:
    """Build a new immutable policy set from all approved, non-expired rules and CAS the pointer.

    Versions are always ``pointer + 1`` so the chain has no gaps. A set row at that version which the pointer
    does not reference is an in-flight attempt of a concurrent activation (it is deleted when that attempt
    loses its CAS); we wait for it, and remove it if it is older than ``ORPHAN_SET_SECONDS`` (crashed writer).
    Raises RuleRegistryError(409) after MAX_CAS_ATTEMPTS lost races."""
    lost = 0
    deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
    while lost < MAX_CAS_ATTEMPTS:
        pointer = repo.get_pointer()
        new_version = pointer["version"] + 1
        rules = _set_candidates(_today())
        refs = sorted(rule_ref(r) for r in rules)
        pset = PolicySetVersion(policy_set_version=new_version, rule_refs=refs,
                                rule_hashes={rule_ref(r): evaluator.canonical_rule_hash(r) for r in rules},
                                created_at=_now(), created_by=actor, reason=reason[:200],
                                previous_version=pointer["version"] or None)
        stored = repo.insert_policy_set(pset.model_dump())
        if stored is None:
            existing = repo.get_policy_set(new_version)
            if existing and repo.get_pointer()["version"] < new_version:
                created = existing.get("created_at")
                if isinstance(created, dt.datetime) and (_now() - created).total_seconds() > ORPHAN_SET_SECONDS:
                    repo.delete_policy_set(existing["set_id"])
                    continue
            if time.monotonic() > deadline:
                break
            time.sleep(0.01 + (uuid.uuid4().int % 20) / 1000)
            continue
        if repo.cas_pointer(pointer["version"], new_version, stored["set_id"],
                            expected_set_id=pointer.get("set_id")):
            return pointer, stored
        repo.delete_policy_set(stored["set_id"])
        lost += 1
    raise RuleRegistryError(409, MSG_CONCURRENT)


def _rollback_activation(pointer_before: dict, pset: dict) -> bool:
    try:
        ok = repo.cas_pointer(pset["policy_set_version"], pointer_before["version"], pointer_before.get("set_id"),
                              expected_set_id=pset["set_id"])
        if ok:
            repo.delete_policy_set(pset["set_id"])
        return ok
    except Exception:  # noqa: BLE001
        logger.error("Could not roll back policy set %s.", pset.get("policy_set_version"))
        return False


def _restore(rule_id: str, version: int, fields: dict) -> None:
    try:
        repo.update_rule(rule_id, version, fields)
    except Exception:  # noqa: BLE001
        logger.error("Could not restore rule %s@%s during rollback.", rule_id, version)


def _record_activation_failure(actor: str, rule_id: str, version: int, action: str) -> None:
    try:
        repo.record_event("rule_activation_failed", actor, rule_id=rule_id, rule_version=int(version),
                          action=action, reason="audit_or_persistence_failure")
    except Exception:  # noqa: BLE001
        logger.error("Could not record activation failure for %s@%s.", rule_id, version)


# ---------------------------------------------------------------------------------------------- approve
def _check_approver(actor: Union[UserPublic, str], doc: dict) -> str:
    name = _require_reviewer(actor)
    source = doc.get("source")
    if name == EXTRACTOR_ACTOR:
        raise RuleRegistryError(403, "The extraction service cannot approve rules.")
    if name == BOOTSTRAP_ACTOR and source != "legacy_config":
        raise RuleRegistryError(403, "Only legacy configuration rules can be bootstrapped automatically.")
    if source == "manual" and doc.get("created_by") == name:
        raise RuleRegistryError(403, MSG_SEPARATION)
    return name


def _replay(doc: dict) -> dict:
    ref = rule_ref(doc)
    until = doc.get("effective_until")
    wait = REPLAY_WAIT_SECONDS
    if until and str(until)[:10] < _today().isoformat():
        wait = 0.0  # expired rules are never part of a new set; don't wait for one
    deadline = time.monotonic() + wait
    while True:
        pset = active_policy_set()
        if pset and ref in (pset.get("rule_refs") or []):
            return {"rule": repo.get_rule(doc["rule_id"], doc["rule_version"]) or doc,
                    "policy_set_version": int(pset["policy_set_version"]), "replayed": True}
        current = repo.get_rule(doc["rule_id"], doc["rule_version"])
        if current is None or current.get("status") != "approved":
            raise RuleRegistryError(409, "The rule is no longer approved; reload it.")
        if time.monotonic() >= deadline:
            # Approved but not (yet) in the active set, e.g. expired; report the current set.
            return {"rule": current, "policy_set_version": active_version(), "replayed": True}
        time.sleep(0.05)


def approve(rule_id: str, version: int, actor: Union[UserPublic, str], comment: Optional[str] = None,
            expected_policy_set_version: Optional[int] = None) -> dict:
    """Approve a candidate version and activate a new policy set. Idempotent for already-approved versions."""
    _require_reviewer(actor)
    doc = _load(rule_id, version)
    name = _check_approver(actor, doc)
    status = doc.get("status")
    if status == "approved":
        return _replay(doc)
    if status in TERMINAL_STATUSES:
        raise RuleRegistryError(409, f"A {status} rule cannot be approved; create a new version instead.")
    if expected_policy_set_version is not None and active_version() != int(expected_policy_set_version):
        raise RuleRegistryError(409, MSG_STALE_SET)
    rule = to_model(doc)
    errors, conflicts = check_rule(rule)
    if errors or conflicts:
        repo.update_rule(rule_id, version, {"status": "needs_review", "validation_errors": errors,
                                            "conflict_refs": conflicts}, expected_statuses=EDITABLE_STATUSES)
        detail = ("The rule has unresolved conflicts and cannot be approved." if conflicts and not errors
                  else "The rule has validation errors and cannot be approved.")
        raise RuleRegistryError(409, detail, errors=errors, conflicts=conflicts)

    now = _now()
    previous_fields = {k: doc.get(k) for k in ("status", "validation_errors", "conflict_refs", "reviewed_by",
                                               "reviewed_at", "review_comment", "approved_by", "approved_at")}
    approved = repo.update_rule(rule_id, version, {
        "status": "approved", "validation_errors": [], "conflict_refs": [], "reviewed_by": name,
        "reviewed_at": now, "review_comment": comment, "approved_by": name, "approved_at": now},
        expected_statuses=EDITABLE_STATUSES)
    if approved is None:
        current = _load(rule_id, version)
        if current.get("status") == "approved":
            return _replay(current)
        raise RuleRegistryError(409, "The rule changed while approving; reload and retry.")

    # Supersede older approved versions of the same rule and an explicitly superseded rule.
    superseded: list[tuple[str, int]] = []
    targets = [d for d in repo.list_rules(rule_id=rule_id, status="approved", limit=100)
               if int(d["rule_version"]) < int(version)]
    explicit = parse_ref(rule.supersedes or "")
    if explicit and explicit[0] != rule_id:
        target_doc = repo.get_rule(*explicit)
        if target_doc and target_doc.get("status") == "approved":
            targets.append(target_doc)
    try:
        for t in targets:
            if repo.update_rule(t["rule_id"], t["rule_version"], {"status": "superseded", "superseded_at": now,
                                                                  "superseded_by": rule_ref(doc)},
                                expected_statuses=["approved"]):
                superseded.append((t["rule_id"], int(t["rule_version"])))
        pointer_before, pset = _activate(name, f"approve {rule_ref(doc)}")
    except Exception:
        for rid, ver in superseded:
            _restore(rid, ver, {"status": "approved", "superseded_at": None, "superseded_by": None})
        _restore(rule_id, version, previous_fields)
        raise

    try:
        repo.record_event("rule_approved", name, rule_id=rule_id, rule_version=int(version),
                          rule_hash=doc.get("rule_hash"), source=doc.get("source"),
                          policy_set_version=pset["policy_set_version"], comment_present=bool(comment))
        for rid, ver in superseded:
            repo.record_event("rule_superseded", name, rule_id=rid, rule_version=ver,
                              superseded_by=rule_ref(doc), policy_set_version=pset["policy_set_version"])
        repo.record_event("policy_set_activated", name, policy_set_version=pset["policy_set_version"],
                          previous_version=pointer_before["version"], rule_refs=pset["rule_refs"],
                          rule_id=rule_id, trigger=rule_ref(doc))
    except Exception as exc:
        if _rollback_activation(pointer_before, pset):
            for rid, ver in superseded:
                _restore(rid, ver, {"status": "approved", "superseded_at": None, "superseded_by": None})
            _restore(rule_id, version, previous_fields)
        _record_activation_failure(name, rule_id, version, "approve")
        if isinstance(exc, (PersistenceError, RuleRegistryError)):
            raise RuleRegistryError(503, MSG_AUDIT_FAILED) from None
        raise
    return {"rule": repo.get_rule(rule_id, version) or approved,
            "policy_set_version": int(pset["policy_set_version"]), "replayed": False}


# ---------------------------------------------------------------------------------------------- reject
def reject(rule_id: str, version: int, actor: Union[UserPublic, str], comment: Optional[str]) -> dict:
    """Reject a candidate (or deactivate an approved rule, which activates a new policy set without it)."""
    name = _require_reviewer(actor)
    if not (comment or "").strip():
        raise RuleRegistryError(422, MSG_COMMENT_REQUIRED)
    doc = _load(rule_id, version)
    status = doc.get("status")
    if status == "rejected":
        return doc
    if status in ("superseded", "expired"):
        raise RuleRegistryError(409, f"A {status} rule cannot be rejected.")
    now = _now()
    fields = {"status": "rejected", "reviewed_by": name, "reviewed_at": now, "review_comment": comment.strip(),
              "rejected_at": now}
    previous = {k: doc.get(k) for k in ("status", "reviewed_by", "reviewed_at", "review_comment", "rejected_at")}
    updated = repo.update_rule(rule_id, version, fields, expected_statuses=[status])
    if updated is None:
        current = _load(rule_id, version)
        if current.get("status") == "rejected":
            return current
        raise RuleRegistryError(409, "The rule changed while rejecting; reload and retry.")
    pointer_before = pset = None
    try:
        if status == "approved":
            pointer_before, pset = _activate(name, f"reject {rule_ref(doc)}")
    except Exception:
        _restore(rule_id, version, previous)
        raise
    try:
        repo.record_event("rule_rejected", name, rule_id=rule_id, rule_version=int(version),
                          previous_status=status, policy_set_version=(pset or {}).get("policy_set_version"))
        if pset is not None:
            repo.record_event("rule_deactivated", name, rule_id=rule_id, rule_version=int(version),
                              policy_set_version=pset["policy_set_version"])
            repo.record_event("policy_set_activated", name, policy_set_version=pset["policy_set_version"],
                              previous_version=pointer_before["version"], rule_refs=pset["rule_refs"],
                              rule_id=rule_id, trigger=f"reject {rule_ref(doc)}")
    except Exception as exc:
        if pset is None or _rollback_activation(pointer_before, pset):
            _restore(rule_id, version, previous)
        _record_activation_failure(name, rule_id, version, "reject")
        if isinstance(exc, (PersistenceError, RuleRegistryError)):
            raise RuleRegistryError(503, "The rejection could not be recorded in the audit log; nothing "
                                         "changed. Retry later.") from None
        raise
    return repo.get_rule(rule_id, version) or updated


# ---------------------------------------------------------------------------------------------- bootstrap
def bootstrap_from_seed(path: Union[str, Path, None] = None) -> int:
    """Create + auto-approve the ``legacy_config`` seed rules as ``system:bootstrap``. Idempotent.

    Skips a rule when the same canonical hash already exists as approved/rejected/superseded (a reviewer's
    decision is respected) or when the current approved version of that rule_id was approved by a human.
    Returns the number of rules approved by this call."""
    _, rules = evaluator.load_seed_bundle(path)
    approved = 0
    for seed in rules:
        if seed.source != "legacy_config":
            logger.warning("Seed rule %s is not legacy_config; skipped.", seed.rule_id)
            continue
        seed_hash = evaluator.canonical_rule_hash(seed)
        existing = repo.list_rules(rule_id=seed.rule_id, limit=100)
        same = [d for d in existing if d.get("seed_hash") == seed_hash]
        if any(d.get("status") in ("approved", "rejected", "superseded", "expired") for d in same):
            continue
        current = [d for d in existing if d.get("status") == "approved"]
        if current and current[0].get("approved_by") != BOOTSTRAP_ACTOR:
            continue
        pending = [d for d in same if d.get("status") in EDITABLE_STATUSES]
        doc = pending[0] if pending else create_candidate(seed, BOOTSTRAP_ACTOR, extra={"seed_hash": seed_hash})
        result = approve(doc["rule_id"], doc["rule_version"], BOOTSTRAP_ACTOR,
                         comment="Legacy configuration rule bootstrapped at startup.")
        if not result["replayed"]:
            approved += 1
    if approved:
        repo.record_event("rule_bootstrap_completed", BOOTSTRAP_ACTOR, approved_count=approved,
                          policy_set_version=active_version())
    return approved
