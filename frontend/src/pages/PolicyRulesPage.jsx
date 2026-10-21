import { useCallback, useState } from "react";
import {
  approveRule, createRule, getActivePolicySet, getRule, getRuleFacts, listDocuments, listRules, rejectRule, validateRule,
} from "../api/policies";
import ConfirmDialog from "../components/ConfirmDialog";
import DataTable from "../components/DataTable";
import PageHeader from "../components/PageHeader";
import { EmptyState, ErrorState, LoadingState, Notice } from "../components/States";
import StatusBadge from "../components/StatusBadge";
import Tabs from "../components/Tabs";
import { useAuth } from "../lib/auth";
import { formatDate, formatDateTime, humanize } from "../lib/format";
import { useApi } from "../lib/useApi";
import { RateCardsSection } from "./PolicyLibraryPage";
import "../styles/pages/policy.css";

export const STALE_WARNING = "Approving a rule activates a new policy set. Pending approvals evaluated under the "
  + "previous set may become stale and must be re-run before they can be decided.";
const PENDING = ["candidate", "needs_review", "validated"];
const REVIEWABLE = new Set(PENDING);

/** Plain-language rule state: active vs pending vs rejected must be distinct in text, not just color. */
const RULE_STATE = {
  approved: { label: "Active", tone: "approved", group: "active" },
  candidate: { label: "Pending review", tone: "draft", group: "pending" },
  needs_review: { label: "Pending — needs fixes", tone: "needs_review", group: "pending" },
  validated: { label: "Pending — validated", tone: "validated", group: "pending" },
  rejected: { label: "Rejected", tone: "rejected", group: "rejected" },
  superseded: { label: "Replaced by newer version", tone: "retired", group: "other" },
  expired: { label: "Expired", tone: "retired", group: "other" },
};
function RuleBadge({ status }) {
  const s = RULE_STATE[status];
  return <StatusBadge kind="policy" status={s?.tone || status} label={s?.label || humanize(status)} />;
}
const DECISION = { pass: "Pass", review_required: "Needs review", blocked: "Block", needs_clarification: "Ask for more information" };
const SEVERITY = { info: "Info", review: "Review", block: "Block" };

const FILTERS = [
  { value: "", label: "All rules" },
  { value: "approved", label: "Active" },
  { value: "pending", label: "Pending review" },
  { value: "rejected", label: "Rejected" },
  { value: "superseded", label: "Replaced" },
  { value: "expired", label: "Expired" },
];

function ruleRef(r) { return `${r.rule_id}@${r.rule_version}`; }

function evidenceLabel(e) {
  const parts = [e.filename || e.document_id || "Document"];
  if (e.document_version != null) parts.push(`v${e.document_version}`);
  parts.push(e.page_number != null ? `p. ${e.page_number}` : "page unavailable");
  if (e.section) parts.push(`§ ${e.section}`);
  return parts.join(" · ");
}

function errorList(err) {
  const b = err?.body;
  return [...(Array.isArray(b?.errors) ? b.errors : []), ...(Array.isArray(b?.conflicts) ? b.conflicts.map((c) => `Conflict: ${c}`) : [])];
}

// ------------------------------------------------------------------ active set banner
function ActiveSetBanner({ pset, error, loading, onRetry }) {
  if (loading && !pset) return <section className="card"><LoadingState label="Loading active policy set…" /></section>;
  if (error) return <section className="card"><ErrorState error={error} onRetry={onRetry} title="Active policy set unavailable" /></section>;
  if (!pset) return null;
  if (!pset.active) {
    return <Notice tone="warning" title="No active policy set">No rules are active yet. Approved rules form the active policy set.</Notice>;
  }
  const count = (pset.rule_refs || []).length;
  return (
    <section className="card policy-banner" aria-label="Active policy set">
      <div className="stack-sm" style={{ gap: 2 }}>
        <strong>Active policy set: version {pset.policy_set_version}</strong>
        <span className="muted small">
          {count} active rule{count === 1 ? "" : "s"} · activated {formatDateTime(pset.created_at)}
          {pset.created_by ? ` by ${pset.created_by}` : ""}{pset.reason ? ` · ${pset.reason}` : ""}
        </span>
      </div>
      <StatusBadge kind="policy" status="active" label="In force" />
    </section>
  );
}

