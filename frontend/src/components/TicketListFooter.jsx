import { InlineError } from "./States";

/** "Showing N of total" + "Load more" for a useTicketList() result. */
export default function TicketListFooter({ list, noun = "requests" }) {
  if (list.loading && !list.rows.length) return null;
  if (list.error) return null;
  const shown = list.rows.length;
  return (
    <div className="stack-sm">
      {list.moreError && <InlineError error={list.moreError} />}
      <div className="row-between">
        <p className="small muted" role="status" aria-live="polite">
          {list.total !== null
            ? `Showing ${shown} of ${list.total} ${noun}.`
            : shown ? `Showing ${shown} of ${list.loaded.length} loaded ${noun}. Search applies to the loaded list.` : ""}
        </p>
        {list.hasMore && (
          <button type="button" className="btn" onClick={list.loadMore} disabled={list.morePending}>
            {list.morePending ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    </div>
  );
}
