import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDateTime } from "../lib/format";
import { adminNextAction, ATTENTION_STATUSES, listAdminTickets } from "../api/requests";
import "../styles/pages/requests.css";

/** Summary groups built only from server `counts` (GET /api/admin/tickets). */
export const COUNT_GROUPS = [
  { id: "needs_review", label: "Needs review", statuses: ["submitted", "reviewed"], hint: "New or reviewed, waiting for an admin" },
  { id: "in_progress", label: "In progress", statuses: ["processing", "needs_info", "pending_approval", "changes_requested"], hint: "Running, or waiting for the requester or an approver" },
  { id: "problems", label: "Expired or failed", statuses: ["approval_expired", "failed", "persistence_error"], hint: "Expired or failed — re-run needed" },
  { id: "completed", label: "Completed", statuses: ["approved", "rejected"], hint: "Approved or closed" },
];

export function groupCount(counts, statuses) {
  if (!counts || typeof counts !== "object") return null;
  return statuses.reduce((n, s) => n + (Number(counts[s]) || 0), 0);
}

export default function AdminOverviewPage() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApi(() => listAdminTickets({ limit: 100 }), []);
  const counts = data?.counts && typeof data.counts === "object" ? data.counts : null;
  const attention = useMemo(
    () => ((data?.tickets) || []).filter((t) => t && t.ticket_id && ATTENTION_STATUSES.has(t.status)).slice(0, 8),
    [data],
  );

  const columns = [
    { key: "ticket_id", header: "Request ID", render: (t) => <span className="mono">{t.ticket_id}</span> },
    { key: "title", header: "Summary", render: (t) => t.title || "—" },
    { key: "owner", header: "Requester", render: (t) => t.owner_name || t.owner_username || "—" },
    { key: "status", header: "Status", render: (t) => <StatusBadge status={t.status} /> },
    { key: "next", header: "Action", render: (t) => <span className="next-action">{adminNextAction(t.status)}</span> },
  ];

  return (
    <div className="stack">
      <PageHeader title="Overview" description="Requests that need operations work."
        actions={<Link className="btn btn-primary" to="/admin/requests">View all requests</Link>} />
      {loading && !data ? <LoadingState /> : error ? <ErrorState error={error} onRetry={reload} title="We couldn't load the overview" /> : (
        <>
          {counts ? (
            <section aria-labelledby="ov-counts" className="stack-sm">
              <h2 id="ov-counts" className="sr-only">Request counts</h2>
              <div className="req-stats">
                {COUNT_GROUPS.map((g) => (
                  <div className="card stat" key={g.id} data-testid={`count-${g.id}`}>
                    <span className="stat-value">{groupCount(counts, g.statuses)}</span>
                    <span className="stat-label">{g.label}</span>
                    <span className="small muted">{g.hint}</span>
                  </div>
                ))}
              </div>
            </section>
          ) : (
            <p className="muted small">Request counts aren't available right now.</p>
          )}
          <section aria-labelledby="ov-attention" className="stack-sm">
            <div className="row-between">
              <h2 id="ov-attention">Needs attention</h2>
              <Link to="/admin/requests" className="small">All requests</Link>
            </div>
            <DataTable caption="Requests needing attention" columns={columns} rows={attention} rowKey="ticket_id"
              onRowClick={(t) => navigate(`/admin/requests/${encodeURIComponent(t.ticket_id)}`)}
              empty={<EmptyState title="Nothing needs attention" description="New, reviewed, expired or failed requests will appear here." />} />
          </section>
        </>
      )}
    </div>
  );
}
