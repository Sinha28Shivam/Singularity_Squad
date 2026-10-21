import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import { EmptyState } from "../components/States";
import ListFooter from "../components/TicketListFooter";
import { useTicketList } from "../lib/useTicketList";
import { formatDate, formatDateTime } from "../lib/format";
import { categoryLabel, listMyTickets, TICKET_QUERY_MAX, TICKET_STATUSES, userNextAction } from "../api/requests";
import "../styles/pages/requests.css";

const PAGE_SIZE = 50;
const matchesMine = (t, q) => `${t.ticket_id} ${t.title || ""}`.toLowerCase().includes(q);

export default function MyRequestsPage() {
  const navigate = useNavigate();
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const list = useTicketList(listMyTickets, { query, status, pageSize: PAGE_SIZE, matches: matchesMine });
  const { rows, loading, error, reload } = list;

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

  const filtered = Boolean(status || query.trim());
  const hasAny = list.loaded.length > 0 || filtered;
  return (
    <div className="stack">
      <PageHeader title="My requests" description="Track the workplace event requests you've submitted."
        actions={<Link to="/requests/new" className="btn btn-primary"><Plus size={16} aria-hidden="true" />New request</Link>} />
      {hasAny && (
        <div className="req-filters">
          <div className="field">
            <label htmlFor="my-search">Search</label>
            <input id="my-search" className="input" type="search" value={query} placeholder="Request ID or summary"
              maxLength={TICKET_QUERY_MAX} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="my-status">Status</label>
            <select id="my-status" className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">All statuses</option>
              {TICKET_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
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
        empty={filtered
          ? <EmptyState title="No requests match" description="Try a different search or status." />
          : <EmptyState title="No requests yet" description="When you submit a request for a workplace event, it will appear here."
            action={<Link to="/requests/new" className="btn">Create a request</Link>} />}
      />
      <ListFooter list={list} noun="requests" />
    </div>
  );
}
