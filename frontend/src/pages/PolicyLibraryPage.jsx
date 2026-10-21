import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  archiveDocument, DEFAULT_MAX_FILE_MB, DOCUMENT_STATUSES, DOCUMENT_TYPES, extractRules, getDocument, listDocuments,
  listRateCards, MAX_FILES_PER_UPLOAD, reprocessDocument, uploadDocuments, verifyRateCard,
} from "../api/policies";
import ConfirmDialog from "../components/ConfirmDialog";
import DataTable from "../components/DataTable";
import PageHeader from "../components/PageHeader";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import Tabs from "../components/Tabs";
import { useAuth } from "../lib/auth";
import { formatDate, formatDateTime, humanize } from "../lib/format";
import { useApi } from "../lib/useApi";
import "../styles/pages/policy.css";

const UNVERIFIED = "Unverified — extracted text may be wrong. A reviewer must verify terms before comparison.";
const KEY_RE = /^[a-z0-9][a-z0-9_.-]*$/;
const RATE_STATUS = { needs_review: "Needs review", verified: "Verified", rejected: "Rejected" };
const RATE_TONE = { needs_review: "needs_review", verified: "approved", rejected: "rejected" };

const DOC_LABELS = {
  uploaded: "Uploaded", validating: "Validating", extracting: "Extracting text", needs_ocr: "Needs OCR",
  indexing: "Indexing", superseded: "Superseded",
};
function DocBadge({ status }) {
  return <StatusBadge kind="policy" status={status} label={DOC_LABELS[status]} />;
}

