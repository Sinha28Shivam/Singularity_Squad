import { AlertCircle, CheckCircle2, Info, Inbox, TriangleAlert } from "lucide-react";

export function LoadingState({ label = "Loading…" }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <div className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, description, action, icon: Icon = Inbox }) {
  return (
    <div className="state">
      <Icon size={28} aria-hidden="true" />
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  );
}

/** ErrorState shows the safe message from ApiError (never raw bodies) and an optional retry. */
export function ErrorState({ error, onRetry, title = "Something went wrong" }) {
  const message = error?.message || "Please try again.";
  return (
    <div className="state" role="alert">
      <AlertCircle size={28} color="var(--color-error)" aria-hidden="true" />
      <h3>{title}</h3>
      <p>{message}</p>
      {onRetry && <button type="button" className="btn" onClick={onRetry}>Try again</button>}
    </div>
  );
}

const ICONS = { info: Info, warning: TriangleAlert, error: AlertCircle, success: CheckCircle2 };

/** Inline notice/banner. `tone`: info | warning | error | success | neutral. */
export function Notice({ tone = "info", title, children, role }) {
  const Icon = ICONS[tone] || Info;
  return (
    <div className={`notice notice-${tone}`} role={role || (tone === "error" ? "alert" : undefined)}>
      <Icon size={16} aria-hidden="true" />
      <div className="stack-sm" style={{ gap: 2 }}>
        {title && <strong>{title}</strong>}
        {children && <div>{children}</div>}
      </div>
    </div>
  );
}

/** Inline form/action error from an ApiError. */
export function InlineError({ error }) {
  if (!error) return null;
  return <Notice tone="error">{error.message || "Something went wrong."}</Notice>;
}
