/** Spend analytics (read-only; admin + approver). The backend computes every total. */
import { api } from "./client";

function range({ from, to } = {}) {
  return { from: from || undefined, to: to || undefined };
}

export const getAnalyticsSummary = (filters) => api.get("/api/analytics/summary", range(filters));

/** Resolves to a CSV Blob of approved tickets in the range. */
export const downloadAnalyticsCsv = (filters) => api.download("/api/analytics/export.csv", range(filters));
