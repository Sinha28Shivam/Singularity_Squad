import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { InlineError } from "./States";

/**
 * Accessible confirmation dialog for consequential actions.
 * Props: open, title, description, confirmLabel, tone ("primary"|"danger"), requireReason (min chars),
 * reasonLabel, onConfirm(reason) -> Promise, onCancel. The dialog stays open and shows the server error
 * if onConfirm rejects; it only closes after the server confirms (spec §6.6).
 */
export default function ConfirmDialog({
  open, title, description, confirmLabel = "Confirm", tone = "primary", requireReason = 0,
  reasonLabel = "Reason", children, onConfirm, onCancel,
}) {
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const titleId = useId();
  const descId = useId();
  const firstRef = useRef(null);
  const lastFocus = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    lastFocus.current = document.activeElement;
    setReason(""); setError(null); setPending(false);
    const t = setTimeout(() => firstRef.current?.focus(), 0);
    const onKey = (e) => { if (e.key === "Escape") onCancel?.(); };
    document.addEventListener("keydown", onKey);
    return () => { clearTimeout(t); document.removeEventListener("keydown", onKey); lastFocus.current?.focus?.(); };
  }, [open, onCancel]);

  if (!open) return null;
  const tooShort = requireReason > 0 && reason.trim().length < requireReason;

  async function submit(e) {
    e.preventDefault();
    if (tooShort || pending) return;
    setPending(true); setError(null);
    try { await onConfirm?.(reason.trim()); } catch (err) { setError(err); setPending(false); }
  }

  return createPortal(
    <div className="dialog-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget && !pending) onCancel?.(); }}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined} onSubmit={submit}>
        <h2 id={titleId}>{title}</h2>
        {description && <p id={descId} className="muted">{description}</p>}
        {children}
        {requireReason > 0 && (
          <div className="field">
            <label htmlFor={`${titleId}-reason`} className="required">{reasonLabel}</label>
            <textarea id={`${titleId}-reason`} ref={firstRef} className="textarea" value={reason}
              onChange={(e) => setReason(e.target.value)} maxLength={1000} aria-invalid={reason.length > 0 && tooShort} />
            <span className="help">At least {requireReason} characters.</span>
          </div>
        )}
        <InlineError error={error} />
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={pending}
            ref={requireReason > 0 ? undefined : firstRef}>Cancel</button>
          <button type="submit" className={`btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`} disabled={tooShort || pending}>
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
