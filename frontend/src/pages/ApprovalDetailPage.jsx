import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { decideApproval, getApproval, getTicketEvidence } from "../api/approvals";
import { idempotencyKey } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";
import PageHeader from "../components/PageHeader";
import { ErrorState, LoadingState, Notice } from "../components/States";
import StatusBadge from "../components/StatusBadge";
import { useAuth } from "../lib/auth";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import { useApi } from "../lib/useApi";
import { APPROVAL_SAFETY, codeText, RISK_LABEL } from "./ApprovalsPage";
import "../styles/pages/approvals.css";

const DRAFT_COPY = "A draft PO is not an order. Nothing is purchased.";
const STALE_COPY = "The active policy set changed after this request was evaluated. The request must be re-run "
  + "before it can be decided.";
const UNCONFIRMED = "The outcome could not be confirmed. Reload the request before trying again.";

const ACTIONS = {
  approve: { label: "Approve", past: "Approved", tone: "primary", reason: false,
    description: "This records your internal authorization for this exact request version. The draft PO stays a draft." },
  request_changes: { label: "Request changes", past: "Changes requested", tone: "primary", reason: true,
    description: "The requester will be asked to update and resubmit the request." },
  reject: { label: "Reject", past: "Rejected", tone: "danger", reason: true,
    description: "The request will be rejected. The draft PO is not sent to anyone." },
};

const POLICY_OUTCOME = {
  pass: "Passed", review_required: "Needs review", hard_block: "Blocked", unknown: "Not enough evidence",
  blocked: "Blocked", needs_clarification: "Needs more information",
};

function Field({ label, children }) {
  return <><dt>{label}</dt><dd>{children ?? "—"}</dd></>;
}

function show(v) { return v === null || v === undefined || v === "" ? "—" : v; }

function Reasons({ view }) {
  const codes = view.reason_codes || [];
  const reasons = view.reasons || [];
  const n = Math.max(codes.length, reasons.length);
  if (!n) return <p className="muted small">No review reasons were recorded.</p>;
  return (
    <ul className="approval-reasons">
      {Array.from({ length: n }, (_, i) => <li key={i}>{reasons[i] || codeText(codes[i])}</li>)}
    </ul>
  );
}

function Decisions({ decisions }) {
  const list = (decisions || []).filter((d) => d && typeof d === "object");
  if (!list.length) return <p className="muted small">No decisions recorded yet.</p>;
  return (
    <ul className="approval-decisions">
      {list.map((d, i) => (
        <li key={d.decision_id || i}>
          <strong>{ACTIONS[d.action]?.past || humanize(d.action)}</strong> by {d.actor_id || "unknown"}
          {d.actor_role ? ` (${d.actor_role})` : ""} · {formatDateTime(d.decided_at)}
          {d.request_version != null ? ` · version ${d.request_version}` : ""}
          {d.comment && <div className="approval-quote">“{d.comment}”</div>}
        </li>
      ))}
    </ul>
  );
}

