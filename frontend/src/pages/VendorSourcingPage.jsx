import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import Tabs from "../components/Tabs";
import ConfirmDialog from "../components/ConfirmDialog";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDate, formatDateTime, formatMoney, humanize } from "../lib/format";
import {
  CANDIDATE_STATUSES, MIN_REASON, allowedDecisions, candidateEvents, createCandidate, decideCandidate,
  getCandidate, getSourcingMetrics, listCandidates, searchVendors,
} from "../api/vendorSourcing";
import "../styles/pages/sourcing.css";

const SOURCING_BANNER = "Decision support only — no supplier is contacted and nothing is awarded.";
const PAGE_SIZE = 20;
const MAX_EVIDENCE = 10;
const DECISION_COPY = {
  approve: { label: "Approve", title: "Approve this candidate?", tone: "primary",
    description: "Approved candidates can be invited to internal RFQ records. No supplier is contacted." },
  reject: { label: "Reject", title: "Reject this candidate?", tone: "danger",
    description: "A rejected candidate cannot be approved later. No supplier is contacted." },
  suspend: { label: "Suspend", title: "Suspend this candidate?", tone: "danger",
    description: "A suspended candidate cannot be invited to new RFQs until approved again." },
};

const list = (v) => (Array.isArray(v) && v.length ? v.join(", ") : "—");
const splitList = (s) => [...new Set(String(s || "").split(",").map((x) => x.trim()).filter(Boolean))];

