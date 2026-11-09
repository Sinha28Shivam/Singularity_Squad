import { useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import { saveBlob } from "../api/client";
import {
  downloadInvoicePdf, getInvoiceDocument, invoicePdfProblem, listInvoiceDocuments, rejectInvoiceDocument,
  uploadInvoiceDocument, verifyInvoiceDocument,
} from "../api/invoiceIntake";

const STATUS = {
  pending_review: ["Pending review", "warning"],
  verified: ["Verified", "success"],
  rejected: ["Rejected", "error"],
};

function DocStatus({ status }) {
  const [label, tone] = STATUS[status] || [status || "Unknown", "neutral"];
  return <span className={`badge badge-${tone}`} data-status={status}>{label}</span>;
}

const money = (n) => (n === null || n === undefined || n === "" ? "—" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const r2 = (n) => Math.round((Number(n) + Number.EPSILON) * 100) / 100;

// ------------------------------------------------------------------------------------------- upload
function UploadForm({ onUploaded }) {
  const [file, setFile] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  function pick(e) {
    const f = e.target.files?.[0] || null;
    setFile(f);
    setProblem(f ? invoicePdfProblem(f) : "");
  }

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    const p = invoicePdfProblem(file);
    setProblem(p);
    if (p) return;
    setBusy(true); setError(null);
    try {
      const doc = await uploadInvoiceDocument(file);
      onUploaded(doc);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card stack" aria-labelledby="upload-h">
      <h2 id="upload-h">Upload invoice PDF</h2>
      <form className="stack" onSubmit={submit} noValidate aria-label="Invoice PDF upload">
        <p className="muted small">PDF only · up to 5 MiB · up to 20 pages · not password-protected. An optional delivery note can follow on a page headed “Delivery Note”. Extracted values stay unverified until you check them.</p>
        <div className="field">
          <label htmlFor="invoice-file" className="required">Invoice PDF</label>
          <input id="invoice-file" type="file" accept="application/pdf" className="input" onChange={pick} aria-invalid={Boolean(problem)} />
          {problem && <span className="field-error">{problem}</span>}
        </div>
        <InlineError error={error} />
        <div className="row">
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Uploading…" : "Upload invoice"}</button>
        </div>
      </form>
    </section>
  );
}

// ------------------------------------------------------------------------------------------- verify
function initialForm(doc) {
  const v = doc.verified || doc.extracted || {};
  const d = doc.verified_delivery || doc.extracted_delivery || null;
  const str = (x) => (x === null || x === undefined ? "" : String(x));
  return {
    invoice_id: str(v.invoice_id), vendor_id: str(v.vendor_id), po_id: str(v.po_id), invoice_date: str(v.invoice_date),
    delivery_id: str(v.delivery_id), currency: str(v.currency) || "INR",
    subtotal: str(v.subtotal), tax: str(v.tax), total: str(v.total),
    line_items: (v.line_items || []).map((li) => ({ description: str(li.description), quantity: str(li.quantity), unit_price: str(li.unit_price), line_total: str(li.line_total) })),
    include_delivery: Boolean(d && Object.keys(d.items || {}).length),
    delivered_on: str(d?.delivered_on), signed_by: str(d?.signed_by),
    delivery_items: Object.entries(d?.items || {}).map(([description, qty]) => ({ description, qty: str(qty) })),
  };
}

function toBody(f) {
  const num = (s) => (s === "" ? NaN : Number(s));
  const body = {
    invoice_id: f.invoice_id.trim(), vendor_id: f.vendor_id.trim(), po_id: f.po_id.trim(), invoice_date: f.invoice_date,
    delivery_id: f.delivery_id.trim(), currency: f.currency.trim().toUpperCase(),
    line_items: f.line_items.map((li) => ({ description: li.description.trim(), quantity: num(li.quantity), unit_price: num(li.unit_price), line_total: num(li.line_total) })),
    subtotal: num(f.subtotal), tax: num(f.tax), total: num(f.total),
  };
  if (f.include_delivery) {
    body.delivery = {
      delivery_id: f.delivery_id.trim(), delivered_on: f.delivered_on, signed_by: f.signed_by.trim(),
      items: Object.fromEntries(f.delivery_items.filter((i) => i.description.trim()).map((i) => [i.description.trim(), num(i.qty)])),
    };
  }
  return body;
}

/** Same deterministic checks the server runs on verify (the server is authoritative). */
export function arithmeticProblems(f) {
  const out = [];
  let sum = 0;
  f.line_items.forEach((li, i) => {
    const exp = r2(Number(li.quantity) * Number(li.unit_price));
    sum += Number(li.line_total) || 0;
    if (Math.abs(exp - Number(li.line_total)) > 0.01) out.push(`Line ${i + 1}: quantity × unit price = ${money(exp)}`);
  });
  if (f.line_items.length && Math.abs(r2(sum) - Number(f.subtotal)) > 0.01) out.push(`Subtotal should be ${money(r2(sum))}`);
  if (Math.abs(r2(Number(f.subtotal) + Number(f.tax)) - Number(f.total)) > 0.01) out.push(`Total should be ${money(r2(Number(f.subtotal) + Number(f.tax)))}`);
  return out;
}

function Field({ id, label, value, onChange, type = "text", required = true, ...rest }) {
  return (
    <div className="field">
      <label htmlFor={id} className={required ? "required" : undefined}>{label}</label>
      <input id={id} type={type} className="input" value={value} onChange={(e) => onChange(e.target.value)} {...rest} />
    </div>
  );
}

function ReviewPanel({ docId, onChanged, onClose }) {
  const [doc, setDoc] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [reason, setReason] = useState("");
  const [showText, setShowText] = useState(false);

  useEffect(() => {
    let live = true;
    setDoc(null); setLoadError(null); setError(null); setReason("");
    getInvoiceDocument(docId).then((d) => { if (live) { setDoc(d); setForm(initialForm(d)); } }).catch((e) => live && setLoadError(e));
    return () => { live = false; };
  }, [docId]);

  if (loadError) return <section className="card"><ErrorState error={loadError} /></section>;
  if (!doc || !form) return <section className="card"><LoadingState label="Loading invoice document…" /></section>;

  const pending = doc.status === "pending_review";
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));
  const setLine = (i, k) => (v) => setForm((f) => ({ ...f, line_items: f.line_items.map((li, j) => (j === i ? { ...li, [k]: v } : li)) }));
  const setDel = (i, k) => (v) => setForm((f) => ({ ...f, delivery_items: f.delivery_items.map((li, j) => (j === i ? { ...li, [k]: v } : li)) }));
  const problems = arithmeticProblems(form);
  const serverErrors = error?.body?.detail?.errors || [];

  async function act(fn) {
    setBusy(true); setError(null);
    try {
      const updated = await fn();
      setDoc(updated); setForm(initialForm(updated));
      onChanged(updated);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    try { saveBlob(await downloadInvoicePdf(doc.doc_id), doc.filename || "invoice.pdf"); } catch (err) { setError(err); }
  }

  return (
    <section className="card stack" aria-labelledby="review-h">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="review-h">Review {doc.filename} <DocStatus status={doc.status} /></h2>
        <div className="row">
          <button type="button" className="btn" onClick={download}>Download PDF</button>
          <button type="button" className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
      {(doc.injection_flags || []).length > 0 && (
        <Notice tone="error" title="Possible prompt-injection text in this PDF">
          The document contains instruction-like text ({doc.injection_flags.join("; ")}). It was treated as data only; check every value against the original PDF.
        </Notice>
      )}
      {(doc.warnings || []).length > 0 && (
        <Notice tone="warning" title="Extraction warnings">
          <ul data-testid="extraction-warnings">{doc.warnings.map((w) => <li key={w}><code>{w}</code></li>)}</ul>
        </Notice>
      )}
      <Notice tone="info">Values below were extracted automatically ({doc.extraction?.method || "regex"}) and are untrusted until you verify them.</Notice>
      {doc.status === "rejected" && <Notice tone="error" title="Rejected">{doc.rejected_reason}</Notice>}
      {doc.status === "verified" && <Notice tone="success" title="Verified">Invoice {doc.verified?.invoice_id} is available for invoice matching.</Notice>}

      <form className="stack" aria-label="Verify invoice fields" noValidate onSubmit={(e) => { e.preventDefault(); if (!problems.length) act(() => verifyInvoiceDocument(doc.doc_id, toBody(form))); }}>
        <fieldset disabled={!pending || busy} className="stack">
          <div className="grid-2">
            <Field id="f-invoice_id" label="Invoice ID" value={form.invoice_id} onChange={set("invoice_id")} placeholder="INV-…" />
            <Field id="f-vendor_id" label="Vendor ID" value={form.vendor_id} onChange={set("vendor_id")} />
            <Field id="f-po_id" label="PO ID" value={form.po_id} onChange={set("po_id")} />
            <Field id="f-invoice_date" label="Invoice date" type="date" value={form.invoice_date} onChange={set("invoice_date")} />
            <Field id="f-delivery_id" label="Delivery ID" value={form.delivery_id} onChange={set("delivery_id")} />
            <Field id="f-currency" label="Currency" value={form.currency} onChange={set("currency")} maxLength={3} />
          </div>
          <div className="table-wrap">
            <table className="table" aria-label="Line items">
              <thead><tr><th>Description</th><th className="num">Quantity</th><th className="num">Unit price</th><th className="num">Line total</th><th /></tr></thead>
              <tbody>
                {form.line_items.map((li, i) => (
                  <tr key={i}>
                    <td><input aria-label={`Line ${i + 1} description`} className="input" value={li.description} onChange={(e) => setLine(i, "description")(e.target.value)} /></td>
                    <td><input aria-label={`Line ${i + 1} quantity`} className="input" inputMode="decimal" value={li.quantity} onChange={(e) => setLine(i, "quantity")(e.target.value)} /></td>
                    <td><input aria-label={`Line ${i + 1} unit price`} className="input" inputMode="decimal" value={li.unit_price} onChange={(e) => setLine(i, "unit_price")(e.target.value)} /></td>
                    <td><input aria-label={`Line ${i + 1} line total`} className="input" inputMode="decimal" value={li.line_total} onChange={(e) => setLine(i, "line_total")(e.target.value)} /></td>
                    <td><button type="button" className="btn" onClick={() => setForm((f) => ({ ...f, line_items: f.line_items.filter((_, j) => j !== i) }))}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row"><button type="button" className="btn" onClick={() => setForm((f) => ({ ...f, line_items: [...f.line_items, { description: "", quantity: "1", unit_price: "0", line_total: "0" }] }))}>Add line</button></div>
          <div className="grid-2">
            <Field id="f-subtotal" label="Subtotal" value={form.subtotal} onChange={set("subtotal")} inputMode="decimal" />
            <Field id="f-tax" label="Tax" value={form.tax} onChange={set("tax")} inputMode="decimal" />
            <Field id="f-total" label="Total" value={form.total} onChange={set("total")} inputMode="decimal" />
          </div>
          <div className="field">
            <label><input type="checkbox" checked={form.include_delivery} onChange={(e) => set("include_delivery")(e.target.checked)} /> Include delivery note</label>
          </div>
          {form.include_delivery && (
            <div className="stack-sm" aria-label="Delivery note">
              <div className="grid-2">
                <Field id="f-delivered_on" label="Delivered on" type="date" value={form.delivered_on} onChange={set("delivered_on")} />
                <Field id="f-signed_by" label="Signed by" value={form.signed_by} onChange={set("signed_by")} />
              </div>
              {form.delivery_items.map((it, i) => (
                <div className="row" key={i}>
                  <input aria-label={`Delivered item ${i + 1}`} className="input" value={it.description} onChange={(e) => setDel(i, "description")(e.target.value)} />
                  <input aria-label={`Delivered quantity ${i + 1}`} className="input" inputMode="decimal" value={it.qty} onChange={(e) => setDel(i, "qty")(e.target.value)} />
                </div>
              ))}
              <div className="row"><button type="button" className="btn" onClick={() => setForm((f) => ({ ...f, delivery_items: [...f.delivery_items, { description: "", qty: "" }] }))}>Add delivered item</button></div>
            </div>
          )}
          {pending && problems.length > 0 && (
            <Notice tone="warning" title="Arithmetic does not add up"><ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul></Notice>
          )}
          {serverErrors.length > 0 && <ul className="field-error">{serverErrors.map((e) => <li key={e.field}>{e.field}: {e.message}</li>)}</ul>}
          <InlineError error={error} />
          {pending && (
            <div className="row">
              <button type="submit" className="btn btn-primary" disabled={busy || problems.length > 0}>Verify invoice</button>
            </div>
          )}
        </fieldset>
      </form>

      {pending && (
        <form className="stack-sm" aria-label="Reject invoice" noValidate onSubmit={(e) => { e.preventDefault(); if (reason.trim().length >= 3) act(() => rejectInvoiceDocument(doc.doc_id, reason.trim())); }}>
          <div className="field">
            <label htmlFor="reject-reason" className="required">Rejection reason</label>
            <textarea id="reject-reason" className="input" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <div className="row"><button type="submit" className="btn btn-danger" disabled={busy || reason.trim().length < 3}>Reject document</button></div>
        </form>
      )}

      <div>
        <button type="button" className="btn" onClick={() => setShowText((s) => !s)} aria-expanded={showText}>{showText ? "Hide" : "Show"} extracted text (untrusted)</button>
        {showText && <pre className="card-quiet small" style={{ whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{doc.extracted_text || "(no text)"}</pre>}
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------------------------- page
export default function InvoiceIntakePage() {
  const [docs, setDocs] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [filter, setFilter] = useState("");

  const load = useCallback(() => {
    setError(null);
    listInvoiceDocuments(filter || undefined).then((r) => setDocs(r.documents || [])).catch(setError);
  }, [filter]);
  useEffect(() => { load(); }, [load]);

  function upsert(doc) {
    setDocs((list) => [doc, ...(list || []).filter((d) => d.doc_id !== doc.doc_id)]);
  }

  return (
    <div className="stack">
      <PageHeader title="Invoice intake" description="Upload vendor invoice PDFs (with optional delivery notes), check the extracted fields and verify them for invoice matching." />
      <UploadForm onUploaded={(doc) => { upsert(doc); setSelected(doc.doc_id); }} />
      <section className="card stack" aria-labelledby="docs-h">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="docs-h">Invoice documents</h2>
          <label className="row small">Status
            <select className="select" aria-label="Filter by status" value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="">All</option>
              <option value="pending_review">Pending review</option>
              <option value="verified">Verified</option>
              <option value="rejected">Rejected</option>
            </select>
          </label>
        </div>
        {error ? <ErrorState error={error} onRetry={load} /> : docs === null ? <LoadingState label="Loading invoice documents…" /> : docs.length === 0 ? (
          <EmptyState title="No invoice documents yet" description="Upload a vendor invoice PDF to start." />
        ) : (
          <div className="table-wrap">
            <table className="table" aria-label="Invoice documents">
              <thead><tr><th>File</th><th>Invoice</th><th>Vendor</th><th className="num">Total</th><th>Status</th><th>Flags</th><th /></tr></thead>
              <tbody>
                {docs.map((d) => {
                  const v = d.verified || d.extracted || {};
                  return (
                    <tr key={d.doc_id}>
                      <td>{d.filename}</td>
                      <td className="mono">{v.invoice_id || "—"}</td>
                      <td>{v.vendor_id || "—"}</td>
                      <td className="num">{money(v.total)}</td>
                      <td><DocStatus status={d.status} /></td>
                      <td>
                        {(d.injection_flags || []).length > 0 && <span className="badge badge-error">Injection signal</span>}{" "}
                        {(d.warnings || []).length > 0 && <span className="badge badge-warning">{d.warnings.length} warning{d.warnings.length === 1 ? "" : "s"}</span>}
                      </td>
                      <td><button type="button" className="btn" onClick={() => setSelected(d.doc_id)}>{d.status === "pending_review" ? "Review" : "View"}</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {selected && <ReviewPanel docId={selected} onChanged={upsert} onClose={() => setSelected(null)} />}
    </div>
  );
}