// ------------------------------------------------------------------ rule detail
function RuleDetail({ rule: summary, reviewer, activeVersion, onChanged, onClose }) {
  const { data: rule, error, loading, reload } = useApi(() => getRule(summary.rule_id, summary.rule_version),
    [summary.rule_id, summary.rule_version]);
  const versions = useApi(() => listRules({ rule_id: summary.rule_id, limit: 50 }), [summary.rule_id]);
  const [action, setAction] = useState(null); // validate | approve | reject
  const [comment, setComment] = useState("");
  const [result, setResult] = useState(null);

  const open = (a) => { setResult(null); setComment(""); setAction(a); };
  const run = useCallback(async (reason) => {
    const ver = summary.rule_version;
    let res;
    try {
      if (action === "validate") res = await validateRule(summary.rule_id, ver);
      else if (action === "approve") {
        const body = { rule_version: ver };
        if (comment.trim()) body.comment = comment.trim();
        if (activeVersion != null) body.expected_policy_set_version = activeVersion;
        res = await approveRule(summary.rule_id, body);
      } else res = await rejectRule(summary.rule_id, { rule_version: ver, comment: reason });
    } catch (e) {
      if (e?.status === 409) {
        setAction(null);
        setResult({ tone: "warning", title: "Nothing changed", text: e.message, items: errorList(e) });
        reload(); onChanged?.();
        return;
      }
      throw e; // 403 (e.g. creator cannot approve own rule), 422, 5xx stay in the dialog
    }
    setAction(null);
    if (action === "validate") {
      const errs = res?.errors || [];
      const conflicts = res?.conflicts || [];
      setResult(errs.length || conflicts.length
        ? { tone: "warning", title: "Validation found problems", text: "The rule needs fixes before it can be approved.",
          items: [...errs, ...conflicts.map((c) => `Conflict: ${typeof c === "string" ? c : JSON.stringify(c)}`)] }
        : { tone: "success", title: "Validation passed", text: "The rule is ready for approval." });
    } else if (action === "approve") {
      const status = res?.rule?.status;
      setResult(status === "approved"
        ? { tone: "success", title: "Rule approved and active",
          text: `${res.replayed ? "This rule was already approved. " : ""}Active policy set is now version ${res.policy_set_version ?? "—"}. ${STALE_WARNING}` }
        : { tone: "error", title: "Not confirmed", text: "The server did not confirm the approval. Reload before trying again." });
    } else {
      const status = res?.status ?? res?.rule?.status;
      setResult(status === "rejected"
        ? { tone: "success", title: "Rule rejected", text: "The rule will not be used." }
        : { tone: "error", title: "Not confirmed", text: "The server did not confirm the rejection. Reload before trying again." });
    }
    reload(); versions.reload(); onChanged?.();
  }, [action, comment, summary, activeVersion, reload, versions, onChanged]);

  if (loading && !rule) return <section className="card"><LoadingState label="Loading rule…" /></section>;
  if (error) return <section className="card"><ErrorState error={error} onRetry={reload} /></section>;
  if (!rule) return null;

  const outcome = rule.outcome || {};
  const evidence = (rule.evidence_refs || []).filter((e) => e && typeof e === "object");
  const canReview = reviewer && REVIEWABLE.has(rule.status);
  const otherVersions = (versions.data?.rules || []).filter((v) => v.rule_version !== rule.rule_version);
  const dlg = {
    validate: { title: "Validate this rule?", label: "Validate",
      description: "The server re-checks the rule's structure, source evidence and conflicts with other rules." },
    approve: { title: "Approve and activate this rule?", label: "Approve rule",
      description: `${rule.name} (version ${rule.rule_version}) becomes active in a new policy set. ${STALE_WARNING}` },
    reject: { title: "Reject this rule?", label: "Reject rule", tone: "danger",
      description: rule.status === "approved" ? "The rule is removed from the active policy set." : "The rule will not be used." },
  }[action];

  return (
    <section className={`card stack policy-rule-${RULE_STATE[rule.status]?.group || "other"}`} aria-labelledby="rule-title">
      <div className="row-between">
        <h2 id="rule-title">{rule.name}</h2>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
      </div>
      {result && (
        <Notice tone={result.tone} title={result.title} role="status">
          {result.text}
          {result.items?.length > 0 && <ul className="policy-list">{result.items.map((x, i) => <li key={i}>{x}</li>)}</ul>}
        </Notice>
      )}
      <div className="row">
        <RuleBadge status={rule.status} />
        {rule.active && <span className="small">In the active policy set</span>}
        <span className="muted small">Version {rule.rule_version} · {humanize(rule.source)}</span>
      </div>
      {rule.description && <p>{rule.description}</p>}

      <dl className="dl">
        <dt>Applies when</dt><dd>{rule.applies_when ? (rule.applies_when_text || "See technical details") : "Always"}</dd>
        <dt>Triggers when</dt><dd><pre className="policy-expr">{rule.condition_text || "See technical details"}</pre></dd>
        <dt>Then</dt><dd>{DECISION[outcome.decision] || humanize(outcome.decision)}{outcome.message ? ` — ${outcome.message}` : ""}</dd>
        <dt>Severity</dt><dd>{SEVERITY[rule.severity] || humanize(rule.severity)}{rule.mandatory ? " · mandatory" : ""}</dd>
        <dt>Effective</dt><dd>{rule.effective_from || rule.effective_until ? `${formatDate(rule.effective_from)} – ${formatDate(rule.effective_until)}` : "No date limits"}</dd>
        <dt>Created</dt><dd>{rule.created_by || "—"} · {formatDateTime(rule.created_at)}</dd>
        {rule.approved_by && <><dt>Approved</dt><dd>{rule.approved_by} · {formatDateTime(rule.approved_at)}</dd></>}
        {!rule.approved_by && rule.reviewed_by && <><dt>Reviewed</dt><dd>{rule.reviewed_by} · {formatDateTime(rule.reviewed_at)}</dd></>}
        {rule.review_comment && <><dt>Review comment</dt><dd>{rule.review_comment}</dd></>}
      </dl>

      {(rule.validation_errors?.length > 0 || rule.conflict_refs?.length > 0) && (
        <Notice tone="warning" title="Needs fixes before approval">
          <ul className="policy-list">
            {(rule.validation_errors || []).map((e, i) => <li key={`e${i}`}>{e}</li>)}
            {(rule.conflict_refs || []).map((c, i) => <li key={`c${i}`}>Conflicts with {typeof c === "string" ? c : JSON.stringify(c)}</li>)}
          </ul>
        </Notice>
      )}

      {canReview && (
        <div className="stack-sm">
          <p className="muted small">{STALE_WARNING} Another reviewer must approve a rule you created yourself.</p>
          <div className="policy-actions">
            <button type="button" className="btn btn-primary" onClick={() => open("approve")}>Approve</button>
            <button type="button" className="btn" onClick={() => open("validate")}>Validate</button>
            <button type="button" className="btn btn-danger" onClick={() => open("reject")}>Reject</button>
          </div>
        </div>
      )}
      {reviewer && rule.status === "approved" && (
        <div className="policy-actions">
          <button type="button" className="btn btn-danger" onClick={() => open("reject")}>Deactivate (reject)</button>
        </div>
      )}

      <details className="disclosure">
        <summary>Source evidence ({evidence.length})</summary>
        {rule.source === "extracted" && <Notice tone="warning">{"Unverified — extracted text may be wrong. A reviewer must verify terms before comparison."}</Notice>}
        {evidence.length === 0 ? <p className="muted small">No source citation is attached.</p> : (
          <div className="stack-sm">
            {evidence.map((e, i) => (
              <div key={i} className="policy-evidence">
                <strong>{evidenceLabel(e)}</strong>
                {e.excerpt && <blockquote className="untrusted" style={{ margin: "4px 0 0" }}>{e.excerpt}</blockquote>}
              </div>
            ))}
          </div>
        )}
      </details>

      <details className="disclosure">
        <summary>Versions and history</summary>
        <div className="stack-sm">
          {versions.loading && <LoadingState label="Loading versions…" />}
          {versions.error && <ErrorState error={versions.error} onRetry={versions.reload} />}
          {!versions.loading && !versions.error && (otherVersions.length === 0
            ? <p className="muted small">No other versions.</p>
            : <ul className="policy-list">{otherVersions.map((v) => (
              <li key={ruleRef(v)}>Version {v.rule_version} — {RULE_STATE[v.status]?.label || humanize(v.status)}</li>))}</ul>)}
          {(rule.events || []).length > 0 && (
            <ul className="policy-list">
              {rule.events.map((e, i) => (
                <li key={i}>{humanize(e.event_type)} · {formatDateTime(e.created_at)}{e.actor ? ` · ${e.actor}` : ""}
                  {e.policy_set_version != null ? ` · policy set v${e.policy_set_version}` : ""}</li>
              ))}
            </ul>
          )}
        </div>
      </details>

      <details className="disclosure">
        <summary>Technical details</summary>
        <pre className="policy-expr">{JSON.stringify({ rule_id: rule.rule_id, rule_version: rule.rule_version,
          applies_when: rule.applies_when ?? null, condition: rule.condition, outcome: rule.outcome }, null, 2)}</pre>
      </details>

      <ConfirmDialog open={Boolean(action)} title={dlg?.title} description={dlg?.description} confirmLabel={dlg?.label}
        tone={dlg?.tone} requireReason={action === "reject" ? 3 : 0} reasonLabel="Reason for rejecting"
        onConfirm={run} onCancel={() => setAction(null)}>
        {action === "approve" && (
          <div className="field">
            <label htmlFor="rule-approve-comment">Comment (optional)</label>
            <textarea id="rule-approve-comment" className="textarea" maxLength={1000} value={comment}
              onChange={(e) => setComment(e.target.value)} />
          </div>
        )}
      </ConfirmDialog>
    </section>
  );
}