export default function VendorSourcingPage() {
  const [tab, setTab] = useState("directory");
  const [selected, setSelected] = useState(null);
  const openCandidate = useCallback((id) => { setSelected(id); setTab("candidates"); }, []);
  return (
    <div className="stack">
      <PageHeader title="Vendor sourcing"
        description="Search the internal vendor catalog, review vendor candidates and manage RFQ records."
        actions={<Link className="btn" to="/admin/rfqs">RFQs</Link>} />
      <Notice tone="info" title={SOURCING_BANNER} />
      <Tabs label="Vendor sourcing sections" value={tab} onChange={setTab}
        tabs={[{ id: "directory", label: "Vendor directory" }, { id: "candidates", label: "Candidates" }]} />
      {tab === "directory"
        ? <DirectoryTab onOpenCandidate={openCandidate} />
        : <CandidatesTab selected={selected} onSelect={setSelected} />}
      <ActivitySection />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ activity (metrics)
const METRIC_LABELS = {
  candidates_submitted: "Candidates registered",
  decisions_by_type: "Candidate decisions",
  rfqs_created: "RFQs created",
  quotes_by_type: "Quotes recorded",
  verifications: "Quote verifications",
  ineligible_reasons: "Quotes excluded from ranking",
  comparisons: "Comparisons run",
  persistence_failures: "Save failures",
};

export function metricRows(data) {
  const counters = data?.counters && typeof data.counters === "object" ? data.counters : {};
  return Object.entries(counters).map(([name, v]) => {
    const total = typeof v === "number" ? v : Number(v?.total ?? 0);
    const byLabel = v && typeof v === "object" && v.by_label && typeof v.by_label === "object" ? Object.entries(v.by_label) : [];
    return { name, label: METRIC_LABELS[name] || humanize(name), total: Number.isFinite(total) ? total : 0, byLabel };
  });
}

function ActivitySection() {
  const [state, setState] = useState({ data: null, error: null, loading: false, opened: false });
  const reload = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null, opened: true }));
    try {
      const data = await getSourcingMetrics();
      setState({ data, error: null, loading: false, opened: true });
    } catch (error) {
      setState({ data: null, error, loading: false, opened: true });
    }
  }, []);
  const { data, error, loading, opened } = state;
  const rows = metricRows(data);
  return (
    <details className="disclosure" onToggle={(e) => { if (e.currentTarget.open && !opened && !loading) reload(); }}>
      <summary>Activity since the API started</summary>
      <section className="stack-sm" aria-labelledby="sourcing-activity-h">
      <div className="row-between">
        <h2 id="sourcing-activity-h" className="sourcing-section-title">Activity</h2>
        <button type="button" className="btn btn-ghost btn-sm" onClick={reload} disabled={loading}>Refresh</button>
      </div>
      <p className="muted small">Counts since the API started. They reset when the API restarts and are not a business report.</p>
      {!opened ? null : loading && !data ? <LoadingState label="Loading activity…" /> : error ? <ErrorState error={error} onRetry={reload} title="Activity is unavailable" /> :
        rows.length === 0 ? <p className="muted small">No activity recorded since the API started.</p> : (
          <>
            <div className="grid-3" data-testid="sourcing-activity">
              {rows.map((r) => (
                <div key={r.name} className="stat">
                  <span className="stat-value">{r.total}</span>
                  <span className="stat-label">{r.label}</span>
                </div>
              ))}
            </div>
            {rows.some((r) => r.byLabel.length) && (
              <details className="disclosure">
                <summary>Breakdown</summary>
                <ul className="event-list">
                  {rows.filter((r) => r.byLabel.length).map((r) => (
                    <li key={r.name}><strong>{r.label}</strong>
                      <span className="muted"> · {r.byLabel.map(([k, n]) => `${humanize(k)}: ${n}`).join(" · ")}</span></li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </section>
    </details>
  );
}

// ------------------------------------------------------------------------------------------ directory
function DirectoryTab({ onOpenCandidate }) {
  const [draft, setDraft] = useState({ q: "", service: "", status: "" });
  const [filters, setFilters] = useState(draft);
  const { data, error, loading, reload } = useApi(() => searchVendors(filters), [filters]);

  const internalCols = [
    { key: "vendor_id", header: "Vendor ID", render: (v) => <span className="mono">{v.vendor_id}</span> },
    { key: "name", header: "Name" },
    { key: "approved", header: "Status", render: (v) => <StatusBadge kind="vendor" status={v.approved ? "approved" : "pending_review"}
      label={v.approved ? "Approved" : "Not approved"} /> },
    { key: "services", header: "Services", render: (v) => list(v.services) },
    { key: "capacity", header: "Capacity", align: "right", render: (v) => v.capacity ?? "—" },
    { key: "rate", header: "Rate per person", align: "right",
      render: (v) => (v.rate_per_person == null ? "—" : formatMoney(v.rate_per_person, v.currency || "INR")) },
  ];
  const candidateCols = [
    { key: "candidate_id", header: "Candidate ID", render: (c) => <span className="mono">{c.candidate_id}</span> },
    { key: "name", header: "Name" },
    { key: "status", header: "Status", render: (c) => <StatusBadge kind="vendor" status={c.status} /> },
    { key: "services", header: "Services", render: (c) => list(c.services) },
    { key: "regions", header: "Regions", render: (c) => list(c.regions) },
    { key: "source", header: "Source", render: (c) => humanize(c.source) },
  ];

  return (
    <div className="stack">
      <form className="card sourcing-filters" onSubmit={(e) => { e.preventDefault(); setFilters({ ...draft }); }}>
        <div className="field">
          <label htmlFor="dir-q">Search</label>
          <input id="dir-q" className="input" value={draft.q} maxLength={120} placeholder="Name, ID or service"
            onChange={(e) => setDraft({ ...draft, q: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor="dir-service">Service</label>
          <input id="dir-service" className="input" value={draft.service} maxLength={80}
            onChange={(e) => setDraft({ ...draft, service: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor="dir-status">Status</label>
          <select id="dir-status" className="select" value={draft.status}
            onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
            <option value="">Any</option>
            {CANDIDATE_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
          </select>
        </div>
        <button type="submit" className="btn btn-primary">Search</button>
      </form>
      <p className="muted small">
        Only the internal catalog and the candidate registry are searched. No external search is run and no supplier is contacted.
      </p>
      {loading && !data ? <LoadingState /> : error ? <ErrorState error={error} onRetry={reload} /> : (
        <>
          <section className="card stack-sm" aria-labelledby="internal-h">
            <div className="row-between">
              <h2 id="internal-h" className="sourcing-section-title">Internal catalog (read-only)</h2>
              <span className="muted small">{(data?.internal_vendors || []).length} vendors</span>
            </div>
            <DataTable caption="Internal catalog vendors" columns={internalCols} rows={data?.internal_vendors || []}
              rowKey="vendor_id" empty={<EmptyState title="No catalog vendors match" />} />
          </section>
          <section className="card stack-sm" aria-labelledby="registry-h">
            <div className="row-between">
              <h2 id="registry-h" className="sourcing-section-title">Candidate registry</h2>
              <span className="muted small">{data?.candidates_total ?? (data?.candidates || []).length} candidates</span>
            </div>
            <p className="muted small">Candidates are not approved vendors until a person approves them.</p>
            <DataTable caption="Vendor candidates" columns={candidateCols} rows={data?.candidates || []}
              rowKey="candidate_id" onRowClick={(c) => onOpenCandidate(c.candidate_id)}
              empty={<EmptyState title="No candidates match" />} />
          </section>
          {data?.note && <p className="muted small">{String(data.note).slice(0, 300)}</p>}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ candidates
function CandidatesTab({ selected, onSelect }) {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [showForm, setShowForm] = useState(false);
  const [flash, setFlash] = useState(null);
  const { data, error, loading, reload } = useApi(
    () => listCandidates({ status: status || undefined, limit: PAGE_SIZE, offset }), [status, offset]);
  const items = data?.items || [];
  const total = data?.total ?? items.length;

  const cols = [
    { key: "candidate_id", header: "Candidate ID", render: (c) => <span className="mono">{c.candidate_id}</span> },
    { key: "name", header: "Name" },
    { key: "status", header: "Status", render: (c) => <StatusBadge kind="vendor" status={c.status} /> },
    { key: "services", header: "Services", render: (c) => list(c.services) },
    { key: "created_at", header: "Registered", render: (c) => formatDate(c.created_at) },
  ];

  return (
    <div className="stack">
      {flash && <Notice tone="success">{flash}</Notice>}
      <div className="row-between">
        <div className="field" style={{ minWidth: 220 }}>
          <label htmlFor="cand-status">Status</label>
          <select id="cand-status" className="select" value={status}
            onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">All statuses</option>
            {CANDIDATE_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
          </select>
        </div>
        <button type="button" className="btn" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
          {showForm ? "Close form" : "Register candidate"}
        </button>
      </div>
      {showForm && (
        <RegisterCandidateForm onCancel={() => setShowForm(false)} onCreated={(c) => {
          setShowForm(false);
          setFlash(`Candidate ${c?.candidate_id || ""} registered as pending review. No vendor was contacted.`);
          reload();
          if (c?.candidate_id) onSelect(c.candidate_id);
        }} />
      )}
      <section className="card stack-sm" aria-labelledby="cand-list-h">
        <h2 id="cand-list-h" className="sourcing-section-title">Candidates</h2>
        <DataTable caption="Vendor candidates" columns={cols} rows={items} rowKey="candidate_id" loading={loading}
          error={error} onRetry={reload} onRowClick={(c) => onSelect(c.candidate_id)}
          empty={<EmptyState title="No candidates" description="Register a candidate to start a review." />} />
        {!error && total > 0 && (
          <div className="sourcing-pager">
            <span className="muted small">Showing {offset + 1}–{offset + items.length} of {total}</span>
            <div className="row">
              <button type="button" className="btn btn-sm" disabled={offset === 0 || loading}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
              <button type="button" className="btn btn-sm" disabled={data?.next_offset == null || loading}
                onClick={() => setOffset(data.next_offset)}>Next</button>
            </div>
          </div>
        )}
      </section>
      {selected && <CandidateDetail key={selected} candidateId={selected} onClose={() => onSelect(null)}
        onChanged={(c) => { setFlash(`Decision recorded: ${c?.name || selected} is now ${humanize(c?.status).toLowerCase()}. No vendor was contacted.`); reload(); }} />}
    </div>
  );
}

function RegisterCandidateForm({ onCreated, onCancel }) {
  const [form, setForm] = useState({ name: "", services: "", regions: "", contact_email: "", source_reference: "", notes: "" });
  const [evidence, setEvidence] = useState([]);
  const [errors, setErrors] = useState({});
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState(null);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const setEv = (i, k, v) => setEvidence(evidence.map((row, j) => (j === i ? { ...row, [k]: v } : row)));

  function validate() {
    const errs = {};
    if (form.name.trim().length < 2) errs.name = "Enter a vendor name (at least 2 characters).";
    if (!splitList(form.services).length) errs.services = "Enter at least one service.";
    if (form.contact_email.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.contact_email.trim())) errs.contact_email = "Enter a valid email address.";
    evidence.forEach((row, i) => {
      const t = row.document_type.trim(); const r = row.reference.trim();
      if (!t && !r && !row.expires_on) return;
      if (t.length < 2) errs[`ev${i}t`] = "Document type is required (at least 2 characters).";
      if (!r) errs[`ev${i}r`] = "Reference is required.";
    });
    return errs;
  }

  async function submit(e) {
    e.preventDefault();
    const errs = validate();
    setErrors(errs);
    if (Object.keys(errs).length || pending) return;
    const body = { name: form.name.trim(), services: splitList(form.services), regions: splitList(form.regions),
      source: "manual", notes: form.notes.trim() };
    if (form.contact_email.trim()) body.contact_email = form.contact_email.trim();
    if (form.source_reference.trim()) body.source_reference = form.source_reference.trim();
    const ev = evidence.filter((r) => r.document_type.trim() || r.reference.trim()).map((r) => {
      const row = { document_type: r.document_type.trim(), reference: r.reference.trim(), verified: false };
      if (r.expires_on) row.expires_on = r.expires_on;
      return row;
    });
    if (ev.length) body.compliance_evidence = ev;
    setPending(true); setServerError(null);
    try {
      const created = await createCandidate(body);
      onCreated(created);
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  }

  const fieldErr = (k) => errors[k] && <span className="field-error" id={`${k}-err`}>{errors[k]}</span>;
  return (
    <form className="card stack" onSubmit={submit} noValidate aria-labelledby="reg-h">
      <div>
        <h2 id="reg-h" className="sourcing-section-title">Register a vendor candidate</h2>
        <p className="muted small">New candidates always start as pending review. Registering does not approve or contact them.</p>
      </div>
      <div className="grid-2">
        <div className="field">
          <label htmlFor="c-name" className="required">Vendor name</label>
          <input id="c-name" className="input" value={form.name} onChange={set("name")} maxLength={160}
            aria-invalid={Boolean(errors.name)} aria-describedby={errors.name ? "name-err" : undefined} />
          {fieldErr("name")}
        </div>
        <div className="field">
          <label htmlFor="c-services" className="required">Services</label>
          <input id="c-services" className="input" value={form.services} onChange={set("services")}
            aria-invalid={Boolean(errors.services)} aria-describedby="c-services-help" />
          <span className="help" id="c-services-help">Separate with commas, e.g. catering, audio-visual.</span>
          {fieldErr("services")}
        </div>
        <div className="field">
          <label htmlFor="c-regions">Regions</label>
          <input id="c-regions" className="input" value={form.regions} onChange={set("regions")} />
        </div>
        <div className="field">
          <label htmlFor="c-email">Contact email</label>
          <input id="c-email" type="email" className="input" value={form.contact_email} onChange={set("contact_email")}
            maxLength={254} aria-invalid={Boolean(errors.contact_email)} />
          {fieldErr("contact_email")}
        </div>
      </div>
      <div className="field">
        <label htmlFor="c-source">Source / provenance reference</label>
        <input id="c-source" className="input" value={form.source_reference} onChange={set("source_reference")} maxLength={500} />
        <span className="help">Where this vendor came from (referral, website, prior contract). Recorded for review.</span>
      </div>
      <div className="field">
        <label htmlFor="c-notes">Notes</label>
        <textarea id="c-notes" className="textarea" value={form.notes} onChange={set("notes")} maxLength={2000} />
      </div>
      <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="label">Compliance evidence</legend>
        <p className="muted small">References only — submitted evidence is never treated as verified.</p>
        {evidence.map((row, i) => (
          <div className="evidence-row" key={i}>
            <div className="field">
              <label htmlFor={`ev-${i}-type`} className="required">Document type {i + 1}</label>
              <input id={`ev-${i}-type`} className="input" value={row.document_type} maxLength={120}
                onChange={(e) => setEv(i, "document_type", e.target.value)} aria-invalid={Boolean(errors[`ev${i}t`])} />
              {fieldErr(`ev${i}t`)}
            </div>
            <div className="field">
              <label htmlFor={`ev-${i}-ref`} className="required">Reference {i + 1}</label>
              <input id={`ev-${i}-ref`} className="input" value={row.reference} maxLength={500}
                onChange={(e) => setEv(i, "reference", e.target.value)} aria-invalid={Boolean(errors[`ev${i}r`])} />
              {fieldErr(`ev${i}r`)}
            </div>
            <div className="field">
              <label htmlFor={`ev-${i}-exp`}>Expires on {i + 1}</label>
              <input id={`ev-${i}-exp`} type="date" className="input" value={row.expires_on}
                onChange={(e) => setEv(i, "expires_on", e.target.value)} />
            </div>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEvidence(evidence.filter((_, j) => j !== i))}>
              Remove evidence {i + 1}
            </button>
          </div>
        ))}
        {evidence.length < MAX_EVIDENCE && (
          <div><button type="button" className="btn btn-sm"
            onClick={() => setEvidence([...evidence, { document_type: "", reference: "", expires_on: "" }])}>Add evidence</button></div>
        )}
      </fieldset>
      {serverError && (serverError.status === 409
        ? <Notice tone="warning" title="Possible duplicate">{serverError.message}</Notice>
        : <InlineError error={serverError} />)}
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={pending}>{pending ? "Registering…" : "Register candidate"}</button>
        <button type="button" className="btn" onClick={onCancel} disabled={pending}>Cancel</button>
      </div>
    </form>
  );
}

function CandidateDetail({ candidateId, onClose, onChanged }) {
  const { data: cand, error, loading, reload, setData } = useApi(() => getCandidate(candidateId), [candidateId]);
  const events = useApi(() => candidateEvents(candidateId), [candidateId]);
  const [decision, setDecision] = useState(null);
  const cancelDecision = useCallback(() => setDecision(null), []);

  if (loading && !cand) return <section className="card"><LoadingState label="Loading candidate…" /></section>;
  if (error) return <section className="card"><ErrorState error={error} onRetry={reload} /></section>;
  if (!cand) return null;
  const allowed = allowedDecisions(cand).filter((d) => DECISION_COPY[d]);
  const evidence = Array.isArray(cand.compliance_evidence) ? cand.compliance_evidence : [];
  const history = Array.isArray(cand.status_history) ? cand.status_history : [];
  const copy = decision ? DECISION_COPY[decision] : null;

  return (
    <section className="card stack" aria-labelledby="cand-detail-h">
      <div className="row-between">
        <div className="row">
          <h2 id="cand-detail-h" className="sourcing-section-title">{cand.name}</h2>
          <StatusBadge kind="vendor" status={cand.status} />
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Close details</button>
      </div>
      {cand.audit_pending && <Notice tone="warning">Saved, but the audit record is still being delivered. It will be repaired automatically.</Notice>}
      <dl className="dl">
        <dt>Candidate ID</dt><dd className="mono">{cand.candidate_id}</dd>
        <dt>Services</dt><dd>{list(cand.services)}</dd>
        <dt>Regions</dt><dd>{list(cand.regions)}</dd>
        <dt>Contact email</dt><dd>{cand.contact_email || "—"}</dd>
        <dt>Notes</dt><dd>{cand.notes || "—"}</dd>
        <dt>Last review</dt>
        <dd>{cand.reviewed_by ? `${cand.reviewed_by} · ${formatDateTime(cand.reviewed_at)}${cand.review_reason ? ` — ${cand.review_reason}` : ""}` : "Not reviewed yet"}</dd>
      </dl>
      <div className="stack-sm">
        <h3 className="label">Provenance</h3>
        <dl className="dl">
          <dt>Source</dt><dd>{humanize(cand.source)}</dd>
          <dt>Source reference</dt><dd>{cand.source_reference || "—"}</dd>
          <dt>Registered by</dt><dd>{cand.created_by || "—"} · {formatDateTime(cand.created_at)}</dd>
        </dl>
      </div>
      <div className="stack-sm">
        <h3 className="label">Compliance evidence</h3>
        <DataTable caption="Compliance evidence" rows={evidence} rowKey={(e) => `${e.document_type}-${e.reference}`}
          empty={<p className="muted small">No compliance evidence recorded.</p>}
          columns={[
            { key: "document_type", header: "Document type" },
            { key: "reference", header: "Reference" },
            { key: "expires_on", header: "Expires", render: (e) => formatDate(e.expires_on) },
            { key: "verified", header: "Verification", render: (e) => (e.verified
              ? <StatusBadge kind="vendor" status="verified" label={`Verified${e.verified_by ? ` by ${e.verified_by}` : ""}`} />
              : <StatusBadge kind="vendor" status="pending_verification" label="Not verified" />) },
          ]} />
      </div>
      <div className="stack-sm">
        <h3 className="label">Status history</h3>
        {history.length ? (
          <ul className="event-list">
            {history.map((h, i) => (
              <li key={i}>
                <strong>{h.from_status ? `${humanize(h.from_status)} → ` : ""}{humanize(h.to_status)}</strong>
                <span className="muted"> · {h.actor || "—"} · {formatDateTime(h.at)}</span>
                {h.reason && <div className="muted small">{h.reason}</div>}
              </li>
            ))}
          </ul>
        ) : <p className="muted small">No status changes yet.</p>}
      </div>
      <details className="disclosure">
        <summary>Audit events</summary>
        {events.loading && !events.data ? <LoadingState /> : events.error ? <ErrorState error={events.error} onRetry={events.reload} /> :
          (events.data || []).length === 0 ? <p className="muted small">No events recorded.</p> : (
            <ul className="event-list">
              {events.data.map((e) => (
                <li key={e.event_id || `${e.event_type}-${e.created_at}`}>
                  <strong>{humanize(e.event_type || e.action)}</strong>
                  <span className="muted"> · {e.actor || "—"} · {formatDateTime(e.created_at || e.at)}</span>
                  {e.reason && <div className="muted small">{e.reason}</div>}
                </li>
              ))}
            </ul>
          )}
      </details>
      <div className="stack-sm">
        <h3 className="label">Review decision</h3>
        {allowed.length ? (
          <div className="row">
            {allowed.map((d) => (
              <button key={d} type="button" className={`btn ${DECISION_COPY[d].tone === "danger" ? "btn-danger" : "btn-primary"}`}
                onClick={() => setDecision(d)}>{DECISION_COPY[d].label}</button>
            ))}
          </div>
        ) : <p className="muted small">No further decisions are available for a {humanize(cand.status).toLowerCase()} candidate.</p>}
      </div>
      <ConfirmDialog open={Boolean(decision)} title={copy?.title} description={copy?.description}
        confirmLabel={copy?.label} tone={copy?.tone} requireReason={MIN_REASON} reasonLabel="Reason for this decision"
        onCancel={cancelDecision}
        onConfirm={async (reason) => {
          const updated = await decideCandidate(candidateId, decision, reason);
          setDecision(null);
          if (updated?.candidate_id) setData(updated); else reload();
          events.reload();
          onChanged(updated);
        }} />
    </section>
  );
}