// ------------------------------------------------------------------ documents
function DocumentDetail({ documentId, isAdmin, onChanged, onClose }) {
  const { data: doc, error, loading, reload, setData } = useApi(() => getDocument(documentId), [documentId]);
  const [confirm, setConfirm] = useState(null);
  const [result, setResult] = useState(null);

  const run = useCallback(async () => {
    const action = confirm;
    if (action === "reprocess") {
      const res = await reprocessDocument(documentId);
      setConfirm(null);
      if (res?.document) setData({ ...(doc || {}), ...res.document });
      const r = res?.result || {};
      setResult({ tone: r.ok === false ? "warning" : "success",
        text: r.message || (r.ok === false ? `Reprocessing finished with a problem: ${humanize(r.error_code)}.` : "Reprocessing finished.") });
    } else if (action === "archive") {
      const res = await archiveDocument(documentId);
      setConfirm(null);
      if (res?.document) setData({ ...(doc || {}), ...res.document });
      setResult({ tone: "success", text: "Document archived. It is no longer used as policy evidence." });
    } else if (action === "extract") {
      const res = await extractRules(documentId);
      setConfirm(null);
      setResult({ tone: "success", extract: res,
        text: `Found ${(res?.candidates || []).length} candidate rule(s). Candidates are inactive until a reviewer approves them.` });
    }
    onChanged?.();
    reload();
  }, [confirm, documentId, doc, setData, onChanged, reload]);

  if (loading && !doc) return <section className="card"><LoadingState label="Loading document…" /></section>;
  if (error) return <section className="card"><ErrorState error={error} onRetry={reload} /></section>;
  if (!doc) return null;

  const lowText = doc.low_text_pages || [];
  const flags = [];
  if (doc.status === "needs_review") flags.push("This document needs review before it is used as evidence.");
  if (doc.status === "needs_ocr") flags.push("Little or no text could be extracted. The PDF may need OCR.");
  if (lowText.length) flags.push(`Pages with little extracted text: ${lowText.join(", ")}.`);
  if (doc.error_code) flags.push(`Problem reported: ${humanize(doc.error_code)}.`);
  if (doc.extraction_status && doc.extraction_status !== "complete") flags.push(`Text extraction: ${humanize(doc.extraction_status)}.`);
  if (doc.index_status && doc.index_status !== "ready") flags.push(`Search index: ${humanize(doc.index_status)}.`);
  if (doc.superseded_by) flags.push(`Replaced by a newer version (${doc.superseded_by}).`);

  const dialog = {
    reprocess: { title: "Reprocess this document?", label: "Reprocess",
      description: "Text is extracted and indexed again. Existing rules are not changed." },
    archive: { title: "Archive this document?", label: "Archive", tone: "danger",
      description: "An archived document is no longer used as policy evidence. This can't be undone from here." },
    extract: { title: "Extract candidate rules?", label: "Extract rules",
      description: "Candidate rules are proposed from this document. None become active until a reviewer approves them." },
  }[confirm];

  return (
    <section className="card stack" aria-labelledby="doc-detail-title">
      <div className="row-between">
        <h2 id="doc-detail-title">{doc.filename || doc.document_id}</h2>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
      </div>
      {result && <Notice tone={result.tone} role="status">{result.text}</Notice>}
      <div className="row"><DocBadge status={doc.status} /><span className="muted small">{humanize(doc.document_type)}</span></div>
      <dl className="dl">
        <dt>Version</dt><dd>v{doc.version ?? "?"}{doc.version_label ? ` (${doc.version_label})` : ""}</dd>
        <dt>Pages</dt><dd>{doc.page_count ?? 0}</dd>
        <dt>Vendor</dt><dd>{doc.vendor_key || "—"}</dd>
        <dt>Effective</dt><dd>{doc.effective_from || doc.effective_to ? `${formatDate(doc.effective_from)} – ${formatDate(doc.effective_to)}` : "—"}</dd>
        <dt>Uploaded</dt><dd>{formatDateTime(doc.uploaded_at)}{doc.uploaded_by ? ` by ${doc.uploaded_by}` : ""}</dd>
        <dt>Rate-card rows</dt><dd>{doc.rate_entry_count ?? 0}</dd>
      </dl>
      {flags.length > 0 ? (
        <Notice tone="warning" title="Review flags"><ul className="policy-list">{flags.map((f) => <li key={f}>{f}</li>)}</ul></Notice>
      ) : <p className="muted small">No review flags.</p>}

      {isAdmin && (
        <div className="policy-actions">
          {doc.status === "ready" && (
            <button type="button" className="btn btn-primary" onClick={() => { setResult(null); setConfirm("extract"); }}>Extract rules</button>
          )}
          {doc.status !== "archived" && (
            <button type="button" className="btn" onClick={() => { setResult(null); setConfirm("reprocess"); }}>Reprocess</button>
          )}
          {doc.status !== "archived" && (
            <button type="button" className="btn btn-danger" onClick={() => { setResult(null); setConfirm("archive"); }}>Archive</button>
          )}
        </div>
      )}

      {result?.extract && <ExtractResult res={result.extract} />}

      <details className="disclosure">
        <summary>Document history and technical details</summary>
        <div className="stack-sm">
          <dl className="dl">
            <dt>Document ID</dt><dd className="mono">{doc.document_id}</dd>
            <dt>Logical key</dt><dd className="mono">{doc.logical_key || "—"}</dd>
            <dt>Content hash</dt><dd className="mono">{doc.content_hash || "—"}</dd>
            <dt>Text chunks</dt><dd>{doc.chunk_count ?? 0}</dd>
          </dl>
          {(doc.events || []).length > 0 ? (
            <ul className="policy-list">
              {doc.events.map((e, i) => (
                <li key={i}>{humanize(e.event_type)} · {formatDateTime(e.created_at)}{e.actor ? ` · ${e.actor}` : ""}
                  {e.error_code ? ` · ${humanize(e.error_code)}` : ""}{e.reason ? ` · ${e.reason}` : ""}</li>
              ))}
            </ul>
          ) : <p className="muted small">No events recorded.</p>}
        </div>
      </details>

      <ConfirmDialog open={Boolean(confirm)} title={dialog?.title} description={dialog?.description}
        confirmLabel={dialog?.label} tone={dialog?.tone} onConfirm={run} onCancel={() => setConfirm(null)} />
    </section>
  );
}

function ExtractResult({ res }) {
  const cands = (res?.candidates || []).filter((c) => c && typeof c === "object");
  const warnings = res?.warnings || [];
  return (
    <div className="stack-sm">
      <Notice tone="warning">{UNVERIFIED}</Notice>
      {warnings.length > 0 && (
        <ul className="policy-list">{warnings.map((w, i) => <li key={i}>{typeof w === "string" ? humanize(w) : w?.message || humanize(w?.code)}</li>)}</ul>
      )}
      {cands.length === 0 ? <p className="muted small">No enforceable rules were found in this document.</p> : (
        <ul className="policy-list">
          {cands.map((c) => (
            <li key={`${c.rule_id}@${c.rule_version}`}>
              <strong>{c.name || c.rule_id}</strong> — {statusLabel(c.status, "policy")}
              {c.condition_text ? ` · ${c.condition_text}` : ""}
            </li>
          ))}
        </ul>
      )}
      <Link to="/admin/policy-rules">Review candidate rules</Link>
    </div>
  );
}

