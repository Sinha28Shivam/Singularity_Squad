/** Approval queue and decisions (backend: app/routers/approvals.py). The server enforces role, self-approval,
 * expiry, version/hash and idempotency rules; this module only calls the API. */
import { api } from "./client";

const enc = encodeURIComponent;

export const DECIDED_STATUSES = ["approved", "rejected", "changes_requested", "expired", "superseded"];

/** GET /api/approvals -> {approvals: ApprovalListItem[], counts: {status: n}} */
export function listApprovals({ status, limit = 100 } = {}) {
  return api.get("/api/approvals", { status, limit });
}

/** GET /api/approvals/{id} -> ApprovalView (can_decide, cannot_decide_reason, stale_policy, review_snapshot, …) */
export function getApproval(approvalId) {
  return api.get(`/api/approvals/${enc(approvalId)}`);
}

/**
 * POST /api/approvals/{id}/decision with DecisionIn:
 * {action, comment, idempotency_key, request_version, workflow_snapshot_hash}
 * -> {approval, replayed, reconciliation_required, message}
 */
export function decideApproval(approvalId, body) {
  return api.post(`/api/approvals/${enc(approvalId)}/decision`, body);
}

/** GET /api/requests/{ticket_id}/evidence (privileged view for approvers/admins). 404 = no evidence yet. */
export function getTicketEvidence(ticketId) {
  return api.get(`/api/requests/${enc(ticketId)}/evidence`);
}