// ------------------------------------------------------------------ create rule (admin)
const FACTS = ["request.attendees", "request.budget_limit", "request.service", "request.location", "request.event_date",
  "request.event_category", "request.currency", "request.approved_vendor_only", "vendor.selected", "vendor.vendor_id",
  "vendor.approved", "po.total", "po.subtotal", "po.tax", "po.currency", "po.line_count", "workflow.final_status",
  "rate.any_fail", "rate.any_unverified", "policy.insufficient_evidence"];
const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists", "is_missing", "contains_any", "date_before", "date_after"];
const OP_LABEL = { eq: "equals", ne: "does not equal", gt: "greater than", gte: "at least", lt: "less than", lte: "at most",
  in: "is one of", not_in: "is not one of", exists: "is present", is_missing: "is missing", contains_any: "contains any of",
  between: "is between", date_before: "is before", date_after: "is after", date_between: "is between dates" };
const VALUE_TYPES = ["integer", "decimal", "money", "string", "boolean", "date", "string_list"];
const LIST_OPS = new Set(["in", "not_in", "contains_any"]);
const RANGE_OPS = new Set(["between", "date_between"]);
const NO_VALUE_OPS = new Set(["exists", "is_missing"]);

/** Fact registry + operators from GET /api/policy-rules/facts, or the built-in lists when unavailable. */
export function factVocabulary(data) {
  const facts = Array.isArray(data?.facts) ? data.facts.filter((x) => x && typeof x.name === "string") : [];
  const ops = Array.isArray(data?.operators) ? data.operators.filter((o) => typeof o === "string") : [];
  return {
    fromServer: facts.length > 0,
    facts: facts.length ? facts : FACTS.map((name) => ({ name })),
    ops: ops.length ? ops : OPS,
  };
}