function DocumentsSection({ isAdmin, refreshKey }) {
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState(null);
  const { data, error, loading, reload } = useApi(() => listDocuments({ status: status || undefined }), [status, refreshKey]);
  const docs = Array.isArray(data?.documents) ? data.documents : [];
  const columns = [
    { key: "filename", header: "Document", render: (d) => (
      <div className="stack-sm" style={{ gap: 2 }}><span>{d.filename || d.document_id}</span>
        {d.vendor_key && <span className="muted small">Vendor {d.vendor_key}</span>}</div>) },
    { key: "document_type", header: "Type", render: (d) => humanize(d.document_type) },
    { key: "version", header: "Version", render: (d) => `v${d.version ?? "?"}${d.version_label ? ` (${d.version_label})` : ""}` },
    { key: "status", header: "Status", render: (d) => <DocBadge status={d.status} /> },
    { key: "updated", header: "Updated", render: (d) => formatDateTime(d.updated_at || d.uploaded_at) },
  ];
  return (
    <div className="stack">
      <div className="policy-toolbar">
        <div className="field">
          <label htmlFor="doc-status-filter">Status</label>
          <select id="doc-status-filter" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {DOCUMENT_STATUSES.map((s) => <option key={s} value={s}>{DOC_LABELS[s] || statusLabel(s, "policy")}</option>)}
          </select>
        </div>
        <button type="button" className="btn" onClick={reload} disabled={loading}>Refresh</button>
      </div>
      <DataTable caption="Policy documents" columns={columns} rows={docs} rowKey="document_id" loading={loading}
        error={error} onRetry={reload} onRowClick={(d) => setSelected(d.document_id)}
        empty={<EmptyState title="No policy documents" description={isAdmin ? "Upload a policy PDF to get started." : "Documents will appear here once uploaded."} />} />
      {selected && <DocumentDetail key={selected} documentId={selected} isAdmin={isAdmin} onChanged={reload}
        onClose={() => setSelected(null)} />}
    </div>
  );
}

// ------------------------------------------------------------------ upload
function emptyMeta() {
  return { document_type: "company_policy", vendor_key: "", version_label: "", logical_key: "", effective_from: "", effective_to: "" };
}

function validateFiles(files) {
  const errs = [];
  if (files.length > MAX_FILES_PER_UPLOAD) errs.push(`Select at most ${MAX_FILES_PER_UPLOAD} files.`);
  for (const f of files) {
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") errs.push(`${f.name}: only PDF files are accepted.`);
    if (f.size > DEFAULT_MAX_FILE_MB * 1024 * 1024) errs.push(`${f.name}: larger than ${DEFAULT_MAX_FILE_MB} MB.`);
  }
  return errs;
}

function validateMeta(m) {
  const e = {};
  if (!m.document_type) e.document_type = "Choose a document type.";
  if (m.vendor_key && !KEY_RE.test(m.vendor_key)) e.vendor_key = "Use lowercase letters, digits, '.', '_' or '-'.";
  if (m.logical_key && !KEY_RE.test(m.logical_key)) e.logical_key = "Use lowercase letters, digits, '.', '_' or '-'.";
  if (m.effective_from && m.effective_to && m.effective_to < m.effective_from) e.effective_to = "Must not be before the start date.";
  return e;
}

