/** Admin vendor invoice / delivery-note PDF intake. Extracted values are untrusted until a human verifies them. */
import { api } from "./client";

const BASE = "/api/admin/invoice-documents";
const enc = encodeURIComponent;
export const MAX_INVOICE_PDF_BYTES = 5 * 1024 * 1024;

export const listInvoiceDocuments = (status) => api.get(BASE, status ? { status } : undefined);
export const getInvoiceDocument = (docId) => api.get(`${BASE}/${enc(docId)}`);
export const verifyInvoiceDocument = (docId, body) => api.post(`${BASE}/${enc(docId)}/verify`, body);
export const rejectInvoiceDocument = (docId, reason) => api.post(`${BASE}/${enc(docId)}/reject`, { reason });

/** Client-side checks (the server re-checks MIME, %PDF- signature, size, encryption and page count). */
export function invoicePdfProblem(file) {
  if (!file) return "Choose a PDF file.";
  const isPdf = file.type === "application/pdf" || (!file.type && /\.pdf$/i.test(file.name || ""));
  if (!isPdf || !/\.pdf$/i.test(file.name || "")) return "Only PDF files are accepted.";
  if (!file.size) return "The file is empty.";
  if (file.size > MAX_INVOICE_PDF_BYTES) return "The PDF is larger than 5 MiB.";
  return "";
}

/** Multipart upload (field `file`). */
export function uploadInvoiceDocument(file) {
  const form = new FormData();
  form.append("file", file, file.name || "invoice.pdf");
  return api.postForm(BASE, form, undefined, { timeoutMs: 120000 });
}

/** Authenticated download of the stored original PDF. Returns a Blob. */
export const downloadInvoicePdf = (docId) =>
  api.get(`${BASE}/${enc(docId)}/pdf`, undefined, { headers: { Accept: "application/pdf" } });
