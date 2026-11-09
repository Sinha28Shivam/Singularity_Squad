import { Link } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDateTime } from "../lib/format";
import { api } from "../api/client";

export const getInbox = () => api.get("/api/inbox");

const PRIORITY = { 1: ["High", "badge-error"], 2: ["Normal", "badge-warning"], 3: ["Low", "badge-neutral"] };

/** The signed-in user's pending actions. The list is derived by the server; every action is re-checked there. */
export default function InboxPage() {
  const inbox = useApi(() => getInbox(), []);
  const data = inbox.data;
  const items = Array.isArray(data?.items) ? data.items : [];
  return (
    <div className="stack">
      <PageHeader title="My inbox" description="What needs your attention next, most urgent first."
        actions={<button type="button" className="btn" onClick={inbox.reload} disabled={inbox.loading}>Refresh</button>} />
      {inbox.loading && !data ? <LoadingState label="Loading your inbox…" /> :
        inbox.error ? <ErrorState error={inbox.error} onRetry={inbox.reload} title="We couldn't load your inbox" /> :
          !items.length ? <EmptyState title="You're all caught up" description="Nothing is waiting for you right now." /> : (
            <section className="card stack-sm" aria-label="Pending actions">
              <p className="small muted">
                {data.total} pending {data.total === 1 ? "action" : "actions"}
                {data.stalled ? ` · ${data.stalled} waiting longer than ${data.stall_hours} hours` : ""}
              </p>
              <ul className="inbox-list" style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {items.map((i) => {
                  const [plabel, pclass] = PRIORITY[i.priority] || PRIORITY[2];
                  return (
                    <li key={`${i.kind}-${i.ticket_id}`} className="card-quiet row-between" data-testid="inbox-item"
                      style={{ gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
                      <div className="stack-sm" style={{ gap: 2 }}>
                        <div className="row" style={{ gap: 8 }}>
                          <strong>{i.label}</strong>
                          <span className={`badge ${pclass}`}>{plabel}</span>
                          {i.stalled && <span className="badge badge-error">Stalled</span>}
                        </div>
                        <span className="small">{i.detail}</span>
                        <span className="small muted">
                          <span className="mono">{i.ticket_id}</span>{i.title ? ` · ${i.title}` : ""}{i.since ? ` · waiting since ${formatDateTime(i.since)}` : ""}
                        </span>
                      </div>
                      <Link className="btn btn-sm" to={i.link}>Open</Link>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
    </div>
  );
}
