import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import Tabs from "../components/Tabs";
import ConfirmDialog from "../components/ConfirmDialog";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDate, formatDateTime, formatMoney, humanize } from "../lib/format";
import {
  COMPLIANCE_STATUSES, MAX_PDF_PAGES, MIN_REASON, cancelRfq, closeRfq, compareQuotes, decideComparison,
  downloadQuotePdf, getRfq, listComparisons, pdfFileProblem, reviseQuote, submitQuote, uploadPdfQuote, verifyQuote,
} from "../api/vendorSourcing";
import "../styles/pages/sourcing.css";

const SOURCING_BANNER = "Decision support only — no supplier is contacted and nothing is awarded.";
const UNTRUSTED_WARNING = "Unverified — extracted text may be wrong. A reviewer must verify terms before comparison.";
const MONEY_FIELDS = [["unit_price", "Unit price"], ["tax_amount", "Tax amount"], ["delivery_fee", "Delivery fee"], ["other_fees", "Other fees"]];
const FIELD_LABELS = {
  currency: "Currency", unit_price: "Unit price", tax_amount: "Tax amount", delivery_fee: "Delivery fee",
  other_fees: "Other fees", delivery_date: "Delivery date", warranty_months: "Warranty (months)",
  warranty_scope: "Warranty scope", warranty_scope_accepted: "Warranty scope accepted",
};
const GATE_LABELS = { passed: "Verified", review_required: "Declared only", missing: "Missing", expired: "Expired" };
const MONEY_RE = /^\d{1,12}(\.\d{1,2})?$/;

const termsOf = (q) => (q?.submitted && typeof q.submitted === "object" && "unit_price" in q.submitted ? q.submitted : q || {});
const fieldLabel = (f) => (String(f).startsWith("compliance.") ? `Compliance: ${String(f).slice(11)}` : FIELD_LABELS[f] || humanize(f));

function showValue(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "object") {
    if ("status" in v) {
      return [humanize(v.status), v.evidence_ref ? `ref ${v.evidence_ref}` : "", v.expires_on ? `expires ${formatDate(v.expires_on)}` : ""]
        .filter(Boolean).join(" · ");
    }
    return JSON.stringify(v);
  }
  return String(v);
}

