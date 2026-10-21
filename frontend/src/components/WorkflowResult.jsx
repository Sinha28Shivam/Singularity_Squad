/**
 * Readable workflow result (admin `workflow_result`), plus shared read-only panels used by the request pages:
 * PolicyEvidence, ApprovalPanel, NotesList, SubmissionDetails.
 *
 * Nothing here decides anything — outcomes are shown exactly as the backend computed them (spec §6.5).
 * Status first; evidence, citations, audit and raw data live behind disclosures.
 */
import { Fragment } from "react";
import StatusBadge from "./StatusBadge";
import { Notice } from "./States";
import { formatDate, formatDateTime, formatMoney, humanize } from "../lib/format";
import { categoryLabel, serviceLabel } from "../api/requests";
import "../styles/pages/requests.css";

export const DRAFT_PO_NOTE = "A draft PO is not an order. Nothing is purchased.";
export const APPROVAL_NOTE = "Approval is internal authorization only. It does not send a PO or move money.";

// ---------------------------------------------------------------- outcomes
const OVERALL = {
  pass: { label: "Passed", tone: "success", text: "All checks passed. The request is ready for human approval where required." },
  warning: { label: "Needs review", tone: "warning", text: "Some checks need a person to review the findings before anyone approves." },
  blocked: { label: "Blocked", tone: "error", text: "A check blocked this request. It can't go forward as it is." },
  needs_clarification: { label: "Needs more information", tone: "warning", text: "The requester must provide more details before the request can continue." },
};
const STEP_BADGE = {
  pass: ["pass", "Passed"],
  warn: ["warning", "Needs review"],
  fail: ["fail", "Problem found"],
  uncertain: ["uncertain", "Uncertain"],
  info: ["info", "Info"],
  not_run: ["not_run", "Not run"],
};
const SEVERITY = { pass: 0, info: 0, uncertain: 1, warn: 2, warning: 2, fail: 3 };
const PROBLEM = new Set(["fail", "warn", "warning", "uncertain", "mismatch", "duplicate", "blocked", "needs_clarification"]);

function worst(statuses) {
  const s = statuses.filter(Boolean);
  if (!s.length) return null;
  const w = s.reduce((a, b) => ((SEVERITY[b] ?? 1) > (SEVERITY[a] ?? 1) ? b : a));
  return w === "warning" ? "warn" : w;
}

function StepBadge({ status }) {
  const [kind, label] = STEP_BADGE[status] || STEP_BADGE.uncertain;
  return <StatusBadge status={kind} kind="outcome" label={label} />;
}

function EvidenceList({ evidence }) {
  const items = (evidence || []).filter((e) => e && typeof e === "object");
  if (!items.length) return <p className="muted small">No evidence attached.</p>;
  return (
    <ul className="wr-evidence">
      {items.map((e, i) => <li key={i}><span className="mono">{e.source || "source"}</span> — {e.detail || ""}</li>)}
    </ul>
  );
}

