import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import PageHeader from "../components/PageHeader";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import ConfirmDialog from "../components/ConfirmDialog";
import { ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import WorkflowResult, {
  ApprovalPanel, AuditTable, DRAFT_PO_NOTE, NotesList, PolicyEvidence, ReconciliationDetails, SubmissionDetails,
} from "../components/WorkflowResult";
import { NoteForm } from "./RequestDetailPage";
import { useApi } from "../lib/useApi";
import { formatDateTime } from "../lib/format";
import { api, ApiError } from "../api/client";
import {
  addAdminNote, APPROVAL_TICKET_STATUSES, getAdminAudit, getAdminTicket, getTicketApproval, listInvoices, matchInvoice,
  NOT_RUNNABLE_STATUSES, previewInvoiceCheck, reconcileApproval, rejectAdminTicket, runAdminTicket, todayIso,
} from "../api/requests";
import WorkflowTimeline, { PendingActionBanner } from "../components/WorkflowTimeline";
import "../styles/pages/requests.css";

const NOT_RUNNABLE_REASON = {
  approved: "This request is approved. Re-running would replace the approved result, so it isn't allowed.",
  rejected: "This request is closed.",
  processing: "The checks are already running. Refresh in a moment.",
  changes_requested: "The approver asked for changes. Wait for the requester to resubmit.",
};

async function loadEvidence(id) {
  try {
    return await api.get(`/api/requests/${encodeURIComponent(id)}/evidence`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

async function loadAdminTicket(id) {
  const view = await getAdminTicket(id);
  if (!view) return view;
  // Prefer the data embedded in the ticket view; fetch only when the backend omitted the field.
  if (!("approval" in view) && APPROVAL_TICKET_STATUSES.has(view.status)) {
    try { view.approval = await getTicketApproval(id); } catch (e) { view.approvalError = e; }
  }
  const hasRun = Number(view.run_count || 0) > 0 || Boolean(view.workflow_result);
  if (!("policy_evaluation" in view) && hasRun) {
    try { view.policy_evaluation = await loadEvidence(id); } catch (e) { view.policyError = e; }
  }
  return view;
}

function RunPanel({ view, onDone }) {
  const status = view.status;
  const runCount = Number(view.run_count || 0);
  const invoices = useApi(() => listInvoices(), []);
  const [invoiceId, setInvoiceId] = useState("");
  const [today, setToday] = useState(todayIso());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [confirm, setConfirm] = useState(false);
  const runnable = !NOT_RUNNABLE_STATUSES.has(status);
  const label = runCount > 0 ? "Re-run workflow" : "Run workflow";
  const supersedes = status === "pending_approval";

  async function run() {
    setPending(true); setError(null);
    try {
      const updated = await runAdminTicket(view.ticket_id, { invoiceId: invoiceId || null, today: today || null });
      onDone(updated, `Workflow finished — status: ${statusLabel(updated?.status)}.`);
    } catch (e) {
      setError(e);
      throw e;
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="stack-sm">
      <p className="small muted">Runs the request through the policy, vendor, cost, invoice and final-review checks. Nothing is ordered or paid.</p>
      <div className="grid-2">
        <div className="field">
          <label htmlFor="run-today">Demo date (today)</label>
          <input id="run-today" type="date" className="input" value={today} onChange={(e) => setToday(e.target.value)} disabled={pending} />
          <span className="help">Used as "today" for date rules. Defaults to the current date.</span>
        </div>
        <div className="field">
          <label htmlFor="run-invoice">Invoice to check (optional)</label>
          <select id="run-invoice" className="select" value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)}
            disabled={pending || invoices.loading}>
            <option value="">None — skip the invoice check</option>
            {(invoices.data || []).map((i) => <option key={i.invoice_id} value={i.invoice_id}>{i.invoice_id}{i.vendor_id ? ` · ${i.vendor_id}` : ""}</option>)}
          </select>
          {invoices.error && <span className="field-error">Invoices couldn't be loaded. You can still run without one.</span>}
        </div>
      </div>
      {!runnable && <Notice tone="info">{NOT_RUNNABLE_REASON[status] || `Can't run while the status is ${statusLabel(status)}.`}</Notice>}
      {supersedes && runnable && (
        <p className="small">Re-running replaces the pending approval request with a new version.</p>
      )}
      <InlineError error={!confirm ? error : null} />
      <div>
        <button type="button" className="btn btn-primary" disabled={!runnable || pending}
          onClick={() => (supersedes ? setConfirm(true) : run().catch(() => {}))}>
          {pending ? "Running checks…" : label}
        </button>
      </div>
      <ConfirmDialog open={confirm} title="Re-run and replace the pending approval?"
        description="The current approval request will be superseded and a new version created after the run. Approvers will see the new version."
        confirmLabel="Re-run workflow" onCancel={() => setConfirm(false)}
        onConfirm={async () => { await run(); setConfirm(false); }} />
    </div>
  );
}

function InvoiceMatchPanel({ view, onDone }) {
  const invoices = useApi(() => listInvoices(), []);
  const [invoiceId, setInvoiceId] = useState("");
  const [open, setOpen] = useState(false);
  return (
    <div className="stack-sm">
      <p className="small muted">An invoice arrived after approval? Compare it with the approved draft PO. This doesn't change the approval and nothing is paid.</p>
      <div className="row">
        <div className="field">
          <label htmlFor="match-invoice">Invoice</label>
          <select id="match-invoice" className="select" value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} disabled={invoices.loading}>
            <option value="">Choose an invoice</option>
            {(invoices.data || []).map((i) => <option key={i.invoice_id} value={i.invoice_id}>{i.invoice_id}{i.vendor_id ? ` · ${i.vendor_id}` : ""}</option>)}
          </select>
        </div>
      </div>
      {invoices.error && <InlineError error={invoices.error} />}
      <div><button type="button" className="btn" disabled={!invoiceId} onClick={() => setOpen(true)}>Match invoice</button></div>
      <ConfirmDialog open={open} title={`Match invoice ${invoiceId}?`}
        description="The invoice is compared with the approved draft PO and the result is recorded. No payment is made."
        confirmLabel="Match invoice" onCancel={() => setOpen(false)}
        onConfirm={async () => {
          const updated = await matchInvoice(view.ticket_id, invoiceId);
          setOpen(false);
          onDone(updated, `Invoice ${invoiceId} matched against the approved draft PO.`);
        }} />
    </div>
  );
}

const PREVIEW_NOTE = "Preview only — not saved. This doesn't change the request, its audit trail or any approval, and nothing is paid.";

/** Ad-hoc invoice re-check against the current draft PO (POST /api/reconcile). The result is never stored. */
export function InvoicePreviewPanel({ po }) {
  const invoices = useApi(() => listInvoices(), []);
  const [invoiceId, setInvoiceId] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [rec, setRec] = useState(null);

  async function check() {
    if (!invoiceId || pending) return;
    setPending(true); setError(null); setRec(null);
    try {
      setRec(await previewInvoiceCheck(po, invoiceId));
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  return (
    <details className="disclosure">
      <summary>Check another invoice</summary>
      <div className="stack-sm">
        <Notice tone="info" title="Preview only — not saved">
          Compare any invoice with the current draft PO. This doesn't change the request, its audit trail or any approval, and nothing is paid.
        </Notice>
        <div className="row">
          <div className="field">
            <label htmlFor="preview-invoice">Invoice to re-check</label>
            <select id="preview-invoice" className="select" value={invoiceId} disabled={invoices.loading || pending}
              onChange={(e) => { setInvoiceId(e.target.value); setRec(null); setError(null); }}>
              <option value="">Choose an invoice</option>
              {(invoices.data || []).map((i) => <option key={i.invoice_id} value={i.invoice_id}>{i.invoice_id}{i.vendor_id ? ` · ${i.vendor_id}` : ""}</option>)}
            </select>
          </div>
        </div>
        {invoices.error && <InlineError error={invoices.error} />}
        <div><button type="button" className="btn" disabled={!invoiceId || pending} onClick={check}>
          {pending ? "Checking…" : "Check invoice"}
        </button></div>
        <InlineError error={error} />
        {rec && (
          <section className="card-quiet stack-sm" aria-label="Invoice preview result" data-testid="invoice-preview">
            <p className="small"><strong>{PREVIEW_NOTE}</strong></p>
            <ReconciliationDetails rec={rec} />
          </section>
        )}
      </div>
    </details>
  );
}

export default function AdminRequestDetailPage() {
  const { ticketId } = useParams();
  const { data: view, error, loading, reload, setData } = useApi(() => loadAdminTicket(ticketId), [ticketId]);
  const [flash, setFlash] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [audit, setAudit] = useState(null);
  const [auditState, setAuditState] = useState({ pending: false, error: null });
  const [reconcile, setReconcile] = useState({ pending: false, error: null });

  if (loading && !view) return <LoadingState label="Loading request…" />;
  if (error) {
    return (
      <div className="stack">
        <PageHeader title="Request details" />
        <ErrorState error={error} onRetry={reload} title="We couldn't load this request" />
        <Link to="/admin/requests">Back to all requests</Link>
      </div>
    );
  }
  if (!view) return null;

  const tid = view.ticket_id;
  const status = view.status;
  const result = view.workflow_result && typeof view.workflow_result === "object" && Object.keys(view.workflow_result).length ? view.workflow_result : null;
  const po = result?.procurement?.po;
  const showApproval = "approval" in view ? Boolean(view.approval) || APPROVAL_TICKET_STATUSES.has(status) : APPROVAL_TICKET_STATUSES.has(status);
  const approvalCurrent = view.approval?.current || null;
  const canMatchInvoice = status === "approved" && po && !result?.reconciliation && !view.invoice_match;

  async function refreshAfter(updated, message) {
    if (updated && typeof updated === "object") {
      const full = await loadAdminTicket(tid).catch(() => null);
      setData(full || updated);
    } else {
      await reload();
    }
    setFlash(message);
  }

  async function loadAudit() {
    setAuditState({ pending: true, error: null });
    try {
      const data = await getAdminAudit(tid);
      const events = [...((data && data.events) || [])].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
      setAudit(events);
      setAuditState({ pending: false, error: null });
    } catch (e) {
      setAuditState({ pending: false, error: e });
    }
  }

  async function doReconcile() {
    setReconcile({ pending: true, error: null });
    try {
      await reconcileApproval(approvalCurrent.approval_id);
      setReconcile({ pending: false, error: null });
      await refreshAfter(null, "Follow-up updates re-applied. The request was reloaded from the server.");
    } catch (e) {
      setReconcile({ pending: false, error: e });
    }
  }

  return (
    <div className="stack">
      <Link to="/admin/requests" className="small row"><ArrowLeft size={14} aria-hidden="true" />All requests</Link>
      <PageHeader title={view.submission?.title || `Request ${tid}`} description="Review the request, run the checks and follow the approval." />
      {flash && <Notice tone="success" role="status">{flash}</Notice>}
      <PendingActionBanner action={view.pending_action} />
      <WorkflowTimeline events={view.timeline} />

      <section className="card req-section" aria-labelledby="ad-summary">
        <h2 id="ad-summary" className="sr-only">Summary</h2>
        <div className="req-summary">
          <div className="stack-sm"><span className="small muted">Request ID</span><span className="mono" data-testid="ticket-id">{tid}</span></div>
          <div className="stack-sm"><span className="small muted">Status</span><StatusBadge status={status} /></div>
          <div className="stack-sm"><span className="small muted">Runs</span><span>{view.run_count || 0}{view.last_run_by ? ` · last by ${view.last_run_by}` : ""}</span></div>
          <div className="stack-sm"><span className="small muted">Submitted</span><span>{formatDateTime(view.created_at)}</span></div>
        </div>
        {view.error && <Notice tone="error" title="Last run problem">{view.error} (Reference: {tid})</Notice>}
      </section>

      <section className="card req-section" aria-labelledby="ad-run">
        <h2 id="ad-run">Workflow</h2>
        <RunPanel view={view} onDone={refreshAfter} />
      </section>

      <section className="card req-section" aria-labelledby="ad-result">
        <h2 id="ad-result">Workflow result</h2>
        <WorkflowResult result={result} ticketId={tid} invoiceExtra={po ? <InvoicePreviewPanel po={po} /> : null} />
      </section>

      {showApproval && (
        <section className="card req-section" aria-labelledby="ad-approval">
          <h2 id="ad-approval">Approval</h2>
          {view.approvalError ? <Notice tone="warning">Approval details are unavailable: {view.approvalError.message}</Notice> : (
            <ApprovalPanel approval={view.approval} ticketId={tid} audience="admin"
              actions={approvalCurrent?.reconciliation_required && approvalCurrent?.approval_id ? (
                <div className="stack-sm">
                  <InlineError error={reconcile.error} />
                  <div><button type="button" className="btn" onClick={doReconcile} disabled={reconcile.pending}>
                    {reconcile.pending ? "Reconciling…" : "Reconcile follow-up updates"}
                  </button></div>
                </div>
              ) : null} />
          )}
        </section>
      )}

      <section className="card req-section" aria-labelledby="ad-decision">
        <h2 id="ad-decision">Decision</h2>
        <p className="small">Admins can't approve requests. Draft POs that need sign-off go to an eligible approver on the Approvals page.</p>
        {status === "pending_approval" && <Notice tone="info">Waiting for an eligible approver.</Notice>}
        {(status === "approved" || status === "rejected") && (
          <p className="small">
            Final status: <StatusBadge status={status} />
            {view.decision ? ` by ${view.decision.by || "—"} · ${formatDateTime(view.decision.at)}${view.decision.note ? ` — “${view.decision.note}”` : ""}` : " (see the approval history)"}
          </p>
        )}
        {status === "reviewed" && (
          <div className="stack-sm">
            <p className="small muted">This request needs no approval request (for example, it was blocked or has no draft PO). You can close it.</p>
            <div><button type="button" className="btn btn-danger" onClick={() => setRejectOpen(true)}>Close request</button></div>
          </div>
        )}
        {po && <p className="muted small">{DRAFT_PO_NOTE}</p>}
        <ConfirmDialog open={rejectOpen} tone="danger" title={`Close request ${tid}?`}
          description="The request will be rejected and the requester will see your reason. This can't be undone."
          confirmLabel="Close request" requireReason={5} reasonLabel="Reason for the requester"
          onCancel={() => setRejectOpen(false)}
          onConfirm={async (reason) => {
            const updated = await rejectAdminTicket(tid, reason);
            setRejectOpen(false);
            await refreshAfter(updated, `Request ${tid} was closed.`);
          }} />
      </section>

      {(canMatchInvoice || view.invoice_match) && (
        <section className="card req-section" aria-labelledby="ad-invoice">
          <h2 id="ad-invoice">Invoice after approval</h2>
          {view.invoice_match ? (
            <div className="stack-sm">
              <p className="small muted">Matched by {view.invoice_match.matched_by || "—"} · {formatDateTime(view.invoice_match.matched_at)}. No payment was made.</p>
              <ReconciliationDetails rec={view.invoice_match} />
            </div>
          ) : <InvoiceMatchPanel view={view} onDone={refreshAfter} />}
        </section>
      )}

      {(view.policy_evaluation || view.policy_status || view.policyError) && (
        <section className="card req-section" aria-labelledby="ad-policy">
          <h2 id="ad-policy">Policy evidence</h2>
          {view.policyError ? <Notice tone="warning">Policy evidence is unavailable: {view.policyError.message}</Notice>
            : <PolicyEvidence evaluation={view.policy_evaluation} policyStatus={view.policy_status} />}
        </section>
      )}

      <section className="card req-section" aria-labelledby="ad-requester">
        <h2 id="ad-requester">Requester and submitted details</h2>
        <p className="small">{view.owner_name || "—"}{view.owner_username ? <> (<span className="mono">{view.owner_username}</span>)</> : null}{view.owner_email ? ` · ${view.owner_email}` : ""}</p>
        <SubmissionDetails submission={view.submission} />
        {view.raw_text && (
          <details className="disclosure">
            <summary>Request text used by the checks</summary>
            <pre className="untrusted">{view.raw_text}</pre>
          </details>
        )}
      </section>

      <section className="card req-section" aria-labelledby="ad-notes">
        <h2 id="ad-notes">Notes</h2>
        <NotesList notes={view.notes} />
        <NoteForm idPrefix="admin-note" label="Add a note (visible to the requester)"
          onAdd={async (text) => { const updated = await addAdminNote(tid, text); setData({ ...view, ...updated }); }} />
      </section>

      <details className="disclosure" onToggle={(e) => { if (e.currentTarget.open && audit === null && !auditState.pending) loadAudit(); }}>
        <summary>Saved audit history</summary>
        {auditState.pending ? <LoadingState label="Loading audit history…" />
          : auditState.error ? <ErrorState error={auditState.error} onRetry={loadAudit} title="We couldn't load the audit history" />
            : audit ? <AuditTable events={audit} /> : null}
      </details>
    </div>
  );
}