function DraftPo({ po }) {
  if (!po || typeof po !== "object") return <p className="muted small">No draft PO is attached to this request.</p>;
  const cur = po.currency || "INR";
  const items = (po.line_items || []).filter((li) => li && typeof li === "object");
  return (
    <div className="stack-sm">
      <dl className="dl">
        <Field label="Draft PO">{show(po.po_id)}</Field>
        <Field label="Vendor">{po.vendor_name || po.vendor_id ? `${po.vendor_name || "Unknown"}${po.vendor_id ? ` (${po.vendor_id})` : ""}` : "—"}</Field>
        <Field label="Subtotal">{formatMoney(po.subtotal, cur)}</Field>
        <Field label="Tax">{formatMoney(po.tax, cur)}</Field>
        <Field label="Total">{formatMoney(po.total, cur)}</Field>
        <Field label="Required approver">{show(po.required_approver)}</Field>
      </dl>
      {items.length > 0 && (
        <div className="table-wrap approval-lines">
          <table className="table">
            <caption className="sr-only">Draft PO line items</caption>
            <thead><tr>
              <th scope="col">Item</th><th scope="col" className="num">Qty</th>
              <th scope="col" className="num">Unit price</th><th scope="col" className="num">Line total</th>
            </tr></thead>
            <tbody>
              {items.map((li, i) => (
                <tr key={i}>
                  <td>{show(li.description)}</td>
                  <td className="num">{show(li.quantity)}</td>
                  <td className="num">{formatMoney(li.unit_price, cur)}</td>
                  <td className="num">{formatMoney(li.line_total, cur)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">{DRAFT_COPY}</p>
    </div>
  );
}

function citationLabel(e) {
  const parts = [e.filename || e.document_id || "Document"];
  if (e.version != null) parts.push(`v${e.version}${e.version_label ? ` (${e.version_label})` : ""}`);
  if (e.page_number != null) parts.push(`p. ${e.page_number}`);
  if (e.section) parts.push(`§ ${e.section}`);
  return parts.join(" · ");
}

/** Policy evidence for the ticket, loaded only when the disclosure is opened. */
function PolicyEvidence({ ticketId }) {
  const [state, setState] = useState({ loaded: false, loading: false, data: null, error: null });
  const load = useCallback(async () => {
    setState({ loaded: true, loading: true, data: null, error: null });
    try {
      setState({ loaded: true, loading: false, data: await getTicketEvidence(ticketId), error: null });
    } catch (e) {
      if (e?.status === 404) setState({ loaded: true, loading: false, data: null, error: null });
      else setState({ loaded: true, loading: false, data: null, error: e });
    }
  }, [ticketId]);

  const onToggle = (e) => { if (e.currentTarget.open && !state.loaded) load(); };
  const ev = state.data && typeof state.data === "object" ? (state.data.evaluation || state.data) : null;
  const engine = state.data?.rule_engine || ev?.rule_engine;
  const findings = (ev?.findings || []).filter((f) => f && typeof f === "object");
  const evidence = (ev?.evidence || []).filter((x) => x && typeof x === "object");
  const results = (engine?.results || []).filter((r) => r && typeof r === "object");

  return (
    <details className="disclosure" onToggle={onToggle}>
      <summary>Policy evidence and citations</summary>
      {state.loading && <LoadingState label="Loading policy evidence…" />}
      {state.error && <ErrorState error={state.error} onRetry={load} title="Policy evidence unavailable" />}
      {state.loaded && !state.loading && !state.error && !ev && (
        <p className="muted small">No policy evidence is stored for this request yet.</p>
      )}
      {ev && (
        <div className="stack-sm">
          {ev.outcome && <p><strong>Policy outcome:</strong> {POLICY_OUTCOME[ev.outcome] || humanize(ev.outcome)}</p>}
          {engine?.overall && <p><strong>Rule check:</strong> {POLICY_OUTCOME[engine.overall] || humanize(engine.overall)}
            {engine.policy_set_version != null ? ` (policy set v${engine.policy_set_version})` : ""}</p>}
          {results.length > 0 && (
            <ul className="approval-reasons">
              {results.map((r, i) => (
                <li key={`${r.rule_id}-${i}`}>{humanize(r.status)} — {r.explanation || r.reason_code || r.rule_id}</li>
              ))}
            </ul>
          )}
          {findings.length > 0 && (
            <>
              <strong className="small">Findings</strong>
              <ul className="approval-reasons">
                {findings.map((f, i) => (
                  <li key={f.finding_id || i}>{humanize(f.status)} · {humanize(f.severity)} — {f.summary || "No summary"}
                    {f.source === "llm" ? " (AI advisory — never decisive)" : ""}</li>
                ))}
              </ul>
            </>
          )}
          {evidence.length > 0 && (
            <>
              <strong className="small">Citations</strong>
              {evidence.map((c, i) => (
                <div key={c.evidence_id || i} className="card card-quiet stack-sm">
                  <span className="small"><strong>{citationLabel(c)}</strong></span>
                  {c.excerpt && <blockquote className="small" style={{ margin: 0 }}>{c.excerpt}</blockquote>}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </details>
  );
}

export default function ApprovalDetailPage({ readOnly = false }) {
  const { approvalId } = useParams();
  const { user } = useAuth();
  const viewOnly = readOnly || user?.role === "admin";
  const listPath = viewOnly ? "/admin/approvals" : "/approvals";
  const { data: view, error, loading, reload, setData } = useApi(() => getApproval(approvalId), [approvalId]);

  const [pending, setPending] = useState(null); // {action, idempotency_key, request_version, workflow_snapshot_hash}
  const [comment, setComment] = useState("");
  const [result, setResult] = useState(null);   // {tone, title, text}

  const open = useCallback((action) => {
    if (!view) return;
    setResult(null);
    setComment("");
    // One idempotency key per opened dialog; a retry after a network/server error reuses it.
    setPending({ action, idempotency_key: idempotencyKey("apr"), request_version: view.request_version,
      workflow_snapshot_hash: view.workflow_snapshot_hash });
  }, [view]);
  const cancel = useCallback(() => setPending(null), []);

  const confirm = useCallback(async (reason) => {
    const p = pending;
    const text = ACTIONS[p.action].reason ? reason : comment.trim();
    const body = { action: p.action, comment: text || null, idempotency_key: p.idempotency_key,
      request_version: p.request_version, workflow_snapshot_hash: p.workflow_snapshot_hash };
    let res;
    try {
      res = await decideApproval(approvalId, body);
    } catch (e) {
      if (e?.status === 409) {
        setPending(null);
        setResult({ tone: "warning", title: "This request changed or was already decided",
          text: `${e.message} The latest version has been loaded — review it before deciding.` });
        reload();
        return;
      }
      throw e; // dialog stays open with the same idempotency key
    }
    setPending(null);
    const confirmed = res?.approval && typeof res.approval === "object" ? res.approval : null;
    if (!confirmed || confirmed.status === "pending") {
      setResult({ tone: "error", title: "Not confirmed", text: UNCONFIRMED });
      reload();
      return;
    }
    setData(confirmed);
    setResult({ tone: res.reconciliation_required ? "warning" : "success",
      title: `Recorded: ${ACTIONS[p.action].past}`, text: res.message || "Decision recorded." });
  }, [pending, comment, approvalId, reload, setData]);

  if (loading && !view) return <LoadingState label="Loading approval request…" />;
  if (error) {
    return (
      <div className="stack">
        <PageHeader title="Approval request" />
        <ErrorState error={error} onRetry={reload} />
        <Link to={listPath}>Back to approvals</Link>
      </div>
    );
  }
  if (!view) return null;

  const snap = view.review_snapshot && typeof view.review_snapshot === "object" ? view.review_snapshot : {};
  const req = snap.request && typeof snap.request === "object" ? snap.request : {};
  const cur = view.currency || "INR";
  const boundSet = snap.policy_set_version ?? snap.policy_evaluation?.rule_engine?.policy_set_version;
  const isPending = view.status === "pending";
  const canAct = !viewOnly && Boolean(view.can_decide) && isPending;
  const meta = pending ? ACTIONS[pending.action] : null;

  return (
    <div className="stack">
      <PageHeader title={`Approval for ${view.ticket_id || "request"}`}
        description="Review what is being approved, then record your decision."
        actions={<>
          <Link to={listPath} className="btn btn-ghost">Back</Link>
          <button type="button" className="btn" onClick={reload} disabled={loading}>Reload</button>
        </>} />

      {result && <Notice tone={result.tone} title={result.title} role="status">{result.text}</Notice>}

      <section className="card stack-sm" aria-label="Decision status">
        <div className="row-between">
          <div className="row">
            <StatusBadge kind="approval" status={view.status} />
            <span className="muted small">Version {show(view.request_version)}</span>
          </div>
          <span className="stat-value">{formatMoney(view.amount, cur)}</span>
        </div>
        <div className="row small muted">
          <span>Risk: {RISK_LABEL[view.risk_level] || humanize(view.risk_level)}</span>
          <span>Requested {formatDateTime(view.created_at)}</span>
          <span>{isPending ? "Expires" : "Was due"} {formatDateTime(view.expires_at)}</span>
          {view.decided_at && <span>Decided {formatDateTime(view.decided_at)}</span>}
        </div>
      </section>

      <Notice tone="info">{view.safety_notice ? `${APPROVAL_SAFETY} ${view.safety_notice}` : APPROVAL_SAFETY}</Notice>
      {view.stale_policy && <Notice tone="warning" title="Policy changed">{STALE_COPY}</Notice>}
      {view.reconciliation_required && (
        <Notice tone="warning">The decision is recorded, but follow-up updates are incomplete. An admin must reconcile them.</Notice>
      )}

      <section className="card stack-sm" aria-labelledby="apr-decision">
        <h2 id="apr-decision">Decision</h2>
        {viewOnly ? (
          <Notice tone="info" title="View only">
            Admins can review approval requests but cannot decide them. Decisions require an eligible approver.
            {view.cannot_decide_reason ? ` ${view.cannot_decide_reason}` : ""}
          </Notice>
        ) : canAct ? (
          <>
            <p className="muted small">Your decision applies only to version {view.request_version} exactly as shown below.</p>
            <div className="approval-actions">
              <button type="button" className="btn btn-primary" onClick={() => open("approve")}>Approve</button>
              <button type="button" className="btn" onClick={() => open("request_changes")}>Request changes</button>
              <button type="button" className="btn btn-danger" onClick={() => open("reject")}>Reject</button>
            </div>
          </>
        ) : (
          <Notice tone="warning" title="You can't decide this request">
            {view.cannot_decide_reason || `This request is ${humanize(view.status).toLowerCase()} and cannot be decided.`}
          </Notice>
        )}
      </section>

      <div className="grid-2">
        <section className="card stack-sm" aria-labelledby="apr-request">
          <h2 id="apr-request">Request</h2>
          <dl className="dl">
            <Field label="Request">{show(view.ticket_id)}</Field>
            <Field label="Requester">{show(view.requester_id)}</Field>
            <Field label="Service">{show(req.service)}</Field>
            <Field label="Event date">{show(req.date)}</Field>
            <Field label="Attendees">{show(req.attendees)}</Field>
            <Field label="Location">{show(req.location)}</Field>
            <Field label="Budget limit">{req.budget_limit != null ? formatMoney(req.budget_limit, cur) : "—"}</Field>
          </dl>
        </section>
        <section className="card stack-sm" aria-labelledby="apr-reasons">
          <h2 id="apr-reasons">Why approval is needed</h2>
          <Reasons view={view} />
          <dl className="dl">
            <Field label="Approval policy">{show(view.policy_version)}</Field>
            <Field label="Policy set">{boundSet != null ? `Version ${boundSet}` : "Not bound to a rule set"}</Field>
            <Field label="Decided by role">{show(view.required_role)}</Field>
          </dl>
        </section>
      </div>

      <section className="card stack-sm" aria-labelledby="apr-po">
        <h2 id="apr-po">Draft PO</h2>
        <DraftPo po={snap.po} />
      </section>

      <section className="card stack-sm" aria-labelledby="apr-history">
        <h2 id="apr-history">Decision history</h2>
        <Decisions decisions={view.decisions} />
      </section>

      {view.ticket_id && <PolicyEvidence ticketId={view.ticket_id} />}
      <details className="disclosure">
        <summary>Technical details</summary>
        <dl className="dl">
          <Field label="Approval ID"><span className="mono">{view.approval_id}</span></Field>
          <Field label="Workflow run">{show(snap.run_count)}</Field>
          <Field label="Workflow status">{show(snap.final_status && humanize(snap.final_status))}</Field>
          <Field label="Snapshot hash"><span className="mono">{show(view.workflow_snapshot_hash)}</span></Field>
          {snap.policy_evaluation?.evaluation_hash && (
            <Field label="Policy evaluation"><span className="mono">{snap.policy_evaluation.evaluation_hash}</span></Field>
          )}
          {snap.policy_evaluation?.rule_engine?.engine_hash && (
            <Field label="Rule engine hash"><span className="mono">{snap.policy_evaluation.rule_engine.engine_hash}</span></Field>
          )}
        </dl>
      </details>

      <ConfirmDialog open={Boolean(pending)} title={meta ? `${meta.label}?` : ""}
        description={meta ? `${meta.description} ${APPROVAL_SAFETY}` : ""}
        confirmLabel={meta?.label} tone={meta?.tone} requireReason={meta?.reason ? 3 : 0}
        reasonLabel={pending?.action === "reject" ? "Reason for rejecting" : "What needs to change"}
        onConfirm={confirm} onCancel={cancel}>
        {meta && !meta.reason && (
          <div className="field">
            <label htmlFor="apr-approve-comment">Comment (optional)</label>
            <textarea id="apr-approve-comment" className="textarea" value={comment} maxLength={2000}
              onChange={(e) => setComment(e.target.value)} />
          </div>
        )}
        {view && <p className="small muted">Amount {formatMoney(view.amount, cur)} · version {show(view.request_version)}</p>}
      </ConfirmDialog>
    </div>
  );
}
