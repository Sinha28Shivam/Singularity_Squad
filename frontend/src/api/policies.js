/** Policy library, policy rules and rate cards (backend: app/routers/policy_documents.py, policy_rules.py,
 * policy_search.py). Roles are enforced by the server. */
import { api } from "./client";

const enc = encodeURIComponent;

export const DOCUMENT_TYPES = ["company_policy", "vendor_policy", "vendor_contract", "privacy_policy",
  "security_policy", "rate_card", "procurement_guideline", "other"];
export const DOCUMENT_STATUSES = ["uploaded", "validating", "extracting", "needs_ocr", "indexing", "ready",
  "needs_review", "failed", "superseded", "archived"];
export const MAX_FILES_PER_UPLOAD = 20;
export const DEFAULT_MAX_FILE_MB = 20;

// ---------------------------------------------------------------- documents
/** GET /api/policy-documents -> {documents, counts} */
export function listDocuments({ status, document_type, limit = 200 } = {}) {
  return api.get("/api/policy-documents", { status, document_type, limit });
}
/** GET /api/policy-documents/{id} -> document metadata + events */
export function getDocument(documentId) {
  return api.get(`/api/policy-documents/${enc(documentId)}`);
}
/**
 * POST /api/policy-documents/upload (multipart): `files` (one part per PDF) + `metadata` (JSON list, one
 * PolicyDocumentMetadataIn object per file). -> {results: DocumentUploadResult[]}
 */
export function uploadDocuments(files, metadata) {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  form.append("metadata", JSON.stringify(metadata));
  return api.postForm("/api/policy-documents/upload", form, undefined, { timeoutMs: 300000 });
}
/** POST /api/policy-documents/{id}/reprocess -> {result, document} */
export function reprocessDocument(documentId) {
  return api.post(`/api/policy-documents/${enc(documentId)}/reprocess`, {}, { timeoutMs: 300000 });
}
/** POST /api/policy-documents/{id}/archive -> {document} */
export function archiveDocument(documentId) {
  return api.post(`/api/policy-documents/${enc(documentId)}/archive`, {});
}
/** POST /api/policy-documents/{id}/extract-rules -> {document_id, candidates, duplicates, warnings, extraction_model} */
export function extractRules(documentId) {
  return api.post(`/api/policy-documents/${enc(documentId)}/extract-rules`, {}, { timeoutMs: 300000 });
}

// ---------------------------------------------------------------- rules
/** GET /api/policy-rules -> {rules, counts, active_policy_set_version} */
export function listRules({ status, rule_id, limit = 500 } = {}) {
  return api.get("/api/policy-rules", { status, rule_id, limit });
}
/** GET /api/policy-rules/{id}/versions/{v} -> rule + condition_text + active + events */
export function getRule(ruleId, version) {
  return api.get(`/api/policy-rules/${enc(ruleId)}/versions/${enc(version)}`);
}
/** POST /api/policy-rules (PolicyRuleIn) -> created candidate (201) */
export function createRule(body) {
  return api.post("/api/policy-rules", body);
}
/** POST /api/policy-rules/{id}/validate {rule_version} -> {rule, errors, conflicts} */
export function validateRule(ruleId, ruleVersion) {
  return api.post(`/api/policy-rules/${enc(ruleId)}/validate`, { rule_version: ruleVersion });
}
/** POST /api/policy-rules/{id}/approve {rule_version, comment?, expected_policy_set_version?}
 * -> {rule, policy_set_version, replayed} */
export function approveRule(ruleId, body) {
  return api.post(`/api/policy-rules/${enc(ruleId)}/approve`, body);
}
/** POST /api/policy-rules/{id}/reject {rule_version, comment} -> rule */
export function rejectRule(ruleId, body) {
  return api.post(`/api/policy-rules/${enc(ruleId)}/reject`, body);
}
/** GET /api/policy-set/active -> {active, policy_set_version, rule_refs, created_at, created_by, reason, rules} */
export function getActivePolicySet() {
  return api.get("/api/policy-set/active");
}

// ---------------------------------------------------------------- rate cards
/** GET /api/rate-cards -> {rate_cards, count} */
export function listRateCards({ verification_status, vendor_key, limit = 500 } = {}) {
  return api.get("/api/rate-cards", { verification_status, vendor_key, limit });
}
/** POST /api/rate-cards/{rate_id}/verify {decision: verify|reject} -> updated entry */
export function verifyRateCard(rateId, decision) {
  return api.post(`/api/rate-cards/${enc(rateId)}/verify`, { decision });
}
