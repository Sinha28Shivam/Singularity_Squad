import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDate, formatDateTime, formatMoney, humanize } from "../lib/format";
import {
  RFQ_STATUSES, createRfq, listApprovedRecipients, listRfqs, listValidatedTickets, loadTicketSnapshot,
  localOffsetLabel, localToIsoWithOffset,
} from "../api/vendorSourcing";
import "../styles/pages/sourcing.css";

const SOURCING_BANNER = "Decision support only — no supplier is contacted and nothing is awarded.";

export default function RfqsPage() {
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApi(() => listRfqs({ status: status || undefined }), [status]);
  const rows = data?.items || [];

  const cols = [
    { key: "rfq_id", header: "RFQ", render: (r) => <Link to={`/admin/rfqs/${encodeURIComponent(r.rfq_id)}`} className="mono">{r.rfq_id}</Link> },
    { key: "request_id", header: "Request", render: (r) => <span className="mono">{r.request_id || "—"}</span> },
    { key: "service", header: "Service", render: (r) => humanize(r.service) },
    { key: "status", header: "Status", render: (r) => <StatusBadge kind="vendor" status={r.status} /> },
    { key: "response_deadline", header: "Response deadline", render: (r) => formatDateTime(r.response_deadline) },
    { key: "quotes_received", header: "Quotes", align: "right", render: (r) => r.quotes_received ?? "—" },
    { key: "budget_limit", header: "Budget", align: "right", render: (r) => formatMoney(r.budget_limit, r.currency || "INR") },
  ];

  return (
    <div className="stack">
      <PageHeader title="RFQs" description="Internal request-for-quotation records created from validated requests."
        actions={!creating && <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>New RFQ</button>} />
      <Notice tone="info" title={SOURCING_BANNER}>Creating an RFQ records an internal sourcing request only.</Notice>
      {creating && (
        <CreateRfqForm onCancel={() => setCreating(false)}
          onCreated={(rfq) => {
            setCreating(false);
            if (rfq?.rfq_id) navigate(`/admin/rfqs/${encodeURIComponent(rfq.rfq_id)}`);
            else reload();
          }} />
      )}
      <section className="card stack-sm" aria-labelledby="rfq-list-h">
        <div className="row-between">
          <h2 id="rfq-list-h" className="sourcing-section-title">RFQ records</h2>
          <div className="field" style={{ minWidth: 200 }}>
            <label htmlFor="rfq-status">Status</label>
            <select id="rfq-status" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">All statuses</option>
              {RFQ_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
            </select>
          </div>
        </div>
        <DataTable caption="RFQ records" columns={cols} rows={rows} rowKey="rfq_id" loading={loading} error={error}
          onRetry={reload} onRowClick={(r) => navigate(`/admin/rfqs/${encodeURIComponent(r.rfq_id)}`)}
          empty={<EmptyState title="No RFQs yet" description="Create an RFQ from a validated request." />} />
      </section>
    </div>
  );
}

function defaultDeadline(deliveryDate) {
  const d = new Date(`${String(deliveryDate).slice(0, 10)}T17:00`);
  if (Number.isNaN(d.getTime())) return "";
  d.setDate(d.getDate() - 1);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T17:00`;
}

function CreateRfqForm({ onCreated, onCancel }) {
  const tickets = useApi(() => listValidatedTickets(), []);
  const recipients = useApi(() => listApprovedRecipients(), []);
  const [ticketId, setTicketId] = useState("");
  const [snap, setSnap] = useState({ loading: false, request: null, problem: "", error: null });
  const [form, setForm] = useState({ location: "", warranty: "0", warrantyScope: "", compliance: "", deadline: "", picked: [] });
  const [errors, setErrors] = useState({});
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState(null);

  useEffect(() => {
    if (!ticketId) { setSnap({ loading: false, request: null, problem: "", error: null }); return undefined; }
    let live = true;
    setSnap({ loading: true, request: null, problem: "", error: null });
    loadTicketSnapshot(ticketId).then((res) => {
      if (!live) return;
      setSnap({ loading: false, request: res.request || null, problem: res.problem || "", error: null });
      if (res.request) {
        setForm((f) => ({ ...f, location: res.request.location || "", deadline: defaultDeadline(res.request.date) }));
      }
    }).catch((error) => { if (live) setSnap({ loading: false, request: null, problem: "", error }); });
    return () => { live = false; };
  }, [ticketId]);

  const req = snap.request;
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const togglePick = (id) => setForm({ ...form, picked: form.picked.includes(id) ? form.picked.filter((x) => x !== id) : [...form.picked, id] });

  async function submit(e) {
    e.preventDefault();
    if (!req || pending) return;
    const errs = {};
    const location = (req.location || form.location).trim();
    if (location.length < 2) errs.location = "Enter the delivery location.";
    const w = Number(form.warranty);
    if (!Number.isInteger(w) || w < 0 || w > 120) errs.warranty = "Enter whole months between 0 and 120.";
    const deadlineIso = localToIsoWithOffset(form.deadline);
    if (!deadlineIso) errs.deadline = "Choose a response deadline.";
    if (!form.picked.length) errs.picked = "Select at least one approved recipient.";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const body = {
      request_id: ticketId,
      service: String(req.service),
      quantity: String(req.attendees),
      budget_limit: String(req.budget_limit),
      currency: String(req.currency || "INR").toUpperCase(),
      delivery_date: String(req.date).slice(0, 10),
      delivery_location: location,
      warranty_months_required: w,
      compliance_requirements: [...new Set(form.compliance.split(",").map((s) => s.trim()).filter(Boolean))],
      response_deadline: deadlineIso,
      candidate_ids: form.picked,
    };
    if (form.warrantyScope.trim()) body.warranty_scope_required = form.warrantyScope.trim();
    setPending(true); setServerError(null);
    try {
      onCreated(await createRfq(body));
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  }

  const err = (k) => errors[k] && <span className="field-error">{errors[k]}</span>;
  return (
    <form className="card stack" onSubmit={submit} noValidate aria-labelledby="new-rfq-h">
      <div>
        <h2 id="new-rfq-h" className="sourcing-section-title">Create an RFQ from a validated request</h2>
        <p className="muted small">Scope is copied from the request's validated details and re-checked by the server.</p>
      </div>
      {tickets.loading && !tickets.data ? <LoadingState label="Loading validated requests…" /> :
        tickets.error ? <ErrorState error={tickets.error} onRetry={tickets.reload} /> :
          !(tickets.data || []).length ? <EmptyState title="No validated requests"
            description="Only requests whose checks passed (or passed with warnings) can be used for an RFQ." /> : (
            <div className="field">
              <label htmlFor="rfq-ticket" className="required">Validated request</label>
              <select id="rfq-ticket" className="select" value={ticketId} onChange={(e) => setTicketId(e.target.value)}>
                <option value="">Select a request</option>
                {tickets.data.map((t) => (
                  <option key={t.ticket_id} value={t.ticket_id}>
                    {t.ticket_id}{t.title ? ` · ${t.title}` : ""} ({t.final_status === "pass" ? "passed" : "needs review"})
                  </option>
                ))}
              </select>
            </div>
          )}
      {snap.loading && <LoadingState label="Loading request details…" />}
      {snap.error && <InlineError error={snap.error} />}
      {snap.problem && <Notice tone="warning" title="This request can't be used for an RFQ">{snap.problem}</Notice>}
      {req && (
        <>
          <dl className="dl" aria-label="Prefilled from the validated request">
            <dt>Service</dt><dd>{humanize(req.service)}</dd>
            <dt>Quantity</dt><dd>{req.attendees}</dd>
            <dt>Budget limit</dt><dd>{formatMoney(req.budget_limit, req.currency || "INR")}</dd>
            <dt>Delivery date</dt><dd>{formatDate(req.date)}</dd>
            {req.location && <><dt>Delivery location</dt><dd>{req.location}</dd></>}
          </dl>
          <div className="grid-2">
            {!req.location && (
              <div className="field">
                <label htmlFor="rfq-location" className="required">Delivery location</label>
                <input id="rfq-location" className="input" value={form.location} onChange={set("location")} maxLength={200} />
                {err("location")}
              </div>
            )}
            <div className="field">
              <label htmlFor="rfq-warranty">Warranty required (months)</label>
              <input id="rfq-warranty" type="number" min={0} max={120} step={1} className="input" value={form.warranty} onChange={set("warranty")} />
              {err("warranty")}
            </div>
            <div className="field">
              <label htmlFor="rfq-wscope">Warranty scope required</label>
              <input id="rfq-wscope" className="input" value={form.warrantyScope} onChange={set("warrantyScope")} maxLength={1000} />
            </div>
            <div className="field">
              <label htmlFor="rfq-compliance">Compliance requirements</label>
              <input id="rfq-compliance" className="input" value={form.compliance} onChange={set("compliance")} aria-describedby="rfq-compliance-help" />
              <span className="help" id="rfq-compliance-help">Separate with commas, e.g. FSSAI, GST.</span>
            </div>
            <div className="field">
              <label htmlFor="rfq-deadline" className="required">Response deadline</label>
              <input id="rfq-deadline" type="datetime-local" className="input" value={form.deadline} onChange={set("deadline")} aria-describedby="rfq-deadline-help" />
              <span className="help" id="rfq-deadline-help">Your local time{localOffsetLabel(form.deadline) ? ` (${localOffsetLabel(form.deadline)})` : ""}. Sent with the time-zone offset.</span>
              {err("deadline")}
            </div>
          </div>
          <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="label required">Recipients (approved vendors only)</legend>
            {recipients.loading && !recipients.data ? <LoadingState label="Loading approved vendors…" /> :
              recipients.error ? <ErrorState error={recipients.error} onRetry={recipients.reload} /> :
                !(recipients.data || []).length ? <p className="muted small">No approved vendors or approved candidates yet.</p> : (
                  <div className="recipient-list">
                    {recipients.data.map((r) => (
                      <label key={r.id} htmlFor={`rcp-${r.id}`}>
                        <input id={`rcp-${r.id}`} type="checkbox" className="checkbox" checked={form.picked.includes(r.id)} onChange={() => togglePick(r.id)} />
                        <span><span className="mono">{r.id}</span> · {r.name || "—"} <span className="muted small">({r.kind})</span></span>
                      </label>
                    ))}
                  </div>
                )}
            {err("picked")}
          </fieldset>
        </>
      )}
      <InlineError error={serverError} />
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={!req || pending}>{pending ? "Creating…" : "Create RFQ record"}</button>
        <button type="button" className="btn" onClick={onCancel} disabled={pending}>Cancel</button>
      </div>
    </form>
  );
}
