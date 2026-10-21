import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import { EmptyState } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDate, formatDateTime } from "../lib/format";
import { adminNextAction, listAdminTickets, TICKET_STATUSES } from "../api/requests";
import "../styles/pages/requests.css";

export default function RequestQueuePage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = TICKET_STATUSES.includes(params.get("status")) ? params.get("status") : "";
  const [query, setQuery] = useState("");
  const { data, error, loading, reload } = useApi(() => listAdminTickets({ status: status || undefined, limit: 100 }), [status]);
  const counts = data?.counts || {};

  const tickets = useMemo(() => ((data?.tickets) || []).filter((t) => t && t.ticket_id), [data]);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tickets;
    return tickets.filter((t) => [t.ticket_id, t.title, t.owner_name, t.owner_username].filter(Boolean).join(" ").toLowerCase().includes(q));
  }, [tickets, query]);

  function setStatus(s) {
    const next = new URLSearchParams(params);
    if (s) next.set("status", s); else next.delete("status");
    setParams(next, { replace: true });
  }

  const columns = [
    { key: "ticket_id", header: "Request ID", render: (t) => (
      <Link className="mono" to={`/admin/requests/${encodeURIComponent(t.ticket_id)}`} onClick={(e) => e.stopPropagation()}>{t.ticket_id}</Link>
    ) },
    { key: "owner", header: "Requester", render: (t) => t.owner_name || t.owner_username || "—" },
    { key: "title", header: "Summary", render: (t) => (
      <div><div>{t.title || "—"}</div>{t.event_date && <div className="small muted">Event {formatDate(t.event_date)}</div>}</div>
    ) },
    { key: "created_at", header: "Submitted", render: (t) => formatDateTime(t.created_at) },
    { key: "status", header: "Status", render: (t) => <StatusBadge status={t.status} /> },
    { key: "action", header: "Action", render: (t) => <span className="next-action">{adminNextAction(t.status)}</span> },
  ];

  const filtered = Boolean(status || query.trim());
  return (
    <div className="stack">
      <PageHeader title="All requests" description="Every request from every requester. Open one to run the checks or review results." />
      <div className="req-filters">
        <div className="field">
          <label htmlFor="queue-status">Status</label>
          <select id="queue-status" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {TICKET_STATUSES.map((s) => (
              <option key={s} value={s}>{statusLabel(s)}{counts[s] != null ? ` (${counts[s]})` : ""}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="queue-search">Search</label>
          <input id="queue-search" className="input" type="search" value={query} placeholder="ID, summary or requester"
            onChange={(e) => setQuery(e.target.value)} />
        </div>
        <button type="button" className="btn" onClick={reload} disabled={loading}>Refresh</button>
      </div>
      <DataTable caption="All requests" columns={columns} rows={rows} rowKey="ticket_id" loading={loading} error={error}
        onRetry={reload} onRowClick={(t) => navigate(`/admin/requests/${encodeURIComponent(t.ticket_id)}`)}
        empty={filtered
          ? <EmptyState title="No requests match" description="Try a different status or search." />
          : <EmptyState title="No requests yet" description="Requests appear here once requesters submit them." />} />
      {!loading && !error && tickets.length > 0 && (
        <p className="small muted">Showing {rows.length} of {tickets.length} loaded requests. Search applies to the loaded list.</p>
      )}
    </div>
  );
}
