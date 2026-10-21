import { useCallback, useEffect, useRef, useState } from "react";

/**
 * useApi(loader, deps) -> { data, error, loading, reload, setData }
 * Runs `loader()` (an async function returning data) on mount and when deps change.
 * Ignores results from stale calls.
 */
export function useApi(loader, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const seq = useRef(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps);

  const reload = useCallback(async () => {
    const id = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await run();
      if (id === seq.current) setState({ data, error: null, loading: false });
      return data;
    } catch (error) {
      if (id === seq.current) setState({ data: null, error, loading: false });
      return undefined;
    }
  }, [run]);

  useEffect(() => { reload(); }, [reload]);
  const setData = useCallback((data) => setState((s) => ({ ...s, data })), []);
  return { ...state, reload, setData };
}

/** useAction(fn) -> [run, {pending, error, clearError}] for buttons/forms. */
export function useAction(fn) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const run = useCallback(async (...args) => {
    setPending(true); setError(null);
    try { return await fn(...args); } catch (e) { setError(e); throw e; } finally { setPending(false); }
  }, [fn]);
  return [run, { pending, error, clearError: () => setError(null) }];
}
