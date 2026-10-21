import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { DECIDED_STATUSES, listApprovals } from "../api/approvals";
import DataTable from "../components/DataTable";
import PageHeader from "../components/PageHeader";
import { EmptyState, Notice } from "../components/States";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import { useAuth } from "../lib/auth";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import { useApi } from "../lib/useApi";
import "../styles/pages/approvals.css";

export const APPROVAL_SAFETY = "Approval is internal authorization only. It does not send a PO or move money.";
export const RISK_LABEL = { low: "Low", medium: "Medium", high: "High" };

/** "AMOUNT_OVER_THRESHOLD" -> "Amount over threshold" */
export function codeText(code) {
  return humanize(String(code ?? "").toLowerCase());
}

export function reasonText(item) {
  const codes = item?.reason_codes || [];
  return codes.length ? codes.map(codeText).join("; ") : "—";
}

/** Approval queue. mode "pending" = waiting for a decision; "history" = decided requests. Admins view only. */
export default function ApprovalsPage({ mode = "pending", readOnly = false }) {
  const { user } = useAuth();
  const navigate = useNavigate();
  const isHistory = mode === "history";
  // Admin view-only queue can browse every status (defaults to pending); approver queues are fixed by mode.
  const [filter, setFilter] = useState(readOnly && !isHistory ? "pending" : "");
  const showFilter = isHistory || readOnly;
  const base = readOnly || user?.role === "admin" ? "/admin/approvals" : "/approvals";

  const status = showFilter ? filter || undefined : "pending";
  const { data, error, loading, reload } = useApi(() => listApprovals({ status, limit: 200 }), [status]);

  const all = Array.isArray(data?.approvals) ? data.approvals : [];
  const rows = readOnly && !isHistory ? all
    : isHistory ? all.filter((a) => a?.status !== "pending") : all.filter((a) => a?.status === "pending");
  const counts = data?.counts || {};

  const columns = [
    { key: "ticket_id", header: "Request", render: (a) => (
      <div className="stack-sm" style={{ gap: 2 }}>
        <span className="mono">{a.ticket_id}</span>
        {a.summary && <span className="muted small">{a.summary}</span>}
      </div>
    ) },
    { key: "amount", header: "Amount", align: "right", render: (a) => formatMoney(a.amount, a.currency || "INR") },
    { key: "reasons", header: "Why review is needed", render: (a) => <span className="approval-cell-reasons">{reasonText(a)}</span> },
    { key: "risk", header: "Risk", render: (a) => RISK_LABEL[a.risk_level] || humanize(a.risk_level) },
    { key: "created_at", header: "Requested", render: (a) => formatDateTime(a.created_at) },
    { key: "status", header: "Status", render: (a) => <StatusBadge kind="approval" status={a.status} /> },
  ];

  const title = readOnly ? "Approvals" : isHistory ? "Approval history" : "Pending approvals";
  const description = readOnly
    ? "View only. Admins can review the approval queue but decisions require an eligible approver."
    : isHistory
      ? "Requests that have been decided, expired or replaced by a newer version."
      : "Requests waiting for your decision. Open one to review the details before deciding.";

  return (
    <div className="stack">
      <PageHeader title={title} description={description}
        actions={<button type="button" className="btn" onClick={reload} disabled={loading}>Refresh</button>} />
      <Notice tone="info">{APPROVAL_SAFETY}</Notice>

      {!error && data && (
        <div className="approval-stats" aria-label="Approval counts">
          {["pending", ...DECIDED_STATUSES].map((s) => (
            <div key={s} className="card stat">
              <span className="stat-value">{Number(counts[s] || 0)}</span>
              <span className="stat-label">{statusLabel(s, "approval")}</span>
            </div>
          ))}
        </div>
      )}

      {showFilter && (
        <div className="approval-filter">
          <div className="field">
            <label htmlFor="approval-status-filter">Status</label>
            <select id="approval-status-filter" className="select" value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="">{isHistory ? "All decided" : "All statuses"}</option>
              {(isHistory ? DECIDED_STATUSES : ["pending", ...DECIDED_STATUSES]).map((s) => (
                <option key={s} value={s}>{statusLabel(s, "approval")}</option>))}
            </select>
          </div>
        </div>
      )}

      <DataTable caption={title} columns={columns} rows={rows} rowKey="approval_id" loading={loading} error={error}
        onRetry={reload} onRowClick={(a) => navigate(`${base}/${encodeURIComponent(a.approval_id)}`)}
        empty={<EmptyState title={isHistory ? "No decided approvals"
          : readOnly && filter !== "pending" ? "No approval requests match this filter" : "Nothing waiting for approval"}
          description={isHistory ? "Decided requests will appear here." : "New approval requests will appear here."} />} />
    </div>
  );
}
