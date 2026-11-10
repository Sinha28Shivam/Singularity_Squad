import { useCallback, useState } from "react";
import { Download } from "lucide-react";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import { EmptyState, ErrorState, InlineError, LoadingState, Notice } from "../components/States";
import { useApi } from "../lib/useApi";
import { formatMoney, humanize } from "../lib/format";
import { downloadAnalyticsCsv, getAnalyticsSummary } from "../api/analytics";
import { saveBlob } from "../api/client";
import "../styles/pages/analytics.css";

function formatPercent(rate) {
  if (rate === null || rate === undefined) return "—";
  return `${(Number(rate) * 100).toFixed(1)}%`;
}

function formatHours(h) {
  if (h === null || h === undefined) return "—";
  return h < 48 ? `${Number(h).toFixed(1)} h` : `${(Number(h) / 24).toFixed(1)} d`;
}

function Kpi({ id, label, value, hint }) {
  return (
    <div className="card stat analytics-kpi" data-testid={`kpi-${id}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {hint && <span className="small muted">{hint}</span>}
    </div>
  );
}

/** Horizontal bar chart built from plain HTML/CSS. Values are server-provided 2dp strings. */
function BarChart({ id, title, rows, labelOf, currency }) {
  const max = Math.max(0, ...rows.map((r) => Number(r.total) || 0));
  return (
    <section className="card analytics-chart" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="analytics-h2">{title}</h2>
      {rows.length === 0 ? (
        <p className="muted small">No approved spend in this range.</p>
      ) : (
        <ul className="bar-list" data-testid={`chart-${id}`}>
          {rows.map((r) => {
            const pct = max > 0 ? Math.max(2, ((Number(r.total) || 0) / max) * 100) : 0;
            const label = labelOf(r);
            return (
              <li key={label} className="bar-row">
                <span className="bar-label" title={label}>{label}</span>
                <span className="bar-track" role="img" aria-label={`${label}: ${formatMoney(r.total, currency)}`}>
                  <span className="bar-fill" style={{ width: `${pct}%` }} />
                </span>
                <span className="bar-value">
                  {formatMoney(r.total, currency)}
                  <span className="muted small"> · {r.count}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export default function AnalyticsPage() {
  const [draft, setDraft] = useState({ from: "", to: "" });
  const [filters, setFilters] = useState({ from: "", to: "" });
  const [rangeError, setRangeError] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(null);
  const [exported, setExported] = useState(false);

  const { data, error, loading, reload } = useApi(() => getAnalyticsSummary(filters), [filters.from, filters.to]);

  const apply = (e) => {
    e.preventDefault();
    if (draft.from && draft.to && draft.from > draft.to) {
      setRangeError("The start date must be on or before the end date.");
      return;
    }
    setRangeError("");
    setFilters({ ...draft });
  };
  const clear = () => {
    setDraft({ from: "", to: "" });
    setFilters({ from: "", to: "" });
    setRangeError("");
  };

  const exportCsv = useCallback(async () => {
    setExporting(true); setExportError(null); setExported(false);
    try {
      const blob = await downloadAnalyticsCsv(filters);
      const suffix = [filters.from, filters.to].filter(Boolean).join("_") || "all";
      saveBlob(blob, `spend-analytics-${suffix}.csv`);
      setExported(true);
    } catch (e) {
      setExportError(e);
    } finally {
      setExporting(false);
    }
  }, [filters]);

  const currency = data?.currency && data.currency !== "MIXED" ? data.currency : "INR";
  const statuses = Object.entries(data?.tickets_by_status || {});

  return (
    <div className="stack analytics-page">
      <PageHeader title="Spend analytics"
        description="Read-only view of draft-PO spend, invoice exceptions, policy blocks and simulated payments. Totals are computed by the server."
        actions={(
          <button type="button" className="btn" onClick={exportCsv} disabled={exporting}>
            <Download size={16} aria-hidden="true" />{exporting ? "Exporting…" : "Export CSV"}
          </button>
        )} />

      <form className="card analytics-filters" onSubmit={apply} aria-label="Date range">
        <div className="field">
          <label htmlFor="an-from">From</label>
          <input id="an-from" className="input" type="date" value={draft.from}
            onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))} />
        </div>
        <div className="field">
          <label htmlFor="an-to">To</label>
          <input id="an-to" className="input" type="date" value={draft.to}
            onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))} />
        </div>
        <div className="row analytics-filter-actions">
          <button type="submit" className="btn btn-primary">Apply</button>
          <button type="button" className="btn btn-ghost" onClick={clear}>Clear</button>
        </div>
        {rangeError && <p className="field-error" role="alert">{rangeError}</p>}
      </form>

      {exportError && <InlineError error={exportError} />}
      {exported && <Notice tone="success">CSV export downloaded.</Notice>}

      {loading && !data ? <LoadingState label="Loading analytics…" /> : error ? (
        <ErrorState error={error} onRetry={reload} title="Analytics aren't available" />
      ) : !data ? null : data.total_requests === 0 ? (
        <EmptyState title="No requests in this range" description="Try a wider date range." />
      ) : (
        <>
          <section aria-labelledby="an-kpis" className="stack-sm">
            <h2 id="an-kpis" className="sr-only">Key figures</h2>
            <div className="analytics-kpis">
              <Kpi id="requests" label="Requests" value={data.total_requests} />
              <Kpi id="approved" label="Approved" value={data.approved_count} />
              <Kpi id="spend" label="Draft-PO spend" value={formatMoney(data.draft_po_total, currency)}
                hint={data.currency === "MIXED" ? "Mixed currencies" : "Approved tickets"} />
              <Kpi id="exceptions" label="Invoice exception rate" value={formatPercent(data.invoice_exception_rate)}
                hint={data.invoices_reconciled ? `${data.invoice_exceptions ?? 0} of ${data.invoices_reconciled} invoices` : "No invoices reconciled"} />
              <Kpi id="payments" label="Simulated payments" value={data.simulated_payments?.count ?? 0}
                hint={`${formatMoney(data.simulated_payments?.total ?? "0", currency)} · no money moved`} />
            </div>
            <p className="muted small">
              Median cycle time — submit → approve: {formatHours(data.cycle_time_hours?.submit_to_approve_median)};
              {" "}approve → simulated payment: {formatHours(data.cycle_time_hours?.approve_to_payment_sim_median)}.
            </p>
            {statuses.length > 0 && (
              <ul className="analytics-statuses" aria-label="Requests by status">
                {statuses.map(([s, n]) => <li key={s}><span>{humanize(s)}</span> <strong>{n}</strong></li>)}
              </ul>
            )}
          </section>

          <div className="analytics-charts">
            <BarChart id="vendor" title="Spend by vendor" rows={data.spend_by_vendor || []}
              labelOf={(r) => r.vendor_name || r.vendor_id} currency={currency} />
            <BarChart id="category" title="Spend by category" rows={data.spend_by_category || []}
              labelOf={(r) => humanize(r.category)} currency={currency} />
            <BarChart id="month" title="Spend by month" rows={data.spend_by_month || []}
              labelOf={(r) => r.month} currency={currency} />
          </div>

          <div className="analytics-tables">
            <section className="stack-sm" aria-labelledby="an-budget">
              <h2 id="an-budget" className="analytics-h2">Budget vs actual</h2>
              <DataTable caption="Budget vs actual by category" rowKey="category" rows={data.budget_vs_actual || []}
                empty={<p className="muted small">No approved tickets in this range.</p>}
                columns={[
                  { key: "category", header: "Category", render: (r) => humanize(r.category) },
                  { key: "budget_total", header: "Budget", align: "right", render: (r) => formatMoney(r.budget_total, currency) },
                  { key: "po_total", header: "Draft PO", align: "right", render: (r) => formatMoney(r.po_total, currency) },
                  { key: "variance", header: "Remaining", align: "right", render: (r) => {
                    if (r.budget_total === null || r.budget_total === undefined) return "—";
                    const diff = Number(r.budget_total) - Number(r.po_total);
                    return <span className={diff < 0 ? "analytics-over" : undefined}>{formatMoney(diff.toFixed(2), currency)}</span>;
                  } },
                ]} />
            </section>
            <section className="stack-sm" aria-labelledby="an-blocks">
              <h2 id="an-blocks" className="analytics-h2">Top policy blocks</h2>
              <DataTable caption="Most frequent blocking reasons" rowKey="code" rows={data.top_policy_blocks || []}
                empty={<p className="muted small">No blocked requests in this range.</p>}
                columns={[
                  { key: "code", header: "Reason", render: (r) => <code>{r.code}</code> },
                  { key: "count", header: "Requests", align: "right" },
                ]} />
            </section>
          </div>
        </>
      )}
    </div>
  );
}
