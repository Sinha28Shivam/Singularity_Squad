import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import { ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import {
  ApprovalPanel, DRAFT_PO_NOTE, NotesList, PolicyEvidence, SubmissionDetails,
} from "../components/WorkflowResult";
import { RequestForm } from "./NewRequestPage";
import { useApi } from "../lib/useApi";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import {
  addMyNote, APPROVAL_TICKET_STATUSES, formFromSubmission, getMyTicket, getTicketApproval, updateMyTicket,
  USER_EDITABLE_STATUSES, userNextAction,
} from "../api/requests";
import "../styles/pages/requests.css";

const EDIT_HINT = {
  submitted: "You can still change the details before an admin reviews the request.",
  needs_info: "Update the details to answer the questions above, then resubmit.",
  changes_requested: "The approver asked for changes. Update the details and resubmit — an admin will re-run the checks and a new approval version will be created.",
  approval_expired: "The approval request expired without a decision. You can update and resubmit the request; an admin can also re-run it as is.",
};

async function loadTicket(id) {
  const view = await getMyTicket(id);
  // The approval normally comes embedded in the ticket view; fetch it only if the backend omitted the field.
  if (view && !("approval" in view) && APPROVAL_TICKET_STATUSES.has(view.status)) {
    try { view.approval = await getTicketApproval(id); } catch (e) { view.approvalError = e; }
  }
  return view;
}

export function NoteForm({ onAdd, label = "Add a note for the operations team", idPrefix = "note" }) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [fieldError, setFieldError] = useState("");
  const [done, setDone] = useState(false);
  async function submit(e) {
    e.preventDefault();
    if (pending) return;
    setDone(false);
    if (!text.trim()) { setFieldError("Write a note before adding it."); return; }
    setFieldError(""); setPending(true); setError(null);
    try { await onAdd(text.trim()); setText(""); setDone(true); } catch (err) { setError(err); } finally { setPending(false); }
  }
  return (
    <form className="stack-sm" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={`${idPrefix}-text`}>{label}</label>
        <textarea id={`${idPrefix}-text`} className="textarea" rows={3} maxLength={2000} value={text}
          aria-invalid={fieldError ? true : undefined} aria-describedby={fieldError ? `${idPrefix}-err` : undefined}
          onChange={(e) => setText(e.target.value)} />
        {fieldError && <span className="field-error" id={`${idPrefix}-err`}>{fieldError}</span>}
      </div>
      <InlineError error={error} />
      {done && <p className="small muted" role="status">Note added.</p>}
      <div><button type="submit" className="btn" disabled={pending}>{pending ? "Adding…" : "Add note"}</button></div>
    </form>
  );
}