function ChecksList({ checks, title = "Checks" }) {
  const list = (checks || []).filter((c) => c && typeof c === "object");
  if (!list.length) return <p className="muted small">No checks recorded.</p>;
  return (
    <div className="stack-sm">
      <strong className="small">{title}</strong>
      <ul className="wr-checks">
        {list.map((c, i) => (
          <li key={i}>
            <div className="row">
              <StatusBadge status={c.status} kind="outcome" />
              <span>{c.name ? humanize(c.name) : "Check"}</span>
            </div>
            {c.detail && <p className="small">{c.detail}</p>}
            {(c.evidence || []).length > 0 && (
              <details className="disclosure wr-inner">
                <summary>Evidence ({c.evidence.length})</summary>
                <EvidenceList evidence={c.evidence} />
              </details>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function money(v, cur) { return formatMoney(v, cur || "INR"); }

// ---------------------------------------------------------------- steps
export function buildSteps(result) {
  const r = result || {};
  const steps = [];

  const req = r.request;
  const missing = r.missing_fields || [];
  steps.push({
    id: "intake",
    label: "Request details",
    status: !req ? "not_run" : missing.length ? "warn" : "pass",
    explanation: !req ? "The request details could not be read."
      : missing.length ? `Missing details: ${missing.map(humanize).join(", ")}.`
        : "All required details were found in the request.",
    body: req ? (
      <div className="stack-sm">
        <Notice tone="neutral" title="Extracted data">
          These values were read from the request text and are not independently verified. Compare them with the submitted details.
        </Notice>
        <dl className="dl">
          <dt>Event type</dt><dd>{humanize(req.category)}</dd>
          <dt>Service</dt><dd>{humanize(req.service)}</dd>
          <dt>Date</dt><dd>{req.date ? formatDate(req.date) : "—"}</dd>
          <dt>Attendees</dt><dd>{req.attendees ?? "—"}</dd>
          <dt>Budget limit</dt><dd>{req.budget_limit != null ? money(req.budget_limit, req.currency) : "—"}</dd>
          <dt>Location</dt><dd>{req.location || "—"}</dd>
          {req.urgency && (<><dt>Urgency</dt><dd>{humanize(req.urgency)}</dd></>)}
          {(req.requirements || []).length > 0 && (<><dt>Requirements</dt><dd>{req.requirements.join(", ")}</dd></>)}
        </dl>
      </div>
    ) : null,
  });

  const policy = r.policy;
  const clauses = policy?.clauses || [];
  steps.push({
    id: "policy",
    label: "Policy check",
    status: policy ? worst((policy.checks || []).map((c) => c.status)) || "pass" : "not_run",
    explanation: policy ? (policy.summary || "The request was checked against the event and catering policy.")
      : "The policy check didn't run (the request was incomplete or the run stopped earlier).",
    body: policy ? (
      <div className="stack-sm">
        <ChecksList checks={policy.checks} title="Rule-based checks" />
        <details className="disclosure wr-inner">
          <summary>Policy citations ({clauses.length})</summary>
          {clauses.length === 0 ? <p className="muted small">No clauses were retrieved.</p> : (
            <ul className="wr-citations">
              {clauses.map((cl, i) => (
                <li key={cl.clause_id || i}>
                  <strong>{cl.clause_id || "Clause"}{cl.title ? ` — ${cl.title}` : ""}</strong>
                  {cl.text && <p className="small">{cl.text}</p>}
                  <p className="muted small">Source: {cl.source_file || "unknown"}{cl.score != null ? ` · relevance ${Number(cl.score).toFixed(2)}` : ""}</p>
                </li>
              ))}
            </ul>
          )}
        </details>
      </div>
    ) : null,
  });

  const vendors = r.vendors;
  const sel = vendors?.selected;
  steps.push({
    id: "vendors",
    label: "Vendor match",
    status: !vendors ? "not_run" : !sel ? "fail" : vendors.replanned ? "warn" : "pass",
    explanation: !vendors ? "Vendor matching didn't run."
      : !sel ? "No eligible vendor was found. A person needs to review this."
        : vendors.replanned ? `Switched to an alternative vendor: ${vendors.explanation || sel.name || ""}`.trim()
          : (vendors.explanation || `Selected ${sel.name || sel.vendor_id}.`),
    body: vendors ? (
      <div className="stack-sm">
        {sel && (
          <dl className="dl">
            <dt>Selected vendor</dt><dd>{sel.name || "—"} {sel.vendor_id && <span className="mono">({sel.vendor_id})</span>}</dd>
            {sel.rate_per_person != null && (<><dt>Rate per person</dt><dd>{money(sel.rate_per_person)}</dd></>)}
            {sel.capacity != null && (<><dt>Capacity</dt><dd>{sel.capacity}</dd></>)}
          </dl>
        )}
        {(vendors.evaluations || []).length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <caption className="sr-only">Vendors considered</caption>
              <thead><tr><th scope="col">Vendor</th><th scope="col">Eligible</th><th scope="col" className="num">Estimated total</th><th scope="col">Reasons</th></tr></thead>
              <tbody>
                {vendors.evaluations.map((ev, i) => (
                  <tr key={ev.vendor_id || i}>
                    <td>{ev.name || ev.vendor_id}{ev.vendor_id === sel?.vendor_id ? " (selected)" : ""}</td>
                    <td>{ev.eligible ? "Yes" : "No"}</td>
                    <td className="num">{ev.estimated_total != null ? money(ev.estimated_total) : "—"}</td>
                    <td>{(ev.reasons || []).join("; ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    ) : null,
  });

  const proc = r.procurement;
  const po = proc?.po;
  let procStatus = "not_run";
  if (proc) {
    const w = worst((proc.checks || []).map((c) => c.status));
    procStatus = !po ? (w && w !== "pass" ? w : "fail") : w || "pass";
  }
  steps.push({
    id: "procurement",
    label: "Cost & draft PO",
    status: procStatus,
    explanation: !proc ? "The cost check didn't run."
      : !po ? "No draft PO was created (for example, the budget or policy check failed)."
        : `Draft PO ${po.po_id || ""} for ${money(po.total, po.currency)} with ${po.vendor_name || "the selected vendor"}.`,
    body: proc ? (
      <div className="stack-sm">
        {po && (
          <>
            <Notice tone="info">{DRAFT_PO_NOTE}</Notice>
            {(po.line_items || []).length > 0 && (
              <div className="table-wrap">
                <table className="table">
                  <caption className="sr-only">Draft PO line items</caption>
                  <thead><tr><th scope="col">Item</th><th scope="col" className="num">Qty</th><th scope="col" className="num">Unit price</th><th scope="col" className="num">Line total</th></tr></thead>
                  <tbody>
                    {po.line_items.map((li, i) => (
                      <tr key={i}>
                        <td>{li.description}</td>
                        <td className="num">{li.quantity}</td>
                        <td className="num">{money(li.unit_price, po.currency)}</td>
                        <td className="num">{money(li.line_total, po.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <dl className="dl">
              <dt>Subtotal</dt><dd>{money(po.subtotal, po.currency)}</dd>
              <dt>Tax</dt><dd>{money(po.tax, po.currency)}</dd>
              <dt>Total</dt><dd><strong>{money(po.total, po.currency)}</strong></dd>
              <dt>PO status</dt><dd>{humanize(po.status || "draft")}</dd>
              {po.required_approver && (<><dt>Required approver</dt><dd>{po.required_approver}</dd></>)}
            </dl>
          </>
        )}
        {(proc.calculation_steps || []).length > 0 && (
          <details className="disclosure wr-inner">
            <summary>How the total was calculated (rule-based)</summary>
            <ol className="small">{proc.calculation_steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
          </details>
        )}
        <ChecksList checks={proc.checks} title="Budget checks" />
      </div>
    ) : null,
  });

  const rec = r.reconciliation;
  const recMap = { match: "pass", mismatch: "fail", duplicate: "fail" };
  steps.push({
    id: "invoice",
    label: "Invoice check",
    status: rec ? recMap[rec.status] || "uncertain" : "not_run",
    explanation: rec ? `Invoice ${rec.invoice_id || ""}: ${humanize(rec.status)}.${rec.recommendation ? ` ${rec.recommendation}` : ""}`
      : "No invoice was checked in this run.",
    body: rec ? <ReconciliationDetails rec={rec} /> : null,
  });

  const critic = r.critic;
  const criticMap = { pass: "pass", warning: "warn", blocked: "fail", needs_clarification: "uncertain" };
  steps.push({
    id: "critic",
    label: "Final review",
    status: critic ? criticMap[critic.status] || "uncertain" : "not_run",
    explanation: critic ? (critic.summary || humanize(critic.status)) : "The final review didn't run.",
    body: critic ? <ChecksList checks={critic.findings} title="Review findings" /> : null,
  });
  return steps;
}

export function ReconciliationDetails({ rec }) {
  return (
    <div className="stack-sm">
      <dl className="dl">
        <dt>Invoice</dt><dd className="mono">{rec.invoice_id || "—"}</dd>
        <dt>Result</dt><dd><StatusBadge status={rec.status} kind="outcome" /></dd>
        <dt>Expected (draft PO)</dt><dd>{money(rec.expected_total ?? rec.po_total, rec.currency)}</dd>
        <dt>Invoiced</dt><dd>{money(rec.invoiced_total, rec.currency)}</dd>
        <dt>Difference</dt><dd>{money(rec.variance, rec.currency)}</dd>
        {rec.recommendation && (<><dt>Recommendation</dt><dd>{rec.recommendation}</dd></>)}
      </dl>
      <ChecksList checks={rec.checks} title="Invoice checks" />
    </div>
  );
}

export function unresolvedItems(result) {
  const r = result || {};
  const out = [];
  const add = (s) => { if (s && !out.includes(s)) out.push(s); };
  (r.missing_fields || []).forEach((f) => add(`Missing detail: ${humanize(f)}`));
  (r.clarification_questions || []).forEach(add);
  const checks = [
    ...((r.policy?.checks) || []), ...((r.procurement?.checks) || []),
    ...((r.reconciliation?.checks) || []), ...((r.critic?.findings) || []),
  ];
  checks.filter((c) => c && PROBLEM.has(c.status)).forEach((c) => add(`${c.name ? humanize(c.name) : "Check"}: ${c.detail || humanize(c.status)}`));
  if (r.vendors && !r.vendors.selected) add("No eligible vendor was found.");
  if (r.procurement && !r.procurement.po) add("No draft PO was created.");
  return out;
}

/** Workflow result: overall outcome, unresolved items, steps, then audit + raw data in disclosures. */
export default function WorkflowResult({ result, ticketId }) {
  if (!result || typeof result !== "object" || !Object.keys(result).length) {
    return <p className="muted">No workflow result yet. Run the workflow to check this request.</p>;
  }
  const overall = OVERALL[result.final_status] || { label: humanize(result.final_status || "unknown"), tone: "neutral", text: "" };
  const items = unresolvedItems(result);
  const steps = buildSteps(result);
  const po = result.procurement?.po;
  const audit = (result.audit_trail || []).filter((a) => a && typeof a === "object");
  return (
    <div className="stack wr">
      <div className={`wr-overall wr-${overall.tone}`}>
        <div className="row">
          <span className="small muted">Overall outcome</span>
          <span className={`badge badge-${overall.tone}`} data-testid="overall-outcome">{overall.label}</span>
        </div>
        {overall.text && <p>{overall.text}</p>}
        {result.critic?.summary && <p className="small">{result.critic.summary}</p>}
        {result.human_approval_required !== false && (
          <p className="small">
            Human approval required{po?.required_approver ? ` from: ${po.required_approver}` : ""}. An eligible approver decides on the Approvals page.
          </p>
        )}
        {result.persisted === false && (
          <Notice tone="warning" title="Result not fully saved">
            This result is not marked as saved — a database write may have failed. Re-run the workflow.{ticketId ? ` Reference: ${ticketId}` : ""}
          </Notice>
        )}
      </div>

      <section className="stack-sm" aria-labelledby="wr-unresolved">
        <h3 id="wr-unresolved">Unresolved items</h3>
        {items.length === 0 ? <p className="muted small">No unresolved items.</p> : (
          <ul className="wr-unresolved">{items.map((s, i) => <li key={i}>{s}</li>)}</ul>
        )}
      </section>

      <section className="stack-sm" aria-labelledby="wr-steps">
        <h3 id="wr-steps">Steps</h3>
        <ol className="steps">
          {steps.map((s, i) => (
            <li className="step" key={s.id} data-step={s.id}>
              <span className="wr-step-num" aria-hidden="true">{i + 1}</span>
              <div className="stack-sm">
                <div className="row">
                  <strong>{s.label}</strong>
                  <StepBadge status={s.status} />
                </div>
                <p className="small">{s.explanation}</p>
                {s.body && (
                  <details className="disclosure">
                    <summary>Details and evidence</summary>
                    {s.body}
                  </details>
                )}
              </div>
            </li>
          ))}
        </ol>
      </section>

      <details className="disclosure">
        <summary>Audit trail for this run ({audit.length})</summary>
        <AuditTable events={audit} />
      </details>
      <details className="disclosure">
        <summary>Raw result data</summary>
        <pre className="untrusted">{JSON.stringify(result, null, 2)}</pre>
      </details>
      {result.disclaimer && <p className="muted small">{result.disclaimer}</p>}
    </div>
  );
}

export function AuditTable({ events }) {
  const list = (events || []).filter((a) => a && typeof a === "object");
  if (!list.length) return <p className="muted small">No audit entries.</p>;
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="sr-only">Audit trail</caption>
        <thead><tr><th scope="col">#</th><th scope="col">Stage</th><th scope="col">Action</th><th scope="col">Result</th><th scope="col">Detail</th><th scope="col">Method</th><th scope="col">When</th></tr></thead>
        <tbody>
          {list.map((a, i) => (
            <tr key={`${a.sequence ?? a.step ?? i}-${i}`}>
              <td>{a.sequence ?? a.step ?? i + 1}</td>
              <td>{a.agent || "—"}</td>
              <td>{humanize(a.action)}</td>
              <td>{a.status ? <StatusBadge status={a.status} kind="outcome" /> : "—"}</td>
              <td className="small">{a.detail || "—"}</td>
              <td className="small">{a.llm_used ? "AI-assisted" : "Rule-based"}</td>
              <td className="small">{formatDateTime(a.timestamp)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- policy evidence (Policy RAG)
const POLICY_OUTCOME = {
  pass: ["Passed", "success", "No policy issue was found in the retrieved policy documents."],
  review_required: ["Needs review", "warning", "A person must review the policy findings."],
  hard_block: ["Blocked", "error", "A policy rule blocks this request."],
  unknown: ["Unknown", "neutral", "There wasn't enough policy evidence to decide."],
};
const FINDING_STATUS = { pass: "Pass", fail: "Fail", unknown: "Unknown", conflict: "Conflict", not_applicable: "Not applicable" };
const FINDING_TONE = { pass: "success", fail: "error", unknown: "neutral", conflict: "warning", not_applicable: "neutral" };
const SEVERITY_LABEL = { hard_block: "Hard block", review_required: "Review required", warning: "Warning", info: "Info" };

export function citationLabel(ev) {
  const parts = [ev.filename || ev.document_id || "document"];
  if (ev.version != null) parts.push(`v${ev.version}${ev.version_label ? ` (${ev.version_label})` : ""}`);
  else if (ev.version_label) parts.push(`version ${ev.version_label}`);
  if (ev.page_number != null) parts.push(`p. ${ev.page_number}`);
  if (ev.section) parts.push(`§ ${ev.section}`);
  return parts.join(" · ");
}

function normalizeEvaluation(data) {
  if (!data || typeof data !== "object" || !Object.keys(data).length) return null;
  if (data.evaluation && typeof data.evaluation === "object") return { ...data, ...data.evaluation };
  return data;
}

/** Policy evidence panel. `redacted` (requester) shows finding summaries and citation labels only. */
export function PolicyEvidence({ evaluation, policyStatus, redacted = false }) {
  const ev = normalizeEvaluation(evaluation);
  const isRedacted = redacted || Boolean(ev?.redacted);
  const st = policyStatus && typeof policyStatus === "object" ? policyStatus : null;
  if (!ev && !st) return <p className="muted small">No policy evaluation is stored for this request yet.</p>;
  const outcome = ev ? POLICY_OUTCOME[ev.outcome] : null;
  const findings = (ev?.findings || []).filter((f) => f && typeof f === "object");
  const evidence = (ev?.evidence || []).filter((e) => e && typeof e === "object");
  const byId = Object.fromEntries(evidence.filter((e) => e.evidence_id).map((e) => [e.evidence_id, e]));
  const labels = [];
  for (const c of ev?.citations || ev?.evidence || []) {
    if (c && typeof c === "object") { const l = citationLabel(c); if (!labels.includes(l)) labels.push(l); }
  }
  return (
    <div className="stack-sm">
      {ev && ev.outcome && (
        <div className="row">
          <span className="small muted">Policy outcome</span>
          <span className={`badge badge-${outcome ? outcome[1] : "neutral"}`}>{outcome ? outcome[0] : humanize(ev.outcome)}</span>
          {outcome && <span className="small">{outcome[2]}</span>}
        </div>
      )}
      {ev?.retrieval_mode === "keyword_only_degraded" && (
        <Notice tone="warning">Search ran in keyword-only mode, so policy evidence may be incomplete.</Notice>
      )}
      {st && (st.blocked || st.needs_clarification || (st.reasons || []).length > 0) && (
        <div className="stack-sm">
          {st.blocked && <Notice tone="error">A policy rule blocks this request.</Notice>}
          {st.needs_clarification && <Notice tone="warning">More information is needed before approval can be requested.</Notice>}
          {(st.reasons || []).length > 0 && (
            <>
              <strong className="small">Why</strong>
              <ul className="small">{st.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </>
          )}
          {!isRedacted && st.risk_level && <p className="small">Risk level: {humanize(st.risk_level)}</p>}
        </div>
      )}
      {findings.length > 0 && (
        <ul className="wr-checks">
          {findings.map((f, i) => {
            const cited = isRedacted ? (f.citations || []) : (f.evidence_ids || []).map((id) => byId[id]).filter(Boolean);
            return (
              <li key={i}>
                <div className="row">
                  <span className={`badge badge-${FINDING_TONE[f.status] || "neutral"}`}>{FINDING_STATUS[f.status] || humanize(f.status)}</span>
                  {f.severity && <span className="small muted">{SEVERITY_LABEL[f.severity] || humanize(f.severity)}</span>}
                  {!isRedacted && f.category && <span className="small muted">· {humanize(f.category)}</span>}
                </div>
                <p className="small">{f.summary || "(no summary)"}{f.source === "llm" ? " (AI advisory — never decisive)" : ""}</p>
                {!isRedacted && (f.reason || f.required_action) && (
                  <p className="small muted">{[f.reason && `Reason: ${f.reason}`, f.required_action && `Required action: ${f.required_action}`].filter(Boolean).join(" · ")}</p>
                )}
                {cited.length > 0 && <p className="small muted">Sources: {cited.filter((c) => c && typeof c === "object").map(citationLabel).join("; ")}</p>}
              </li>
            );
          })}
        </ul>
      )}
      {isRedacted ? (
        <>
          {labels.length > 0 && (
            <details className="disclosure">
              <summary>Sources consulted ({labels.length})</summary>
              <ul className="small">{labels.map((l) => <li key={l}>{l}</li>)}</ul>
            </details>
          )}
          <p className="muted small">Summary only — policy document text is visible to operations staff and approvers.</p>
        </>
      ) : (
        <>
          {(ev?.missing_evidence || []).length > 0 && (
            <div><strong className="small">Missing evidence</strong><ul className="small">{ev.missing_evidence.map((m, i) => <li key={i}>{String(m)}</li>)}</ul></div>
          )}
          {(ev?.conflicts || []).length > 0 && (
            <div><strong className="small">Conflicting evidence</strong><ul className="small">{ev.conflicts.map((m, i) => <li key={i}>{typeof m === "string" ? m : JSON.stringify(m)}</li>)}</ul></div>
          )}
          {ev && (
            <details className="disclosure">
              <summary>Policy citations ({evidence.length})</summary>
              {evidence.length === 0 ? <p className="muted small">No supporting citations were retrieved.</p> : (
                <ul className="wr-citations">
                  {evidence.map((e, i) => (
                    <li key={e.evidence_id || i}>
                      <strong className="small">{citationLabel(e)}</strong>
                      {e.excerpt && <blockquote className="small">{e.excerpt}</blockquote>}
                      {e.relevance_score != null && <p className="muted small">Relevance {Number(e.relevance_score).toFixed(3)}</p>}
                    </li>
                  ))}
                </ul>
              )}
            </details>
          )}
          {ev?.summary && (
            <details className="disclosure">
              <summary>AI explanation (advisory — does not change the outcome)</summary>
              <p className="small">{String(ev.summary)}</p>
            </details>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- approval panel
const ACTION_PAST = { approve: "Approved", reject: "Rejected", request_changes: "Changes requested" };

/** Normalise a TicketApprovalView (or an embedded single approval) into {current, history, requirement}. */
export function normalizeApproval(tav, ticketId) {
  if (!tav || typeof tav !== "object") return null;
  if (tav.approval_id && !("current" in tav) && !("history" in tav)) return { ticket_id: ticketId, current: tav, history: [tav] };
  const history = (tav.history || []).filter((h) => h && typeof h === "object");
  const current = tav.current && typeof tav.current === "object" ? tav.current : history[history.length - 1] || null;
  return { ...tav, current, history };
}

/**
 * Read-only approval panel. Admins and requesters can never approve here — it shows who must decide.
 * audience: "requester" | "admin".
 */
export function ApprovalPanel({ approval, ticketId, audience = "requester", actions }) {
  const tav = normalizeApproval(approval, ticketId);
  const current = tav?.current;
  const isAdmin = audience === "admin";
  return (
    <div className="stack-sm">
      {!current ? (
        <p className="muted small">No approval request for this request yet.</p>
      ) : (
        <>
          <div className="row">
            <span className="small muted">Approval status</span>
            <StatusBadge status={current.status} kind="approval" />
            {current.request_version != null && <span className="small muted">Version {current.request_version}</span>}
          </div>
          <dl className="dl">
            <dt>Amount</dt><dd>{money(current.amount, current.currency)}</dd>
            {current.risk_level && (<><dt>Risk level</dt><dd>{humanize(current.risk_level)}</dd></>)}
            <dt>Must be decided by</dt>
            <dd>
              {current.required_role ? `An eligible ${humanize(current.required_role).toLowerCase()}` : "An eligible approver"}
              {current.assigned_approver_id ? ` (assigned to ${current.assigned_approver_id})` : ""}
            </dd>
            <dt>Requested</dt><dd>{formatDateTime(current.created_at)}</dd>
            <dt>Expires</dt><dd>{formatDateTime(current.expires_at)}</dd>
            {current.decided_at && (<><dt>Decided</dt><dd>{formatDateTime(current.decided_at)}</dd></>)}
          </dl>
          {((current.reasons || []).length > 0 || (current.reason_codes || []).length > 0) && (
            <div>
              <strong className="small">Why approval is required</strong>
              <ul className="small">
                {(current.reasons?.length ? current.reasons : current.reason_codes).map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
          {isAdmin && current.reconciliation_required && (
            <Notice tone="warning" title="Follow-up updates incomplete">
              The decision is recorded but some follow-up updates didn't finish. Reconcile to re-apply them.
            </Notice>
          )}
          {isAdmin && current.stale_policy && (
            <Notice tone="warning">This request is bound to a policy set that is no longer active.</Notice>
          )}
          <details className="disclosure">
            <summary>Decision history</summary>
            {(tav.history.length ? tav.history : [current]).map((v, i) => (
              <div key={v.approval_id || i} className="stack-sm wr-version">
                <div className="row small">
                  <strong>Version {v.request_version ?? "?"}</strong>
                  <StatusBadge status={v.status} kind="approval" />
                  <span className="muted">requested {formatDateTime(v.created_at)}</span>
                </div>
                {(v.decisions || []).length === 0 ? <p className="muted small">No decisions recorded.</p> : (
                  <ul className="small">
                    {v.decisions.map((d, j) => (
                      <li key={j}>
                        {ACTION_PAST[d.action] || humanize(d.action)} by {d.actor_id || "someone"}
                        {d.actor_role ? ` (${d.actor_role})` : ""} · {formatDateTime(d.decided_at)}
                        {d.comment ? ` — “${d.comment}”` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </details>
        </>
      )}
      {isAdmin && (
        <p className="small">Admins can't approve requests. An eligible approver decides on the Approvals page.</p>
      )}
      <p className="muted small">{APPROVAL_NOTE}</p>
      {actions}
    </div>
  );
}

// ---------------------------------------------------------------- notes + submission
const ROLE_LABEL = { admin: "Operations", approver: "Approver", user: "Requester" };

export function NotesList({ notes }) {
  const list = (notes || []).filter((n) => n && typeof n === "object");
  if (!list.length) return <p className="muted small">No notes yet.</p>;
  return (
    <ul className="notes-list">
      {list.map((n, i) => (
        <li key={i}>
          <div className="small muted">
            <strong>{ROLE_LABEL[n.role] || "Requester"}</strong> · {n.author || "—"} · {formatDateTime(n.created_at)}
          </div>
          <p>{n.text}</p>
        </li>
      ))}
    </ul>
  );
}

export function SubmissionDetails({ submission }) {
  const s = submission;
  if (!s) return <p className="muted small">No structured details stored for this request.</p>;
  const rows = [
    ["Summary", s.title],
    ["Event type", categoryLabel(s.event_category)],
    ["Catering service", serviceLabel(s.service)],
    ["Event date", s.event_date ? formatDate(s.event_date) : null],
    ["Location", s.location],
    ["Attendees", s.attendees],
    ["Budget", s.budget_inr != null ? formatMoney(s.budget_inr) : null],
    ["Approved vendors only", s.approved_vendor_only === false ? "No" : "Yes"],
    ["Notes", s.notes],
  ];
  return (
    <dl className="dl">
      {rows.map(([k, v]) => (
        <Fragment key={k}><dt>{k}</dt><dd>{v === null || v === undefined || v === "" ? "—" : String(v)}</dd></Fragment>
      ))}
    </dl>
  );
}