function UploadSection({ onUploaded }) {
  const inputRef = useRef(null);
  const [files, setFiles] = useState([]);
  const [metas, setMetas] = useState([]);
  const [fileErrors, setFileErrors] = useState([]);
  const [metaErrors, setMetaErrors] = useState([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [results, setResults] = useState(null);

  function pick(e) {
    const list = Array.from(e.target.files || []);
    setFiles(list);
    setMetas(list.map(() => emptyMeta()));
    setFileErrors(validateFiles(list));
    setMetaErrors([]);
    setResults(null);
    setError(null);
  }
  function setMeta(i, key, value) {
    setMetas((ms) => ms.map((m, j) => (j === i ? { ...m, [key]: value } : m)));
  }

  async function submit(e) {
    e.preventDefault();
    if (!files.length || pending) return;
    const fe = validateFiles(files);
    const me = metas.map(validateMeta);
    setFileErrors(fe);
    setMetaErrors(me);
    if (fe.length || me.some((x) => Object.keys(x).length)) return;
    const payload = metas.map((m) => Object.fromEntries(Object.entries(m).filter(([, v]) => v !== "")));
    setPending(true); setError(null); setResults(null);
    try {
      const res = await uploadDocuments(files, payload);
      setResults(Array.isArray(res?.results) ? res.results : []);
      setFiles([]); setMetas([]);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded?.();
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} noValidate>
      <Notice tone="info">
        PDF only · up to {MAX_FILES_PER_UPLOAD} files per upload · about {DEFAULT_MAX_FILE_MB} MB per file (the server enforces the
        exact limit). Each file is checked, extracted and indexed on its own; one bad file does not block the others.
        Rate-card rows always start as needing review.
      </Notice>
      <div className="field">
        <label htmlFor="policy-files" className="required">PDF files</label>
        <input id="policy-files" ref={inputRef} type="file" accept="application/pdf,.pdf" multiple onChange={pick}
          disabled={pending} aria-describedby="policy-files-help" />
        <span id="policy-files-help" className="help">Select one or more policy, contract or rate-card PDFs.</span>
        {fileErrors.map((m) => <span key={m} className="field-error">{m}</span>)}
      </div>

      {files.map((f, i) => {
        const m = metas[i] || emptyMeta();
        const er = metaErrors[i] || {};
        const id = (k) => `meta-${i}-${k}`;
        return (
          <fieldset key={`${f.name}-${i}`} className="policy-file-card" disabled={pending}>
            <legend className="sr-only">Details for {f.name}</legend>
            <h3>{i + 1}. {f.name}</h3>
            <div className="grid-3">
              <div className="field">
                <label htmlFor={id("type")} className="required">Document type</label>
                <select id={id("type")} className="select" value={m.document_type} aria-invalid={Boolean(er.document_type)}
                  onChange={(e) => setMeta(i, "document_type", e.target.value)}>
                  {DOCUMENT_TYPES.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}
                </select>
                {er.document_type && <span className="field-error">{er.document_type}</span>}
              </div>
              <div className="field">
                <label htmlFor={id("vendor")}>Vendor key</label>
                <input id={id("vendor")} className="input" maxLength={64} value={m.vendor_key} aria-invalid={Boolean(er.vendor_key)}
                  onChange={(e) => setMeta(i, "vendor_key", e.target.value)} />
                {er.vendor_key && <span className="field-error">{er.vendor_key}</span>}
              </div>
              <div className="field">
                <label htmlFor={id("label")}>Version label</label>
                <input id={id("label")} className="input" maxLength={64} value={m.version_label}
                  onChange={(e) => setMeta(i, "version_label", e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor={id("logical")}>Logical key</label>
                <input id={id("logical")} className="input" maxLength={96} value={m.logical_key} aria-invalid={Boolean(er.logical_key)}
                  onChange={(e) => setMeta(i, "logical_key", e.target.value)} />
                <span className="help">Links versions of the same document; a new upload with the same key replaces the previous version.</span>
                {er.logical_key && <span className="field-error">{er.logical_key}</span>}
              </div>
              <div className="field">
                <label htmlFor={id("from")}>Effective from</label>
                <input id={id("from")} type="date" className="input" value={m.effective_from}
                  onChange={(e) => setMeta(i, "effective_from", e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor={id("to")}>Effective to</label>
                <input id={id("to")} type="date" className="input" value={m.effective_to} aria-invalid={Boolean(er.effective_to)}
                  onChange={(e) => setMeta(i, "effective_to", e.target.value)} />
                {er.effective_to && <span className="field-error">{er.effective_to}</span>}
              </div>
            </div>
          </fieldset>
        );
      })}

      <InlineError error={error} />
      <div>
        <button type="submit" className="btn btn-primary" disabled={!files.length || pending || fileErrors.length > 0}>
          {pending ? "Uploading…" : files.length ? `Upload ${files.length} file${files.length > 1 ? "s" : ""}` : "Upload"}
        </button>
      </div>
      {pending && <LoadingState label="Uploading and processing — this can take a minute…" />}

      {results && (
        <section className="card stack-sm" aria-labelledby="upload-results">
          <h2 id="upload-results">Upload results</h2>
          {results.length === 0 ? <p className="muted small">The server returned no results.</p> : (
            <ul className="policy-list">
              {results.map((r, i) => (
                <li key={`${r.filename}-${i}`}>
                  <strong>{r.filename}</strong> — {r.ok ? "Accepted" : "Not accepted"}
                  {r.status ? ` · ${DOC_LABELS[r.status] || statusLabel(r.status, "policy")}` : ""}
                  {r.duplicate_of ? ` · same file as ${r.duplicate_of}` : ""}
                  {!r.ok && (r.message || r.error_code) ? ` · ${r.message || humanize(r.error_code)}` : ""}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </form>
  );
}

// ------------------------------------------------------------------ rate cards (shared with policy review)
export function RateCardsSection() {
  const [status, setStatus] = useState("needs_review");
  const [confirm, setConfirm] = useState(null); // {row, decision}
  const [result, setResult] = useState(null);
  const { data, error, loading, reload } = useApi(() => listRateCards({ verification_status: status || undefined }), [status]);
  const rows = Array.isArray(data?.rate_cards) ? data.rate_cards : [];

  const decide = useCallback(async () => {
    const { row, decision } = confirm;
    let res;
    try {
      res = await verifyRateCard(row.rate_id, decision);
    } catch (e) {
      if (e?.status === 409) { setConfirm(null); setResult({ tone: "warning", text: e.message }); reload(); return; }
      throw e;
    }
    setConfirm(null);
    const expected = decision === "verify" ? "verified" : "rejected";
    const got = res?.verification_status ?? res?.entry?.verification_status;
    setResult(got === expected
      ? { tone: "success", text: `Rate ${row.rate_id} recorded as ${RATE_STATUS[got].toLowerCase()}.` }
      : { tone: "warning", text: "The server did not confirm the change. Refresh before trying again." });
    reload();
  }, [confirm, reload]);

  const columns = [
    { key: "item", header: "Item", render: (r) => (
      <div className="stack-sm" style={{ gap: 2 }}><span>{r.description || r.item_or_service_code || r.rate_id}</span>
        <span className="muted small">{r.vendor_name || r.vendor_key || "—"}</span></div>) },
    { key: "rate", header: "Unit rate", align: "right", render: (r) => `${r.currency || "INR"} ${r.unit_rate ?? "—"} / ${r.unit || "?"}` },
    { key: "source", header: "Source", render: (r) => `${r.document_id || "?"} · p. ${r.source_page ?? "?"}` },
    { key: "status", header: "Verification", render: (r) => (
      <StatusBadge kind="vendor" status={RATE_TONE[r.verification_status] || r.verification_status}
        label={RATE_STATUS[r.verification_status] || humanize(r.verification_status)} />) },
    { key: "actions", header: "Actions", render: (r) => r.verification_status === "needs_review" ? (
      <div className="policy-actions" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="btn btn-sm" onClick={() => { setResult(null); setConfirm({ row: r, decision: "verify" }); }}>Verify</button>
        <button type="button" className="btn btn-sm btn-danger" onClick={() => { setResult(null); setConfirm({ row: r, decision: "reject" }); }}>Reject</button>
      </div>) : (r.verified_by ? <span className="muted small">by {r.verified_by}</span> : "—") },
  ];

  return (
    <div className="stack">
      <Notice tone="warning">{UNVERIFIED} Only verified rows are used to check quoted prices.</Notice>
      {result && <Notice tone={result.tone} role="status">{result.text}</Notice>}
      <div className="policy-toolbar">
        <div className="field">
          <label htmlFor="rate-status-filter">Verification</label>
          <select id="rate-status-filter" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All</option>
            {Object.entries(RATE_STATUS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <button type="button" className="btn" onClick={reload} disabled={loading}>Refresh</button>
      </div>
      <DataTable caption="Rate-card rows" columns={columns} rows={rows} rowKey="rate_id" loading={loading} error={error}
        onRetry={reload} empty={<EmptyState title="No rate-card rows match" description="Rows extracted from rate-card PDFs appear here." />} />
      <ConfirmDialog open={Boolean(confirm)}
        title={confirm?.decision === "verify" ? "Verify this rate?" : "Reject this rate?"}
        description={confirm ? `${confirm.row.description || confirm.row.rate_id}: ${confirm.row.currency || "INR"} ${confirm.row.unit_rate} / ${confirm.row.unit || "?"}. `
          + "Compare it with the source page before deciding." : ""}
        confirmLabel={confirm?.decision === "verify" ? "Verify" : "Reject"} tone={confirm?.decision === "reject" ? "danger" : "primary"}
        onConfirm={decide} onCancel={() => setConfirm(null)} />
    </div>
  );
}

// ------------------------------------------------------------------ page
export default function PolicyLibraryPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState("documents");
  const [refreshKey, setRefreshKey] = useState(0);
  const tabs = [
    { id: "documents", label: "Documents" },
    ...(isAdmin ? [{ id: "upload", label: "Upload" }] : []),
    { id: "rates", label: "Rate cards" },
  ];
  return (
    <div className="stack">
      <PageHeader title="Policy library"
        description="Policy PDFs, vendor contracts and rate cards used as cited evidence. Nothing here places an order or moves money." />
      <Tabs tabs={tabs} value={tab} onChange={setTab} label="Policy library sections" />
      {tab === "documents" && <DocumentsSection isAdmin={isAdmin} refreshKey={refreshKey} />}
      {tab === "upload" && isAdmin && <UploadSection onUploaded={() => setRefreshKey((k) => k + 1)} />}
      {tab === "rates" && <RateCardsSection />}
    </div>
  );
}
