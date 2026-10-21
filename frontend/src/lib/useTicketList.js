import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "./useApi";
import { supportsServerSearch, TICKET_QUERY_MAX } from "../api/requests";

/**
 * Server-side ticket search + "Load more" paging for /api/my/tickets and /api/admin/tickets.
 *
 * fetchPage({q, status, limit, offset}) -> {tickets, total, next_offset, counts?}.
 * `query` is debounced before it is sent. If the server response has no `total` (an older API without
 * q/offset support), the hook switches to legacy mode: q is no longer sent and the loaded list is filtered
 * client-side with `matches(ticket, query)` and by status.
 */
export function useTicketList(fetchPage, { query = "", status = "", pageSize = 50, matches, debounceMs = 300 } = {}) {
  const [debounced, setDebounced] = useState(() => query.trim().slice(0, TICKET_QUERY_MAX));
  useEffect(() => {
    const next = query.trim().slice(0, TICKET_QUERY_MAX);
    const t = setTimeout(() => setDebounced(next), debounceMs);
    return () => clearTimeout(t);
  }, [query, debounceMs]);

  const [legacy, setLegacy] = useState(false);
  const q = legacy ? "" : debounced;
  const first = useApi(() => fetchPage({ q: q || undefined, status: status || undefined, limit: pageSize }),
    [fetchPage, q, status, pageSize]);

  useEffect(() => {
    if (first.data) setLegacy(!supportsServerSearch(first.data));
  }, [first.data]);

  const [more, setMore] = useState({ items: [], nextOffset: undefined, pending: false, error: null });
  const baseRef = useRef(null);
  useEffect(() => {
    baseRef.current = first.data;
    setMore({ items: [], nextOffset: undefined, pending: false, error: null });
  }, [first.data]);

  const nextOffset = more.nextOffset !== undefined ? more.nextOffset : first.data?.next_offset ?? null;
  const hasMore = !legacy && nextOffset !== null && nextOffset !== undefined;

  const loadMore = useCallback(async () => {
    if (!hasMore || more.pending) return;
    const base = baseRef.current;
    setMore((m) => ({ ...m, pending: true, error: null }));
    try {
      const res = await fetchPage({ q: q || undefined, status: status || undefined, limit: pageSize, offset: nextOffset });
      if (baseRef.current !== base) return;
      const items = Array.isArray(res?.tickets) ? res.tickets : [];
      setMore((m) => ({ items: [...m.items, ...items], nextOffset: res?.next_offset ?? null, pending: false, error: null }));
    } catch (error) {
      if (baseRef.current === base) setMore((m) => ({ ...m, pending: false, error }));
    }
  }, [fetchPage, hasMore, more.pending, nextOffset, pageSize, q, status]);

  const loaded = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const t of [...((first.data?.tickets) || []), ...more.items]) {
      if (!t || !t.ticket_id || seen.has(t.ticket_id)) continue;
      seen.add(t.ticket_id);
      out.push(t);
    }
    return out;
  }, [first.data, more.items]);

  const rows = useMemo(() => {
    if (!legacy) return loaded;
    const text = query.trim().toLowerCase();
    return loaded.filter((t) => (!status || t.status === status) && (!text || (matches ? matches(t, text) : true)));
  }, [legacy, loaded, query, status, matches]);

  return {
    rows,
    loaded,
    total: legacy ? null : first.data?.total ?? null,
    counts: first.data?.counts || {},
    legacy,
    searching: !legacy && query.trim().slice(0, TICKET_QUERY_MAX) !== debounced,
    loading: first.loading,
    error: first.error,
    reload: first.reload,
    hasMore,
    loadMore,
    morePending: more.pending,
    moreError: more.error,
  };
}
