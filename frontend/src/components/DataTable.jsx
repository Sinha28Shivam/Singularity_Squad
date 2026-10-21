import { EmptyState, ErrorState, LoadingState } from "./States";

/**
 * DataTable({ columns: [{key, header, render?, align?}], rows, rowKey, onRowClick?, loading, error, onRetry, empty })
 * Rows are clickable with Enter/Space when onRowClick is set (keyboard accessible).
 */
export default function DataTable({ columns, rows, rowKey, onRowClick, loading, error, onRetry, empty, caption }) {
  if (loading && !rows?.length) return <LoadingState />;
  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (!rows || rows.length === 0) return empty || <EmptyState title="Nothing here yet" />;
  return (
    <div className="table-wrap">
      <table className="table">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>{columns.map((c) => <th key={c.key} scope="col" className={c.align === "right" ? "num" : undefined}>{c.header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const key = typeof rowKey === "function" ? rowKey(row) : row[rowKey] ?? i;
            const clickable = Boolean(onRowClick);
            return (
              <tr key={key} className={clickable ? "clickable" : undefined}
                tabIndex={clickable ? 0 : undefined}
                onClick={clickable ? () => onRowClick(row) : undefined}
                onKeyDown={clickable ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRowClick(row); } } : undefined}>
                {columns.map((c) => (
                  <td key={c.key} className={c.align === "right" ? "num" : undefined}>
                    {c.render ? c.render(row) : (row[c.key] ?? "—")}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