export default function RequestDetailPage() {
  const { ticketId } = useParams();
  const { data: view, error, loading, reload, setData } = useApi(() => loadTicket(ticketId), [ticketId]);
  const [editing, setEditing] = useState(false);
  const [flash, setFlash] = useState("");

  if (loading && !view) return <LoadingState label="Loading request…" />;
  if (error) {
    return (
      <div className="stack">
        <PageHeader title="Request details" />
        <ErrorState error={error} onRetry={reload} title="We couldn't load this request" />
        <Link to="/requests">Back to my requests</Link>
      </div>
    );
  }
  if (!view) return null;

  const status = view.status;
  const editable = USER_EDITABLE_STATUSES.has(status);
  const questions = view.clarification_questions || [];
  const po = view.po_summary && typeof view.po_summary === "object" && Object.keys(view.po_summary).length ? view.po_summary : null;
  const showApproval = view.approval || view.approvalError || ["pending_approval", "changes_requested", "approval_expired"].includes(status);
  const showPolicy = view.policy_evidence || view.policy_status;

  async function resubmit(payload) {
    const updated = await updateMyTicket(view.ticket_id, payload);
    setData(updated);
    setEditing(false);
    setFlash(`Request ${view.ticket_id} was resubmitted.`);
  }

  return (
    <div className="stack">
      <Link to="/requests" className="small row"><ArrowLeft size={14} aria-hidden="true" />My requests</Link>
      <PageHeader title={view.submission?.title || "Request details"}
        description="Status and progress of your request."
        actions={editable && !editing ? <button type="button" className="btn btn-primary" onClick={() => { setEditing(true); setFlash(""); }}>Edit and resubmit</button> : null} />

      {flash && <Notice tone="success" role="status">{flash}</Notice>}

      <section className="card req-section" aria-labelledby="rd-status">
        <h2 id="rd-status" className="sr-only">Status</h2>
        <div className="req-summary">
          <div className="stack-sm"><span className="small muted">Request ID</span><span className="mono" data-testid="ticket-id">{view.ticket_id}</span></div>
          <div className="stack-sm"><span className="small muted">Status</span><StatusBadge status={status} /></div>
          <div className="stack-sm"><span className="small muted">Submitted</span><span>{formatDateTime(view.created_at)}</span></div>
          {view.updated_at && <div className="stack-sm"><span className="small muted">Last updated</span><span>{formatDateTime(view.updated_at)}</span></div>}
        </div>
        {view.outcome && <p data-testid="outcome">{view.outcome}</p>}
        <p className="small"><strong>Next:</strong> {userNextAction(status)}</p>
      </section>

      {questions.length > 0 && (
        <section className="card req-section" aria-labelledby="rd-questions">
          <h2 id="rd-questions">We need a bit more information</h2>
          <ul>{questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </section>
      )}

      {editing && (
        <section className="card req-section" aria-labelledby="rd-edit">
          <h2 id="rd-edit">Edit and resubmit</h2>
          {EDIT_HINT[status] && <p className="small muted">{EDIT_HINT[status]}</p>}
          <RequestForm idPrefix="edit" initial={formFromSubmission(view.submission)} submitLabel="Resubmit request"
            pendingLabel="Resubmitting…" onSubmit={resubmit} onCancel={() => setEditing(false)} />
        </section>
      )}

      {po && (
        <section className="card req-section" aria-labelledby="rd-po">
          <h2 id="rd-po">Draft purchase order</h2>
          <dl className="dl">
            <dt>Draft PO</dt><dd className="mono">{po.po_id || "—"}</dd>
            <dt>Vendor</dt><dd>{po.vendor_name || "—"}</dd>
            <dt>Total</dt><dd>{formatMoney(po.total, po.currency || "INR")}</dd>
            <dt>PO status</dt><dd>{humanize(po.status || "draft")}</dd>
            {po.approval_status && (<><dt>Internal approval</dt><dd>{humanize(po.approval_status)}</dd></>)}
          </dl>
          <Notice tone="info">{DRAFT_PO_NOTE}</Notice>
        </section>
      )}

      {showApproval && (
        <section className="card req-section" aria-labelledby="rd-approval">
          <h2 id="rd-approval">Approval</h2>
          {view.approvalError
            ? <Notice tone="warning">Approval details are unavailable: {view.approvalError.message}</Notice>
            : <ApprovalPanel approval={view.approval} ticketId={view.ticket_id} audience="requester" />}
        </section>
      )}

      {view.decision && (
        <Notice tone={view.decision.decision === "approve" ? "success" : "error"}
          title={view.decision.decision === "approve" ? "Approved" : "Closed by operations"}>
          {`By ${view.decision.by || "admin"} · ${formatDateTime(view.decision.at)}`}{view.decision.note ? ` — “${view.decision.note}”` : ""}
        </Notice>
      )}

      {showPolicy && (
        <section className="card req-section" aria-labelledby="rd-policy">
          <h2 id="rd-policy">Policy check</h2>
          <PolicyEvidence evaluation={view.policy_evidence} policyStatus={view.policy_status} redacted />
        </section>
      )}

      <section className="card req-section" aria-labelledby="rd-details">
        <h2 id="rd-details">Submitted details</h2>
        <SubmissionDetails submission={view.submission} />
      </section>

      <section className="card req-section" aria-labelledby="rd-notes">
        <h2 id="rd-notes">Notes</h2>
        <NotesList notes={view.notes} />
        <NoteForm idPrefix="my-note" onAdd={async (text) => setData(await addMyNote(view.ticket_id, text))} />
      </section>
    </div>
  );
}