export default function RfqDetailPage() {
  const { rfqId } = useParams();
  const detail = useApi(() => getRfq(rfqId), [rfqId]);
  const comparisons = useApi(() => listComparisons(rfqId), [rfqId]);
  const [selectedQuote, setSelectedQuote] = useState(null);
  const [stateAction, setStateAction] = useState(null);
  const [flash, setFlash] = useState(null);
  const cancelStateAction = useCallback(() => setStateAction(null), []);

  if (detail.loading && !detail.data) return <LoadingState label="Loading RFQ…" />;
  if (detail.error) {
    return (
      <div className="stack">
        <PageHeader title="RFQ" actions={<Link className="btn" to="/admin/rfqs">Back to RFQs</Link>} />
        <ErrorState error={detail.error} onRetry={detail.reload} />
      </div>
    );
  }
  const rfq = detail.data?.rfq || {};
  const quotes = Array.isArray(detail.data?.quotes) ? detail.data.quotes : [];
  const events = Array.isArray(detail.data?.events) ? detail.data.events : [];
  const pendingEvents = Array.isArray(detail.data?.pending_events) ? detail.data.pending_events : [];
  const isOpen = rfq.status === "open";
  const accepting = isOpen && rfq.accepting_quotes !== false;
  const quote = quotes.find((q) => q.quote_id === selectedQuote) || null;
  const refresh = () => { detail.reload(); comparisons.reload(); };

  return (
    <div className="stack">
      <PageHeader title={`RFQ ${rfq.rfq_id || rfqId}`}
        description={`${humanize(rfq.service)} for request ${rfq.request_id || "—"}`}
        actions={<Link className="btn" to="/admin/rfqs">Back to RFQs</Link>}>
        <div className="row" style={{ marginTop: 8 }}>
          <StatusBadge kind="vendor" status={rfq.status} />
          <span className="muted small">Response deadline: {formatDateTime(rfq.response_deadline)}</span>
        </div>
      </PageHeader>
      <Notice tone="info" title={SOURCING_BANNER}>{detail.data?.notice ? String(detail.data.notice).slice(0, 300) : null}</Notice>
      {flash && <Notice tone="success">{flash}</Notice>}
      {rfq.audit_pending && <Notice tone="warning">Saved, but the audit record is still being delivered. It will be repaired automatically.</Notice>}

      <section className="card stack" aria-labelledby="rfq-summary-h">
        <div className="row-between">
          <h2 id="rfq-summary-h" className="sourcing-section-title">Summary</h2>
          {isOpen && (
            <div className="row">
              <button type="button" className="btn" onClick={() => setStateAction("close")}>Close RFQ</button>
              <button type="button" className="btn btn-danger" onClick={() => setStateAction("cancel")}>Cancel RFQ</button>
            </div>
          )}
        </div>
        <dl className="dl">
          <dt>Status</dt><dd><StatusBadge kind="vendor" status={rfq.status} />{rfq.status_reason ? <span className="muted small"> — {rfq.status_reason}</span> : null}</dd>
          <dt>Response deadline</dt><dd>{formatDateTime(rfq.response_deadline)}{isOpen && rfq.accepting_quotes === false ? " (passed)" : ""}</dd>
          <dt>Quantity</dt><dd>{rfq.quantity ?? "—"}</dd>
          <dt>Budget limit</dt><dd>{formatMoney(rfq.budget_limit, rfq.currency || "INR")}</dd>
          <dt>Delivery</dt><dd>{formatDate(rfq.delivery_date)}{rfq.delivery_location ? ` · ${rfq.delivery_location}` : ""}</dd>
          <dt>Warranty required</dt><dd>{rfq.warranty_months_required ?? 0} months{rfq.warranty_scope_required ? ` · ${rfq.warranty_scope_required}` : ""}</dd>
          <dt>Compliance required</dt><dd>{(rfq.compliance_requirements || []).join(", ") || "None"}</dd>
          <dt>Recipients</dt><dd className="mono">{(rfq.candidate_ids || []).join(", ") || "—"}</dd>
          <dt>Quotes received</dt><dd>{rfq.quotes_received ?? quotes.length}</dd>
          <dt>Created</dt><dd>{rfq.created_by || "—"} · {formatDateTime(rfq.created_at)}</dd>
        </dl>
      </section>

      <section className="card stack" aria-labelledby="quotes-h">
        <h2 id="quotes-h" className="sourcing-section-title">Quotes</h2>
        <DataTable caption="Quotes" rows={quotes} rowKey="quote_id" onRowClick={(q) => setSelectedQuote(q.quote_id)}
          empty={<EmptyState title="No quotes yet" description={accepting ? "Record a structured quote or upload a PDF below." : undefined} />}
          columns={[
            { key: "quote_id", header: "Quote", render: (q) => <span className="mono">{q.quote_id}</span> },
            { key: "vendor_id", header: "Vendor", render: (q) => <span className="mono">{q.vendor_id}</span> },
            { key: "quote_reference", header: "Reference" },
            { key: "submission_type", header: "Type", render: (q) => (q.submission_type === "pdf" ? "PDF" : "Structured") },
            { key: "revision", header: "Revision", align: "right", render: (q) => q.revision ?? 1 },
            { key: "status", header: "Status", render: (q) => <StatusBadge kind="vendor" status={q.status} /> },
            { key: "submitted_at", header: "Submitted", render: (q) => formatDateTime(q.submitted_at) },
          ]} />
        {quote && (
          <QuotePanel key={`${quote.quote_id}-${quote.status}`} rfq={rfq} quote={quote} canRevise={accepting}
            onClose={() => setSelectedQuote(null)}
            onChanged={(msg, q) => { setFlash(msg); if (q?.quote_id) setSelectedQuote(q.quote_id); refresh(); }} />
        )}
      </section>

      {accepting ? (
        <AddQuoteSection rfq={rfq} onAdded={(msg, q) => { setFlash(msg); if (q?.quote_id) setSelectedQuote(q.quote_id); refresh(); }} />
      ) : isOpen ? (
        <Notice tone="warning">The response deadline has passed; new quotes are not accepted.</Notice>
      ) : null}

      <ComparisonSection rfq={rfq} comparisons={comparisons} onChanged={(msg) => { setFlash(msg); refresh(); }} />

      <details className="disclosure">
        <summary>Activity ({events.length}{pendingEvents.length ? ` + ${pendingEvents.length} pending` : ""})</summary>
        {events.length + pendingEvents.length === 0 ? <p className="muted small">No events recorded.</p> : (
          <ul className="event-list">
            {[...events, ...pendingEvents].map((e, i) => (
              <li key={e.event_id || i}>
                <strong>{humanize(e.event_type || e.action)}</strong>
                <span className="muted"> · {e.actor || "—"} · {formatDateTime(e.created_at || e.at)}</span>
                {(e.from_status || e.to_status) && <span className="muted small"> · {humanize(e.from_status)} → {humanize(e.to_status)}</span>}
                {e.reason && <div className="muted small">{e.reason}</div>}
              </li>
            ))}
          </ul>
        )}
      </details>

      <ConfirmDialog open={Boolean(stateAction)}
        title={stateAction === "close" ? "Close this RFQ?" : "Cancel this RFQ?"}
        description={stateAction === "close"
          ? "No further quotes will be accepted. Comparison is still allowed."
          : "No quotes, verification, comparison or decision will be possible afterwards."}
        confirmLabel={stateAction === "close" ? "Close RFQ" : "Cancel RFQ"} tone={stateAction === "close" ? "primary" : "danger"}
        requireReason={MIN_REASON} onCancel={cancelStateAction}
        onConfirm={async (reason) => {
          await (stateAction === "close" ? closeRfq : cancelRfq)(rfq.rfq_id || rfqId, reason);
          const what = stateAction === "close" ? "closed" : "cancelled";
          setStateAction(null);
          setFlash(`RFQ ${what}.`);
          refresh();
        }} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ add quote
function AddQuoteSection({ rfq, onAdded }) {
  const [mode, setMode] = useState("structured");
  return (
    <section className="card stack" aria-labelledby="add-quote-h">
      <h2 id="add-quote-h" className="sourcing-section-title">Add a quote</h2>
      <Tabs label="Quote entry method" value={mode} onChange={setMode}
        tabs={[{ id: "structured", label: "Structured entry" }, { id: "pdf", label: "PDF upload" }]} />
      {mode === "structured"
        ? <QuoteForm rfq={rfq} onSaved={(q) => onAdded(`Quote ${q?.quote_id || ""} recorded as unverified.`, q)} />
        : <PdfUpload rfq={rfq} onUploaded={(q) => onAdded(`PDF quote ${q?.quote_id || ""} stored. Its terms stay unverified until a reviewer verifies them.`, q)} />}
    </section>
  );
}

function initialTerms(rfq, base) {
  const t = base || {};
  const comp = t.compliance && typeof t.compliance === "object" ? t.compliance : {};
  const reqs = [...new Set([...(rfq.compliance_requirements || []), ...Object.keys(comp)])];
  const declared = {};
  for (const r of reqs) {
    const c = comp[r];
    declared[r] = c === true || (c && typeof c === "object" && (c.status === "declared" || c.status === "verified"));
  }
  return {
    vendor_id: (rfq.candidate_ids || [])[0] || "",
    quote_reference: base?.quote_reference || "",
    currency: t.currency || rfq.currency || "INR",
    unit_price: t.unit_price ?? "", tax_amount: t.tax_amount ?? "0", delivery_fee: t.delivery_fee ?? "0", other_fees: t.other_fees ?? "0",
    delivery_date: String(t.delivery_date || rfq.delivery_date || "").slice(0, 10),
    warranty_months: String(t.warranty_months ?? 0), warranty_scope: t.warranty_scope || "",
    declared, notes: "", revision_reason: "",
  };
}

function validateTerms(f) {
  const errs = {};
  if (!/^[A-Z]{3}$/.test(f.currency.trim().toUpperCase())) errs.currency = "Use a 3-letter currency code.";
  if (!MONEY_RE.test(String(f.unit_price).trim()) || Number(f.unit_price) <= 0) errs.unit_price = "Enter a positive amount (up to 2 decimals).";
  for (const k of ["tax_amount", "delivery_fee", "other_fees"]) {
    if (String(f[k]).trim() && !MONEY_RE.test(String(f[k]).trim())) errs[k] = "Enter an amount (up to 2 decimals).";
  }
  if (!f.delivery_date) errs.delivery_date = "Choose a delivery date.";
  const w = Number(f.warranty_months);
  if (!Number.isInteger(w) || w < 0 || w > 120) errs.warranty_months = "Enter whole months between 0 and 120.";
  if (!f.warranty_scope.trim()) errs.warranty_scope = "Describe the warranty scope.";
  return errs;
}

function termsBody(f) {
  return {
    currency: f.currency.trim().toUpperCase(),
    unit_price: String(f.unit_price).trim(),
    tax_amount: String(f.tax_amount).trim() || "0",
    delivery_fee: String(f.delivery_fee).trim() || "0",
    other_fees: String(f.other_fees).trim() || "0",
    delivery_date: f.delivery_date,
    warranty_months: Number(f.warranty_months),
    warranty_scope: f.warranty_scope.trim(),
  };
}

/** Structured quote entry; with `revisionOf` it records a new revision of that quote. */
function QuoteForm({ rfq, revisionOf, onSaved, onCancel }) {
  const [f, setF] = useState(() => initialTerms(rfq, revisionOf ? { ...termsOf(revisionOf), quote_reference: revisionOf.quote_reference } : null));
  const [errors, setErrors] = useState({});
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const idp = revisionOf ? `rev-${revisionOf.quote_id}` : "q";

  async function submit(e) {
    e.preventDefault();
    if (pending) return;
    const errs = validateTerms(f);
    if (!f.quote_reference.trim()) errs.quote_reference = "Enter the vendor's quote reference.";
    if (!revisionOf && !f.vendor_id) errs.vendor_id = "Choose the vendor.";
    if (revisionOf && f.revision_reason.trim().length < MIN_REASON) errs.revision_reason = `Give a reason of at least ${MIN_REASON} characters.`;
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const body = { ...termsBody(f), quote_reference: f.quote_reference.trim(), notes: f.notes.trim(),
      compliance: Object.fromEntries(Object.entries(f.declared).map(([k, v]) => [k, { status: v ? "declared" : "missing" }])) };
    if (revisionOf) body.revision_reason = f.revision_reason.trim(); else body.vendor_id = f.vendor_id;
    setPending(true); setServerError(null);
    try {
      const saved = revisionOf ? await reviseQuote(rfq.rfq_id, revisionOf.quote_id, body) : await submitQuote(rfq.rfq_id, body);
      onSaved(saved);
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  }

  const err = (k) => errors[k] && <span className="field-error">{errors[k]}</span>;
  return (
    <form className="stack" onSubmit={submit} noValidate aria-label={revisionOf ? "Revise quote" : "Structured quote"}>
      <div className="grid-2">
        {!revisionOf && (
          <div className="field">
            <label htmlFor={`${idp}-vendor`} className="required">Vendor</label>
            <select id={`${idp}-vendor`} className="select" value={f.vendor_id} onChange={set("vendor_id")}>
              {(rfq.candidate_ids || []).map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
            {err("vendor_id")}
          </div>
        )}
        <div className="field">
          <label htmlFor={`${idp}-ref`} className="required">Quote reference</label>
          <input id={`${idp}-ref`} className="input" value={f.quote_reference} onChange={set("quote_reference")} maxLength={120} />
          {err("quote_reference")}
        </div>
        <TermsFields f={f} set={set} err={err} idp={idp} />
      </div>
      {Object.keys(f.declared).length > 0 && (
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label">Compliance the vendor declares</legend>
          <p className="muted small">Declarations are not proof. A reviewer records evidence during verification.</p>
          {Object.keys(f.declared).map((r, i) => (
            <label key={r} htmlFor={`${idp}-decl-${i}`} className="row" style={{ fontWeight: 400 }}>
              <input id={`${idp}-decl-${i}`} type="checkbox" className="checkbox" checked={f.declared[r]}
                onChange={(e) => setF({ ...f, declared: { ...f.declared, [r]: e.target.checked } })} />
              Vendor declares {r}
            </label>
          ))}
        </fieldset>
      )}
      <div className="field">
        <label htmlFor={`${idp}-notes`}>Notes</label>
        <textarea id={`${idp}-notes`} className="textarea" value={f.notes} onChange={set("notes")} maxLength={2000} />
      </div>
      {revisionOf && (
        <div className="field">
          <label htmlFor={`${idp}-reason`} className="required">Revision reason</label>
          <textarea id={`${idp}-reason`} className="textarea" value={f.revision_reason} onChange={set("revision_reason")} maxLength={1000} />
          {err("revision_reason")}
        </div>
      )}
      <InlineError error={serverError} />
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={pending}>
          {pending ? "Saving…" : revisionOf ? "Record revision" : "Record quote"}
        </button>
        {onCancel && <button type="button" className="btn" onClick={onCancel} disabled={pending}>Cancel</button>}
      </div>
    </form>
  );
}

function TermsFields({ f, set, err, idp }) {
  return (
    <>
      <div className="field">
        <label htmlFor={`${idp}-currency`} className="required">Currency</label>
        <input id={`${idp}-currency`} className="input" value={f.currency} onChange={set("currency")} maxLength={3} />
        {err("currency")}
      </div>
      {MONEY_FIELDS.map(([k, label]) => (
        <div className="field" key={k}>
          <label htmlFor={`${idp}-${k}`} className={k === "unit_price" ? "required" : undefined}>{label}</label>
          <input id={`${idp}-${k}`} className="input" inputMode="decimal" value={f[k]} onChange={set(k)} />
          {err(k)}
        </div>
      ))}
      <div className="field">
        <label htmlFor={`${idp}-ddate`} className="required">Delivery date</label>
        <input id={`${idp}-ddate`} type="date" className="input" value={f.delivery_date} onChange={set("delivery_date")} />
        {err("delivery_date")}
      </div>
      <div className="field">
        <label htmlFor={`${idp}-wm`}>Warranty (months)</label>
        <input id={`${idp}-wm`} type="number" min={0} max={120} step={1} className="input" value={f.warranty_months} onChange={set("warranty_months")} />
        {err("warranty_months")}
      </div>
      <div className="field">
        <label htmlFor={`${idp}-ws`} className="required">Warranty scope</label>
        <input id={`${idp}-ws`} className="input" value={f.warranty_scope} onChange={set("warranty_scope")} maxLength={1000} />
        {err("warranty_scope")}
      </div>
    </>
  );
}

function PdfUpload({ rfq, onUploaded }) {
  const [file, setFile] = useState(null);
  const [vendor, setVendor] = useState((rfq.candidate_ids || [])[0] || "");
  const [ref, setRef] = useState("");
  const [errors, setErrors] = useState({});
  const [uploading, setUploading] = useState(false);
  const [serverError, setServerError] = useState(null);

  function pick(e) {
    const f = e.target.files?.[0] || null;
    setFile(f);
    const problem = f ? pdfFileProblem(f) : "";
    setErrors((x) => ({ ...x, file: problem }));
  }

  async function submit(e) {
    e.preventDefault();
    if (uploading) return;
    const errs = {};
    const problem = pdfFileProblem(file);
    if (problem) errs.file = problem;
    if (!vendor) errs.vendor = "Choose the vendor.";
    if (!ref.trim()) errs.ref = "Enter the vendor's quote reference.";
    setErrors(errs);
    if (Object.values(errs).some(Boolean)) return;
    setUploading(true); setServerError(null);
    try {
      const bytes = typeof file.arrayBuffer === "function" ? await file.arrayBuffer() : file;
      const saved = await uploadPdfQuote(rfq.rfq_id, bytes,
        { vendor_id: vendor, quote_reference: ref.trim(), filename: file.name || "quotation.pdf" });
      onUploaded(saved);
    } catch (err) {
      setServerError(err);
    } finally {
      setUploading(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} noValidate aria-label="PDF quote upload">
      <p className="muted small">PDF only · up to 5 MiB · 1–{MAX_PDF_PAGES} pages · not password-protected. Extracted text stays unverified until a reviewer verifies the terms.</p>
      <div className="grid-2">
        <div className="field">
          <label htmlFor="pdf-file" className="required">Quotation PDF</label>
          <input id="pdf-file" type="file" accept="application/pdf" className="input" onChange={pick} aria-invalid={Boolean(errors.file)} />
          {errors.file && <span className="field-error">{errors.file}</span>}
          {file && !errors.file && <span className="help">{file.name} · {(file.size / 1024).toFixed(0)} KB</span>}
        </div>
        <div className="field">
          <label htmlFor="pdf-vendor" className="required">Vendor</label>
          <select id="pdf-vendor" className="select" value={vendor} onChange={(e) => setVendor(e.target.value)}>
            {(rfq.candidate_ids || []).map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          {errors.vendor && <span className="field-error">{errors.vendor}</span>}
        </div>
        <div className="field">
          <label htmlFor="pdf-ref" className="required">Quote reference</label>
          <input id="pdf-ref" className="input" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={120} />
          {errors.ref && <span className="field-error">{errors.ref}</span>}
        </div>
      </div>
      {uploading && <div role="status" aria-live="polite" className="row"><div className="spinner" aria-hidden="true" /><span>Uploading PDF…</span></div>}
      <InlineError error={serverError} />
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={uploading}>{uploading ? "Uploading…" : "Upload PDF quote"}</button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------------------------------ quote panel
function QuotePanel({ rfq, quote, canRevise, onClose, onChanged }) {
  const [revising, setRevising] = useState(false);
  const sub = termsOf(quote);
  const isPdf = quote.submission_type === "pdf" || quote.extracted_text != null;
  const verification = quote.verification && typeof quote.verification === "object" ? quote.verification : null;
  const verified = quote.verified && typeof quote.verified === "object" ? quote.verified : null;
  const diff = Array.isArray(verification?.diff) ? verification.diff : [];

  return (
    <div className="card-quiet stack" aria-labelledby="quote-panel-h">
      <div className="row-between">
        <div className="row">
          <h3 id="quote-panel-h" className="sourcing-section-title">Quote {quote.quote_id}</h3>
          <StatusBadge kind="vendor" status={quote.status} />
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Close quote</button>
      </div>
      <dl className="dl">
        <dt>Vendor</dt><dd className="mono">{quote.vendor_id}</dd>
        <dt>Reference</dt><dd>{quote.quote_reference}</dd>
        <dt>Revision</dt><dd>{quote.revision ?? 1}{quote.revision_of ? ` (revises ${quote.revision_of})` : ""}{quote.superseded_by ? ` · superseded by ${quote.superseded_by}` : ""}</dd>
        {"unit_price" in sub && <>
          <dt>Submitted price</dt><dd>{showValue(sub.unit_price)} per unit · tax {showValue(sub.tax_amount)} · delivery {showValue(sub.delivery_fee)} · other {showValue(sub.other_fees)} ({sub.currency || "—"})</dd>
          <dt>Submitted delivery</dt><dd>{formatDate(sub.delivery_date)}</dd>
          <dt>Submitted warranty</dt><dd>{sub.warranty_months ?? "—"} months · {sub.warranty_scope || "—"}</dd>
        </>}
        {isPdf && <><dt>PDF</dt><dd>{quote.pdf_filename || "quotation.pdf"} · {quote.pdf_page_count ?? "?"} pages{quote.pdf_size ? ` · ${(quote.pdf_size / 1024).toFixed(0)} KB` : ""}</dd></>}
      </dl>
      {isPdf && (
        <div className="stack-sm">
          <Notice tone="warning">{UNTRUSTED_WARNING}</Notice>
          <div className="untrusted" aria-label="Extracted PDF text (untrusted)">
            <pre>{quote.extracted_text ? String(quote.extracted_text) : "No text could be extracted."}</pre>
          </div>
          <PdfDownload rfqId={rfq.rfq_id} quote={quote} />
        </div>
      )}
      {quote.status === "pending_verification" && !quote.superseded_by && rfq.status !== "cancelled" && (
        <VerifyForm rfq={rfq} quote={quote} onVerified={(q) => onChanged(`Quote ${quote.quote_id} verified by a reviewer.`, q)} />
      )}
      {quote.status === "verified" && (
        <div className="stack-sm">
          <h4 className="label">Submitted vs verified</h4>
          <p className="muted small">
            Verified by {verification?.reviewer || quote.verified_by || "—"} · {formatDateTime(verification?.reviewed_at || quote.verified_at)}
            {(verification?.note || quote.reviewer_note) ? ` — ${verification?.note || quote.reviewer_note}` : ""}
          </p>
          {diff.length ? (
            <DataTable caption="Submitted vs verified changes" rows={diff} rowKey="field" columns={[
              { key: "field", header: "Field", render: (d) => fieldLabel(d.field) },
              { key: "submitted", header: "Submitted", render: (d) => showValue(d.submitted) },
              { key: "verified", header: "Verified", render: (d) => showValue(d.verified) },
            ]} />
          ) : <p className="muted small">No differences between the submitted and verified terms.</p>}
          {verified && (
            <details className="disclosure">
              <summary>Verified terms</summary>
              <dl className="dl">
                {Object.keys(FIELD_LABELS).filter((k) => k in verified).map((k) => (
                  <Fragment key={k}><dt>{FIELD_LABELS[k]}</dt><dd>{showValue(verified[k])}</dd></Fragment>
                ))}
                {Object.entries(verified.compliance || {}).map(([k, v]) => (
                  <Fragment key={`c-${k}`}><dt>Compliance: {k}</dt><dd>{showValue(v)}</dd></Fragment>
                ))}
              </dl>
            </details>
          )}
        </div>
      )}
      {canRevise && quote.status !== "superseded" && !quote.superseded_by && (
        revising
          ? <div className="stack-sm"><h4 className="label">Revise this quote</h4>
            <QuoteForm rfq={rfq} revisionOf={quote} onCancel={() => setRevising(false)}
              onSaved={(q) => { setRevising(false); onChanged(`Revision ${q?.quote_id || ""} recorded; the previous quote is superseded.`, q); }} /></div>
          : <div><button type="button" className="btn" onClick={() => setRevising(true)}>Revise quote</button></div>
      )}
    </div>
  );
}

function PdfDownload({ rfqId, quote }) {
  const [state, setState] = useState({ loading: false, url: "", error: null });
  const urlRef = useRef("");
  useEffect(() => () => { if (urlRef.current) URL.revokeObjectURL?.(urlRef.current); }, []);
  async function prepare() {
    setState({ loading: true, url: "", error: null });
    try {
      const blob = await downloadQuotePdf(rfqId, quote.quote_id);
      const data = blob instanceof Blob ? blob : new Blob([blob], { type: "application/pdf" });
      const url = URL.createObjectURL(data);
      urlRef.current = url;
      setState({ loading: false, url, error: null });
    } catch (error) {
      setState({ loading: false, url: "", error });
    }
  }
  return (
    <div className="row">
      {state.url
        ? <a className="btn" href={state.url} download={quote.pdf_filename || `${quote.quote_id}.pdf`}>Download original PDF</a>
        : <button type="button" className="btn" onClick={prepare} disabled={state.loading}>{state.loading ? "Preparing…" : "Get original PDF"}</button>}
      <InlineError error={state.error} />
    </div>
  );
}

function VerifyForm({ rfq, quote, onVerified }) {
  const sub = termsOf(quote);
  const subComp = sub.compliance && typeof sub.compliance === "object" ? sub.compliance : {};
  const reqs = [...new Set([...(rfq.compliance_requirements || []), ...Object.keys(subComp)])];
  const [f, setF] = useState(() => ({
    ...initialTerms(rfq, "unit_price" in sub ? sub : null),
    scopeAccepted: false, note: "",
    comp: Object.fromEntries(reqs.map((r) => {
      const c = subComp[r];
      const status = c === true ? "declared" : c && typeof c === "object" && COMPLIANCE_STATUSES.includes(c.status) ? c.status : "missing";
      return [r, { status, evidence_ref: (c && c.evidence_ref) || "", expires_on: (c && c.expires_on) || "" }];
    })),
  }));
  const [errors, setErrors] = useState({});
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const setComp = (r, k, v) => setF({ ...f, comp: { ...f.comp, [r]: { ...f.comp[r], [k]: v } } });
  const idp = `v-${quote.quote_id}`;

  async function submit(e) {
    e.preventDefault();
    if (pending) return;
    const errs = validateTerms(f);
    reqs.forEach((r, i) => {
      if (f.comp[r].status === "verified" && !f.comp[r].evidence_ref.trim()) errs[`comp${i}`] = "Verified compliance needs an evidence reference.";
    });
    if (f.note.trim().length < MIN_REASON) errs.note = `Write a reviewer note of at least ${MIN_REASON} characters.`;
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const compliance = {};
    for (const r of reqs) {
      const c = f.comp[r];
      const entry = { status: c.status };
      if (c.evidence_ref.trim()) entry.evidence_ref = c.evidence_ref.trim();
      if (c.expires_on) entry.expires_on = c.expires_on;
      compliance[r] = entry;
    }
    const body = { ...termsBody(f), compliance, warranty_scope_accepted: Boolean(f.scopeAccepted), reviewer_note: f.note.trim() };
    setPending(true); setServerError(null);
    try {
      onVerified(await verifyQuote(rfq.rfq_id, quote.quote_id, body));
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  }

  const err = (k) => errors[k] && <span className="field-error">{errors[k]}</span>;
  return (
    <form className="stack" onSubmit={submit} noValidate aria-label="Verify quote terms">
      <div>
        <h4 className="label">Verify terms</h4>
        <p className="muted small">Enter the terms exactly as confirmed against the source document. Only verified terms are compared; differences from the submitted values are recorded.</p>
      </div>
      <div className="grid-2"><TermsFields f={f} set={set} err={err} idp={idp} /></div>
      {rfq.warranty_scope_required && <p className="muted small">Warranty scope required by the RFQ: {rfq.warranty_scope_required}</p>}
      <label htmlFor={`${idp}-scope-ok`} className="row" style={{ fontWeight: 400 }}>
        <input id={`${idp}-scope-ok`} type="checkbox" className="checkbox" checked={f.scopeAccepted}
          onChange={(e) => setF({ ...f, scopeAccepted: e.target.checked })} />
        Warranty scope accepted (meets the RFQ requirement)
      </label>
      {reqs.length > 0 && (
        <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="label">Compliance per requirement</legend>
          {reqs.map((r, i) => (
            <div className="compliance-row" key={r}>
              <strong className="small">{r}</strong>
              <div className="field">
                <label htmlFor={`${idp}-c${i}-status`}>{r} status</label>
                <select id={`${idp}-c${i}-status`} className="select" value={f.comp[r].status} onChange={(e) => setComp(r, "status", e.target.value)}>
                  {COMPLIANCE_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor={`${idp}-c${i}-ref`}>{r} evidence reference</label>
                <input id={`${idp}-c${i}-ref`} className="input" value={f.comp[r].evidence_ref} maxLength={300}
                  onChange={(e) => setComp(r, "evidence_ref", e.target.value)} />
                {err(`comp${i}`)}
              </div>
              <div className="field">
                <label htmlFor={`${idp}-c${i}-exp`}>{r} expires on</label>
                <input id={`${idp}-c${i}-exp`} type="date" className="input" value={f.comp[r].expires_on}
                  onChange={(e) => setComp(r, "expires_on", e.target.value)} />
              </div>
            </div>
          ))}
        </fieldset>
      )}
      <div className="field">
        <label htmlFor={`${idp}-note`} className="required">Reviewer note</label>
        <textarea id={`${idp}-note`} className="textarea" value={f.note} onChange={set("note")} maxLength={1000} />
        {err("note")}
      </div>
      <InlineError error={serverError} />
      <div className="row"><button type="submit" className="btn btn-primary" disabled={pending}>{pending ? "Verifying…" : "Verify terms"}</button></div>
    </form>
  );
}

// ------------------------------------------------------------------------------------------ comparison
function ComparisonSection({ rfq, comparisons, onChanged }) {
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);
  const [fresh, setFresh] = useState(null);
  const [choice, setChoice] = useState("accept");
  const [quoteId, setQuoteId] = useState("");
  const [confirming, setConfirming] = useState(false);
  const cancelConfirm = useCallback(() => setConfirming(false), []);
  const cancelled = rfq.status === "cancelled";
  const latest = (comparisons.data || [])[0] || null;
  const cmp = fresh && (!latest || fresh.comparison_id !== latest.comparison_id || !latest.human_decision) ? fresh : latest;
  const rows = Array.isArray(cmp?.quotes) ? cmp.quotes : [];
  const eligible = rows.filter((r) => r.eligible && r.quote_id);
  const selected = quoteId || cmp?.recommended_quote_id || eligible[0]?.quote_id || "";
  const decision = cmp?.decision && typeof cmp.decision === "object" ? cmp.decision : null;

  async function run() {
    setRunning(true); setRunError(null);
    try {
      const out = await compareQuotes(rfq.rfq_id);
      setFresh(out); setQuoteId(""); setChoice("accept");
      comparisons.reload();
    } catch (err) {
      setRunError(err);
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="card stack" aria-labelledby="compare-h">
      <div className="row-between">
        <div className="row">
          <h2 id="compare-h" className="sourcing-section-title">Comparison</h2>
          <span className="badge badge-info">Decision support only</span>
        </div>
        <button type="button" className="btn" onClick={run} disabled={cancelled || running}>{running ? "Comparing…" : "Run comparison"}</button>
      </div>
      <p className="muted small">Hard gates first (verification, currency, delivery, warranty, compliance, budget), then lowest total cost. The score is explanatory only and does not change the ranking. Nothing is selected automatically.</p>
      {cancelled && <Notice tone="info">This RFQ is cancelled; comparisons are not allowed.</Notice>}
      <InlineError error={runError} />
      {comparisons.loading && !comparisons.data && !fresh ? <LoadingState label="Loading comparisons…" /> :
        comparisons.error && !fresh ? <ErrorState error={comparisons.error} onRetry={comparisons.reload} /> :
          !cmp ? <EmptyState title="No comparison yet" description="Verify quotes, then run a comparison." /> : (
            <div className="stack">
              <dl className="dl">
                <dt>Comparison</dt><dd className="mono">{cmp.comparison_id}</dd>
                <dt>Run</dt><dd>{cmp.created_by || "—"} · {formatDateTime(cmp.created_at)}</dd>
                <dt>Budget limit</dt><dd>{formatMoney(cmp.budget_limit, cmp.currency || rfq.currency || "INR")}</dd>
                <dt>Eligible quotes</dt><dd>{cmp.eligible_count ?? eligible.length}</dd>
                <dt>Suggested (lowest eligible cost)</dt><dd className="mono">{cmp.recommended_quote_id || "None"}</dd>
                <dt>Scoring policy</dt><dd>{cmp.scoring_policy_version != null ? `Version ${cmp.scoring_policy_version}` : cmp.scoring_error ? "Unavailable" : "—"}</dd>
              </dl>
              <CompareTable rows={rows} currency={cmp.currency || rfq.currency || "INR"} policyVersion={cmp.scoring_policy_version} />
              {cmp.human_decision && cmp.human_decision !== "pending" ? (
                <Notice tone="info" title={cmp.human_decision === "accepted" ? "Human selection recorded" : "Comparison rejected"}>
                  {cmp.human_decision === "accepted" ? `Selected quote ${decision?.quote_id || "—"}. ` : ""}
                  {decision?.reason ? `Reason: ${decision.reason}. ` : ""}
                  {decision?.decided_by ? `By ${decision.decided_by} · ${formatDateTime(decision.decided_at)}. ` : ""}
                  This is a sourcing record only — no supplier is contacted and nothing is ordered.
                </Notice>
              ) : !cancelled && (
                <div className="stack-sm">
                  <h3 className="label">Human decision</h3>
                  <div className="radio-row" role="radiogroup" aria-label="Decision">
                    <label htmlFor="cmp-accept"><input id="cmp-accept" type="radio" name="cmp-decision" checked={choice === "accept"}
                      onChange={() => setChoice("accept")} disabled={!eligible.length} />Accept an eligible quote</label>
                    <label htmlFor="cmp-reject"><input id="cmp-reject" type="radio" name="cmp-decision" checked={choice === "reject"}
                      onChange={() => setChoice("reject")} />Reject this comparison</label>
                  </div>
                  {choice === "accept" && (eligible.length ? (
                    <div className="field" style={{ maxWidth: 360 }}>
                      <label htmlFor="cmp-quote" className="required">Quote to accept</label>
                      <select id="cmp-quote" className="select" value={selected} onChange={(e) => setQuoteId(e.target.value)}>
                        {eligible.map((r) => <option key={r.quote_id} value={r.quote_id}>{r.quote_id} · {r.vendor_id} · {formatMoney(r.total_cost, cmp.currency || "INR")}</option>)}
                      </select>
                    </div>
                  ) : <p className="muted small">No eligible quotes to accept. You can reject this comparison.</p>)}
                  <div>
                    <button type="button" className="btn btn-primary" disabled={choice === "accept" && !eligible.length}
                      onClick={() => setConfirming(true)}>Record decision</button>
                  </div>
                </div>
              )}
              <ConfirmDialog open={confirming}
                title={choice === "accept" ? `Accept quote ${selected}?` : "Reject this comparison?"}
                description="This records a human selection only. No supplier is contacted and no order is placed."
                confirmLabel={choice === "accept" ? "Accept quote" : "Reject comparison"} tone={choice === "accept" ? "primary" : "danger"}
                requireReason={MIN_REASON} onCancel={cancelConfirm}
                onConfirm={async (reason) => {
                  await decideComparison(rfq.rfq_id, cmp.comparison_id, choice, choice === "accept" ? selected : null, reason);
                  setConfirming(false);
                  setFresh(null);
                  onChanged(choice === "accept" ? `Human selection recorded: quote ${selected} accepted.` : "Comparison rejected.");
                }} />
            </div>
          )}
    </section>
  );
}

function CompareTable({ rows, currency, policyVersion }) {
  const money = (v) => (v == null ? "—" : formatMoney(v, currency));
  return (
    <DataTable caption="Quote comparison" rows={rows} rowKey="quote_id"
      empty={<p className="muted small">No quotes to compare.</p>}
      columns={[
        { key: "rank", header: "Rank", align: "right", render: (r) => r.rank ?? "—" },
        { key: "quote_id", header: "Quote", render: (r) => <><span className="mono">{r.quote_id}</span><div className="muted small">{r.vendor_id}</div></> },
        { key: "total_cost", header: "Total cost", align: "right", render: (r) => (r.total_cost == null ? "—" : (
          <>
            <strong>{money(r.total_cost)}</strong>
            <span className="cost-components">{money(r.unit_price)} × {r.quantity ?? "—"}</span>
            <span className="cost-components">Tax {money(r.tax_amount)} · Delivery {money(r.delivery_fee)} · Other {money(r.other_fees)}</span>
            {r.within_budget === false && <span className="cost-components">Over budget</span>}
          </>)) },
        { key: "delivery", header: "Delivery", render: (r) => (r.delivery_date ? <>{formatDate(r.delivery_date)}<div className="muted small">{r.delivery_compliant === false ? "Late" : r.delivery_compliant ? "On time" : ""}</div></> : "—") },
        { key: "warranty", header: "Warranty", render: (r) => (r.warranty_months == null ? "—" : <>{r.warranty_months} months<div className="muted small">{r.warranty_compliant === false ? "Does not meet requirement" : r.warranty_compliant ? "Meets requirement" : ""}</div></>) },
        { key: "compliance", header: "Compliance gate", render: (r) => {
          const gate = r.compliance_gate && typeof r.compliance_gate === "object" ? Object.entries(r.compliance_gate) : [];
          return gate.length ? <ul className="gate-list">{gate.map(([k, g]) => <li key={k}>{k}: {GATE_LABELS[g] || humanize(g)}</li>)}</ul> : "—";
        } },
        { key: "eligibility", header: "Eligibility", render: (r) => (
          <>
            <StatusBadge kind="vendor" status={r.eligibility || (r.eligible ? "eligible" : "ineligible")} />
            {Array.isArray(r.reasons) && r.reasons.length > 0 && <ul className="reason-list">{r.reasons.map((x, i) => <li key={i}>{String(x)}</li>)}</ul>}
          </>) },
        { key: "score", header: "Score (explanatory)", align: "right", render: (r) => (r.score && typeof r.score === "object"
          ? <>{r.score.total ?? "—"}{policyVersion != null && <div className="muted small">Policy v{policyVersion}</div>}</> : "—") },
      ]} />
  );
}
