import { formatDateTime, humanize } from "../lib/format";
import StatusBadge from "./StatusBadge";

export function PendingActionBanner({ action }) {
  if (!action) return null;
  return (
    <section className={`pending-action ${action.blocked ? "pending-action-blocked" : ""}`} aria-labelledby="pending-action-title">
      <div>
        <p className="eyebrow">Next action</p>
        <h2 id="pending-action-title">{action.label || "Review the request"}</h2>
        <p className="small">{action.reason || "The next action is determined by the server."}</p>
        {action.blocked_reason && <p className="small muted">{action.blocked_reason}</p>}
      </div>
      <span className="small pending-action-owner">
        {action.required ? `Owner: ${humanize(action.owner)}` : "No action required"}
      </span>
    </section>
  );
}

export default function WorkflowTimeline({ events, title = "Workflow timeline" }) {
  const list = Array.isArray(events) ? events : [];
  return (
    <section className="card req-section" aria-labelledby="workflow-timeline-title">
      <div className="row timeline-heading">
        <h2 id="workflow-timeline-title">{title}</h2>
        <span className="small muted">{list.length} recorded {list.length === 1 ? "event" : "events"}</span>
      </div>
      {!list.length ? (
        <p className="muted small">No workflow events are available yet.</p>
      ) : (
        <ol className="workflow-timeline">
          {list.map((event) => (
            <li key={`${event.sequence}-${event.timestamp}`} className="workflow-timeline-item">
              <div className="workflow-timeline-marker" aria-hidden="true" />
              <div className="workflow-timeline-content">
                <div className="row timeline-meta">
                  <strong>{humanize(event.action)}</strong>
                  <StatusBadge status={event.status} kind="outcome" />
                  <time className="small muted" dateTime={event.timestamp}>{formatDateTime(event.timestamp)}</time>
                </div>
                <p className="small">{event.summary}</p>
                <p className="small muted">Recorded by {event.actor}</p>
                {event.evidence_refs?.length > 0 && (
                  <p className="small muted">Evidence: {event.evidence_refs.join(", ")}</p>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
