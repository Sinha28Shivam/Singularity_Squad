import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import { InlineError, Notice } from "../components/States";
import {
  createMyTicket, EMPTY_FORM, EVENT_CATEGORIES, SERVICES, toSubmission, validateForm,
} from "../api/requests";
import "../styles/pages/requests.css";

export const NOT_AN_ORDER = "Submitting a request doesn't place an order.";

const FIELD_ORDER = ["title", "event_category", "service", "event_date", "location", "attendees", "budget_inr", "notes"];

/**
 * TicketSubmission form, grouped by meaning (spec §6.3). Values are kept after any error.
 * onSubmit(payload) -> Promise; a rejection is shown inline and the form stays filled.
 */
export function RequestForm({ initial, submitLabel = "Submit request", pendingLabel = "Submitting…", onSubmit, onCancel, idPrefix = "req" }) {
  const [form, setForm] = useState(() => ({ ...EMPTY_FORM, ...(initial || {}) }));
  const [errors, setErrors] = useState({});
  const [touched, setTouched] = useState(false);
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState(null);
  const formRef = useRef(null);
  const id = (k) => `${idPrefix}-${k}`;

  function set(k, v) {
    const next = { ...form, [k]: v };
    setForm(next);
    if (touched) setErrors(validateForm(next));
  }

  async function submit(e) {
    e.preventDefault();
    if (pending) return;
    setTouched(true);
    const errs = validateForm(form);
    setErrors(errs);
    if (Object.keys(errs).length) {
      const first = FIELD_ORDER.find((k) => errs[k]);
      formRef.current?.querySelector(`#${id(first)}`)?.focus();
      return;
    }
    setPending(true); setServerError(null);
    try {
      await onSubmit(toSubmission(form));
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  }

  const err = (k) => errors[k] ? <span className="field-error" id={`${id(k)}-err`}>{errors[k]}</span> : null;
  const aria = (k, help) => ({
    id: id(k),
    "aria-invalid": errors[k] ? true : undefined,
    "aria-describedby": [errors[k] ? `${id(k)}-err` : null, help ? `${id(k)}-help` : null].filter(Boolean).join(" ") || undefined,
  });

  return (
    <form className="req-form" onSubmit={submit} noValidate ref={formRef}>
      <fieldset>
        <legend>What is needed?</legend>
        <div className="field">
          <label htmlFor={id("title")} className="required">Summary</label>
          <input {...aria("title", true)} className="input" value={form.title} maxLength={120}
            placeholder="Quarterly team lunch" onChange={(e) => set("title", e.target.value)} />
          <span className="help" id={`${id("title")}-help`}>A short title, 3–120 characters.</span>
          {err("title")}
        </div>
        <div className="grid-2">
          <div className="field">
            <label htmlFor={id("event_category")} className="required">Event type</label>
            <select {...aria("event_category")} className="select" value={form.event_category}
              onChange={(e) => set("event_category", e.target.value)}>
              {EVENT_CATEGORIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            {err("event_category")}
          </div>
          <div className="field">
            <label htmlFor={id("service")} className="required">Catering service</label>
            <select {...aria("service")} className="select" value={form.service} onChange={(e) => set("service", e.target.value)}>
              {SERVICES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            {err("service")}
          </div>
        </div>
      </fieldset>

      <fieldset>
        <legend>When and where?</legend>
        <div className="grid-2">
          <div className="field">
            <label htmlFor={id("event_date")} className="required">Event date</label>
            <input {...aria("event_date")} className="input" type="date" value={form.event_date}
              onChange={(e) => set("event_date", e.target.value)} />
            {err("event_date")}
          </div>
          <div className="field">
            <label htmlFor={id("location")}>Location</label>
            <input {...aria("location")} className="input" value={form.location} maxLength={120}
              placeholder="Bengaluru office" onChange={(e) => set("location", e.target.value)} />
            {err("location")}
          </div>
        </div>
      </fieldset>

      <fieldset>
        <legend>Budget and quantity</legend>
        <div className="grid-2">
          <div className="field">
            <label htmlFor={id("attendees")} className="required">Attendees</label>
            <input {...aria("attendees", true)} className="input" type="number" inputMode="numeric" min={1} max={5000} step={1}
              value={form.attendees} onChange={(e) => set("attendees", e.target.value)} />
            <span className="help" id={`${id("attendees")}-help`}>1 to 5,000 people.</span>
            {err("attendees")}
          </div>
          <div className="field">
            <label htmlFor={id("budget_inr")} className="required">Budget (INR)</label>
            <input {...aria("budget_inr", true)} className="input" type="number" inputMode="decimal" min={1} step="any"
              value={form.budget_inr} onChange={(e) => set("budget_inr", e.target.value)} />
            <span className="help" id={`${id("budget_inr")}-help`}>Your total budget for the event, including tax.</span>
            {err("budget_inr")}
          </div>
        </div>
        <label className="checkbox" htmlFor={id("approved_vendor_only")}>
          <input id={id("approved_vendor_only")} type="checkbox" checked={form.approved_vendor_only}
            onChange={(e) => set("approved_vendor_only", e.target.checked)} />
          Use approved vendors only
        </label>
      </fieldset>

      <fieldset>
        <legend>Additional context</legend>
        <div className="field">
          <label htmlFor={id("notes")}>Notes</label>
          <textarea {...aria("notes", true)} className="textarea" value={form.notes} maxLength={2000} rows={4}
            placeholder="Dietary needs, timing, anything else the team should know"
            onChange={(e) => set("notes", e.target.value)} />
          <span className="help" id={`${id("notes")}-help`}>Optional, up to 2,000 characters.</span>
          {err("notes")}
        </div>
      </fieldset>

      {Object.keys(errors).length > 0 && (
        <Notice tone="error" title="Please fix the highlighted fields.">Your entries have been kept.</Notice>
      )}
      <InlineError error={serverError} />
      <p className="muted small">{NOT_AN_ORDER}</p>
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={pending}>{pending ? pendingLabel : submitLabel}</button>
        {onCancel && <button type="button" className="btn" onClick={onCancel} disabled={pending}>Cancel</button>}
      </div>
    </form>
  );
}

export default function NewRequestPage() {
  const [created, setCreated] = useState(null);
  const [formKey, setFormKey] = useState(0);

  if (created) {
    const tid = created.ticket_id;
    return (
      <div className="stack">
        <PageHeader title="New request" description="Ask the operations team to arrange a workplace event." />
        <div className="card stack-sm" role="status">
          <h2>Request submitted</h2>
          <div className="row">
            <span>Request ID</span>
            <span className="mono" data-testid="created-id">{tid}</span>
            <StatusBadge status={created.status || "submitted"} />
          </div>
          <p>An operations admin will review it. You'll see updates on the request page.</p>
          <p className="muted small">{NOT_AN_ORDER}</p>
          <div className="row">
            {tid && <Link className="btn btn-primary" to={`/requests/${encodeURIComponent(tid)}`}>View request {tid}</Link>}
            <button type="button" className="btn" onClick={() => { setCreated(null); setFormKey((k) => k + 1); }}>Submit another request</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="stack">
      <PageHeader title="New request" description="Ask the operations team to arrange a workplace event. Fields marked required must be filled in." />
      <RequestForm key={formKey} idPrefix="new" onSubmit={async (payload) => setCreated(await createMyTicket(payload))} />
    </div>
  );
}
