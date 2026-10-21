import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import { EmptyState } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatDate, formatDateTime } from "../lib/format";
import { categoryLabel, listMyTickets, TICKET_STATUSES, userNextAction } from "../api/requests";
import "../styles/pages/requests.css";

export default function MyRequestsPage() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApi(() => listMyTickets(50), []);
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");

  const tickets = useMemo(() => ((data && data.tickets) || []).filter((t) => t && t.ticket_id), [data]);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tickets.filter((t) => (!status || t.status === status) &&
      (!q || `${t.ticket_id} ${t.title || ""}`.toLowerCase().includes(q)));
  }, [tickets, status, query]);
  const present = useMemo(() => TICKET_STATUSES.filter((s) => tickets.some((t) => t.status === s)), [tickets]);

  const columns = [
    { key: "ticket_id", header: "Request ID", render: (t) => (
      <Link to={`/requests/${encodeURIComponent(t.ticket_id)}`} className="mono" onClick={(e) => e.stopPropagation()}>{t.ticket_id}</Link>
    ) },
    { key: "title", header: "Summary", render: (t) => (
      <div>
        <div>{t.title || "—"}</div>
        {t.event_category && (
          <div className="small muted">{categoryLabel(t.event_category)}{t.event_date ? ` · ${formatDate(t.event_date)}` : ""}</div>
        )}
      </div>
    ) },
    { key: "created_at", header: "Submitted", render: (t) => formatDateTime(t.created_at) },
    { key: "status", header: "Status", render: (t) => <StatusBadge status={t.status} /> },
    { key: "next", header: "Next action", render: (t) => <span className="next-action">{userNextAction(t.status)}</span> },
  ];

  const hasAny = tickets.length > 0;
  return (
    <div className="stack">
      <PageHeader title="My requests" description="Track the workplace event requests you've submitted."
        actions={<Link to="/requests/new" className="btn btn-primary"><Plus size={16} aria-hidden="true" />New request</Link>} />
      {hasAny && (
        <div className="req-filters">
          <div className="field">
            <label htmlFor="my-search">Search</label>
            <input id="my-search" className="input" type="search" value={query} placeholder="Request ID or summary"
              onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="my-status">Status</label>
            <select id="my-status" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">All statuses</option>
              {present.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
            </select>
          </div>
        </div>
      )}
      <DataTable
        caption="My requests"
        columns={columns}
        rows={rows}
        rowKey="ticket_id"
        loading={loading}
        error={error}
        onRetry={reload}
        onRowClick={(t) => navigate(`/requests/${encodeURIComponent(t.ticket_id)}`)}
        empty={hasAny
          ? <EmptyState title="No requests match" description="Try a different search or status." />
          : <EmptyState title="No requests yet" description="When you submit a request for a workplace event, it will appear here."
            action={<Link to="/requests/new" className="btn">Create a request</Link>} />}
      />
    </div>
  );
}
