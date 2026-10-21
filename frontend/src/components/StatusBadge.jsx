/**
 * Status badge: always shows a text label (never color alone). Tone mapping is presentation only.
 * Plain-language labels for every status the backend emits; unknown values are humanized.
 */
const TICKET = {
  submitted: ["Submitted", "neutral"],
  processing: ["In progress", "info"],
  reviewed: ["Reviewed", "info"],
  needs_info: ["Needs more information", "warning"],
  pending_approval: ["Waiting for approval", "warning"],
  changes_requested: ["Changes requested", "warning"],
  approval_expired: ["Approval expired", "warning"],
  approved: ["Approved", "success"],
  rejected: ["Rejected", "error"],
  failed: ["Failed", "error"],
  persistence_error: ["Save failed", "error"],
};
const APPROVAL = {
  pending: ["Pending review", "warning"],
  approved: ["Approved", "success"],
  rejected: ["Rejected", "error"],
  changes_requested: ["Changes requested", "warning"],
  expired: ["Expired", "neutral"],
  superseded: ["Superseded", "neutral"],
  cancelled: ["Cancelled", "neutral"],
};
const OUTCOME = {
  pass: ["Passed", "success"],
  warning: ["Needs review", "warning"],
  needs_clarification: ["Needs more information", "warning"],
  blocked: ["Blocked", "error"],
  fail: ["Failed", "error"],
  match: ["Matched", "success"],
  mismatch: ["Mismatch", "error"],
  duplicate: ["Duplicate", "error"],
  uncertain: ["Uncertain", "warning"],
  warn: ["Warning", "warning"],
  info: ["Info", "neutral"],
};
const VENDOR = {
  pending_review: ["Pending review", "warning"],
  approved: ["Approved", "success"],
  rejected: ["Rejected", "error"],
  suspended: ["Suspended", "neutral"],
  open: ["Open", "info"],
  closed: ["Closed", "neutral"],
  cancelled: ["Cancelled", "neutral"],
  pending_verification: ["Unverified", "warning"],
  verified: ["Verified", "success"],
  superseded: ["Superseded", "neutral"],
  eligible: ["Eligible", "success"],
  review_required: ["Review required", "warning"],
  ineligible: ["Not eligible", "error"],
};
const POLICY = {
  draft: ["Draft", "neutral"],
  candidate: ["Candidate", "neutral"],
  validated: ["Validated", "info"],
  active: ["Active", "success"],
  ready: ["Ready", "success"],
  needs_review: ["Needs review", "warning"],
  processing: ["Processing", "info"],
  archived: ["Archived", "neutral"],
  failed: ["Failed", "error"],
  rejected: ["Rejected", "error"],
  approved: ["Approved", "success"],
  retired: ["Retired", "neutral"],
};
const PAYMENT = {
  submitted_to_simulator: ["Submitted to simulator", "info"],
  accepted_by_simulator: ["Accepted by simulator", "info"],
  processing_simulated: ["Simulated processing", "info"],
  failed_simulated: ["Simulation failed (retryable)", "warning"],
};
const MAPS = { ticket: TICKET, approval: APPROVAL, outcome: OUTCOME, vendor: VENDOR, policy: POLICY, payment: PAYMENT };

export function statusLabel(status, kind = "ticket") {
  const entry = (MAPS[kind] || {})[status];
  if (entry) return entry[0];
  if (!status) return "Unknown";
  const s = String(status).replace(/[_-]+/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export default function StatusBadge({ status, kind = "ticket", label }) {
  const entry = (MAPS[kind] || {})[status];
  const tone = entry ? entry[1] : "neutral";
  return <span className={`badge badge-${tone}`} data-status={status}>{label || statusLabel(status, kind)}</span>;
}
