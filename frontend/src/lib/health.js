import { useEffect, useState } from "react";
import { api } from "../api/client";

let cached = null;
/** GET /api/health once per session (feature flags such as payments_simulation_enabled). */
export function useHealth() {
  const [health, setHealth] = useState(cached);
  useEffect(() => {
    if (cached) return;
    api.get("/api/health", undefined, { auth: false }).then((h) => { cached = h; setHealth(h); }).catch(() => setHealth(null));
  }, []);
  return health;
}
export function _resetHealthCache() { cached = null; }