function emptyRule() {
  return { name: "", description: "", severity: "review", mandatory: false, field: "po.total", op: "gt", value: "",
    value_type: "money", currency: "INR", decision: "review_required", reason_code: "", message: "", document_id: "",
    page_number: "", excerpt: "" };
}

function parseValue(f) {
  if (NO_VALUE_OPS.has(f.op)) return undefined;
  const raw = f.value.trim();
  if (LIST_OPS.has(f.op) || RANGE_OPS.has(f.op)) {
    const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
    return f.value_type === "integer" ? parts.map((p) => Number.parseInt(p, 10)) : parts;
  }
  if (f.value_type === "boolean") return raw.toLowerCase() === "true";
  if (f.value_type === "integer") return Number.parseInt(raw, 10);
  return raw;
}

function validateRuleForm(f) {
  const e = {};
  if (f.name.trim().length < 3) e.name = "Enter a name of at least 3 characters.";
  if (!/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$/.test(f.field.trim())) e.field = "Use a fact name like po.total.";
  if (!NO_VALUE_OPS.has(f.op) && !f.value.trim()) e.value = "Enter a value.";
  if (RANGE_OPS.has(f.op) && f.value.trim() && f.value.split(",").map((s) => s.trim()).filter(Boolean).length !== 2) {
    e.value = "Enter exactly two values separated by a comma (from, to).";
  }
  if (f.value_type === "integer" && !NO_VALUE_OPS.has(f.op) && !LIST_OPS.has(f.op) && !RANGE_OPS.has(f.op) && !/^-?\d+$/.test(f.value.trim())) e.value = "Enter a whole number.";
  if (f.value_type === "boolean" && !NO_VALUE_OPS.has(f.op) && !/^(true|false)$/i.test(f.value.trim())) e.value = "Enter true or false.";
  if (f.value_type === "money" && !/^[A-Z]{3}$/.test(f.currency)) e.currency = "Use a 3-letter currency code.";
  if (!/^[A-Z][A-Z0-9_]{2,63}$/.test(f.reason_code.trim())) e.reason_code = "Use capitals, digits and underscores (e.g. PO_OVER_LIMIT).";
  if (f.message.trim().length < 3) e.message = "Explain the outcome in at least 3 characters.";
  if (!f.document_id) e.document_id = "Choose the source policy document.";
  if (f.page_number && !/^\d+$/.test(f.page_number)) e.page_number = "Enter a page number.";
  return e;
}

function CreateRuleForm({ onCreated, onCancel }) {
  const docs = useApi(() => listDocuments({ status: "ready" }), []);
  const factsApi = useApi(() => getRuleFacts().catch(() => null), []);
  const vocab = factVocabulary(factsApi.data);
  const [f, setF] = useState(emptyRule());
  const [errors, setErrors] = useState({});
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const knownFact = vocab.facts.find((x) => x.name === f.field.trim());
  function setField(e) {
    const value = e.target.value;
    const fact = vocab.facts.find((x) => x.name === value.trim());
    setF((s) => ({
      ...s, field: value,
      ...(fact?.type && VALUE_TYPES.includes(fact.type) ? { value_type: fact.type } : {}),
      ...(fact?.currency ? { currency: fact.currency } : {}),
    }));
  }
  const readyDocs = Array.isArray(docs.data?.documents) ? docs.data.documents : [];

  async function submit(e) {
    e.preventDefault();
    if (pending) return;
    const errs = validateRuleForm(f);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const doc = readyDocs.find((d) => d.document_id === f.document_id);
    const condition = { field: f.field.trim(), op: f.op, value_type: f.value_type };
    const value = parseValue(f);
    if (value !== undefined) condition.value = value;
    if (f.value_type === "money") condition.currency = f.currency;
    const evidence = { document_id: f.document_id, document_version: Number(doc?.version || 1), content_hash: doc?.content_hash || "" };
    if (f.page_number) evidence.page_number = Number(f.page_number);
    if (f.excerpt.trim()) evidence.excerpt = f.excerpt.trim();
    const body = { name: f.name.trim(), severity: f.severity, mandatory: f.mandatory, condition,
      outcome: { decision: f.decision, reason_code: f.reason_code.trim(), message: f.message.trim() }, evidence_refs: [evidence] };
    if (f.description.trim()) body.description = f.description.trim();
    setPending(true); setError(null);
    try {
      const created = await createRule(body);
      onCreated?.(created);
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  }

  const fld = (k, label, input, { required, help } = {}) => (
    <div className="field">
      <label htmlFor={`rule-${k}`} className={required ? "required" : undefined}>{label}</label>
      {input}
      {help && <span className="help">{help}</span>}
      {errors[k] && <span className="field-error">{errors[k]}</span>}
    </div>
  );
  const serverItems = errorList(error);

  return (
    <form className="card stack" onSubmit={submit} noValidate aria-labelledby="create-rule-title">
      <h2 id="create-rule-title">New rule candidate</h2>
      <p className="muted small">A new rule is a candidate only. It does nothing until another reviewer approves it.</p>
      <fieldset className="stack" disabled={pending} style={{ border: 0, padding: 0, margin: 0 }}>
        <div className="grid-2">
          {fld("name", "Rule name", <input id="rule-name" className="input" maxLength={120} value={f.name} onChange={set("name")} aria-invalid={Boolean(errors.name)} />, { required: true })}
          {fld("severity", "Severity", (
            <select id="rule-severity" className="select" value={f.severity} onChange={set("severity")}>
              {Object.entries(SEVERITY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>), { required: true })}
        </div>
        {fld("description", "Description", <textarea id="rule-description" className="textarea" maxLength={1000} value={f.description} onChange={set("description")} />)}
        <div className="grid-3">
          {fld("field", "Fact", (<>
            <input id="rule-field" className="input" list="rule-facts" value={f.field} onChange={setField} aria-invalid={Boolean(errors.field)} />
            <datalist id="rule-facts">{vocab.facts.map((x) => <option key={x.name} value={x.name}>{x.description || undefined}</option>)}</datalist></>),
          { required: true, help: knownFact?.description
            ? `${knownFact.description}${knownFact.type ? ` (${humanize(knownFact.type)})` : ""}`
            : vocab.fromServer && f.field.trim() ? "Not a known fact — the server will reject unknown facts." : undefined })}
          {fld("op", "Condition", (
            <select id="rule-op" className="select" value={f.op} onChange={set("op")}>
              {vocab.ops.map((o) => <option key={o} value={o}>{OP_LABEL[o] || humanize(o)}</option>)}
            </select>), { required: true })}
          {fld("value_type", "Value type", (
            <select id="rule-value_type" className="select" value={f.value_type} onChange={set("value_type")}>
              {VALUE_TYPES.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}
            </select>), { required: true })}
          {!NO_VALUE_OPS.has(f.op) && fld("value", "Value", <input id="rule-value" className="input" value={f.value} onChange={set("value")} aria-invalid={Boolean(errors.value)} />,
            { required: true, help: LIST_OPS.has(f.op) ? "Separate values with commas."
              : RANGE_OPS.has(f.op) ? "Enter two values separated by a comma (from, to)." : undefined })}
          {f.value_type === "money" && fld("currency", "Currency", <input id="rule-currency" className="input" maxLength={3} value={f.currency} onChange={set("currency")} aria-invalid={Boolean(errors.currency)} />, { required: true })}
        </div>
        <label className="checkbox"><input type="checkbox" checked={f.mandatory} onChange={set("mandatory")} />
          Mandatory — if the fact is missing, the request needs review</label>
        <div className="grid-3">
          {fld("decision", "Outcome", (
            <select id="rule-decision" className="select" value={f.decision} onChange={set("decision")}>
              {Object.entries(DECISION).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>), { required: true })}
          {fld("reason_code", "Reason code", <input id="rule-reason_code" className="input" maxLength={64} value={f.reason_code} onChange={set("reason_code")} aria-invalid={Boolean(errors.reason_code)} />, { required: true })}
        </div>
        {fld("message", "Outcome message", <textarea id="rule-message" className="textarea" maxLength={400} value={f.message} onChange={set("message")} aria-invalid={Boolean(errors.message)} />, { required: true })}
        <div className="grid-3">
          {fld("document_id", "Source document", (
            <select id="rule-document_id" className="select" value={f.document_id} onChange={set("document_id")} aria-invalid={Boolean(errors.document_id)}>
              <option value="">{docs.loading ? "Loading…" : "Choose a ready document"}</option>
              {readyDocs.map((d) => <option key={d.document_id} value={d.document_id}>{d.filename || d.document_id} (v{d.version})</option>)}
            </select>), { required: true, help: docs.error ? docs.error.message : "Only ready documents can be cited." })}
          {fld("page_number", "Page", <input id="rule-page_number" className="input" inputMode="numeric" value={f.page_number} onChange={set("page_number")} aria-invalid={Boolean(errors.page_number)} />)}
        </div>
        {fld("excerpt", "Quoted text from the source", <textarea id="rule-excerpt" className="textarea" maxLength={600} value={f.excerpt} onChange={set("excerpt")} />,
          { help: "Must match the document text exactly, or leave empty." })}
      </fieldset>
      {error && (
        <Notice tone="error">{error.message}
          {serverItems.length > 0 && <ul className="policy-list">{serverItems.map((x, i) => <li key={i}>{x}</li>)}</ul>}
        </Notice>
      )}
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={pending}>{pending ? "Creating…" : "Create candidate"}</button>
        <button type="button" className="btn" onClick={onCancel} disabled={pending}>Cancel</button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------ rules list
function RulesSection({ reviewer, isAdmin }) {
  const [filter, setFilter] = useState(reviewer ? "pending" : "");
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState(null);
  const apiStatus = filter && filter !== "pending" ? filter : undefined;
  const list = useApi(() => listRules({ status: apiStatus }), [apiStatus]);
  const pset = useApi(() => getActivePolicySet(), []);

  const all = Array.isArray(list.data?.rules) ? list.data.rules : [];
  const rows = filter === "pending" ? all.filter((r) => PENDING.includes(r.status)) : all;
  const counts = list.data?.counts || {};
  const pendingCount = PENDING.reduce((n, s) => n + Number(counts[s] || 0), 0);
  const activeVersion = pset.data?.policy_set_version ?? list.data?.active_policy_set_version ?? null;

  const refresh = useCallback(() => { list.reload(); pset.reload(); }, [list, pset]);

  const columns = [
    { key: "name", header: "Rule", render: (r) => (
      <div className="stack-sm" style={{ gap: 2 }}><span>{r.name}</span>
        <span className="muted small mono">{ruleRef(r)}</span></div>) },
    { key: "status", header: "Status", render: (r) => <RuleBadge status={r.status} /> },
    { key: "condition", header: "Triggers when", render: (r) => <span className="small">{r.condition_text || "—"}</span> },
    { key: "decision", header: "Outcome", render: (r) => DECISION[r.outcome?.decision] || humanize(r.outcome?.decision) },
    { key: "source", header: "Source", render: (r) => humanize(r.source) },
    { key: "created", header: "Updated", render: (r) => formatDateTime(r.approved_at || r.reviewed_at || r.created_at) },
  ];

  return (
    <div className="stack">
      <ActiveSetBanner pset={pset.data} error={pset.error} loading={pset.loading} onRetry={pset.reload} />
      <Notice tone="info">{STALE_WARNING}</Notice>
      {created && (
        <Notice tone="success" title="Candidate created" role="status">
          {created.name} ({ruleRef(created)}) is {RULE_STATE[created.status]?.label.toLowerCase() || humanize(created.status)}.
          It is not active until another reviewer approves it.
        </Notice>
      )}
      <div className="policy-toolbar">
        <div className="field">
          <label htmlFor="rule-status-filter">Show</label>
          <select id="rule-status-filter" className="select" value={filter} onChange={(e) => { setFilter(e.target.value); setSelected(null); }}>
            {FILTERS.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
          </select>
        </div>
        <button type="button" className="btn" onClick={refresh} disabled={list.loading}>Refresh</button>
        {isAdmin && !creating && (
          <button type="button" className="btn btn-primary" onClick={() => { setCreated(null); setCreating(true); }}>New rule</button>
        )}
        {list.data && <span className="muted small">{Number(counts.approved || 0)} active · {pendingCount} pending · {Number(counts.rejected || 0)} rejected</span>}
      </div>

      {creating && <CreateRuleForm onCancel={() => setCreating(false)}
        onCreated={(r) => { setCreating(false); setCreated(r); list.reload(); }} />}

      <DataTable caption="Policy rules" columns={columns} rows={rows} rowKey={ruleRef} loading={list.loading}
        error={list.error} onRetry={list.reload} onRowClick={(r) => setSelected(r)}
        empty={<EmptyState title={filter === "pending" ? "No rules waiting for review" : "No rules match"}
          description="Rules extracted from policy documents or created by an admin appear here." />} />

      {selected && <RuleDetail key={ruleRef(selected)} rule={selected} reviewer={reviewer} activeVersion={activeVersion}
        onChanged={refresh} onClose={() => setSelected(null)} />}
    </div>
  );
}

export default function PolicyRulesPage({ reviewer = false }) {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState("rules");
  if (reviewer) {
    return (
      <div className="stack">
        <PageHeader title="Policy review"
          description="Review candidate policy rules and extracted rate-card rows. Nothing becomes active without your approval." />
        <Tabs tabs={[{ id: "rules", label: "Rules" }, { id: "rates", label: "Rate cards" }]} value={tab} onChange={setTab}
          label="Policy review sections" />
        {tab === "rules" ? <RulesSection reviewer isAdmin={false} /> : <RateCardsSection />}
      </div>
    );
  }
  return (
    <div className="stack">
      <PageHeader title="Policy rules"
        description="Rules the request checks use. Candidate rules need approval by a policy reviewer before they become active." />
      <RulesSection reviewer={false} isAdmin={isAdmin} />
    </div>
  );
}
