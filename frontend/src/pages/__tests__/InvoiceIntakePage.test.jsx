import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import InvoiceIntakePage from "../InvoiceIntakePage";
import { callsTo, renderWithApp } from "../../test/utils";
import { navForRole, pageTitle } from "../../routes";

const BASE = "/api/admin/invoice-documents";
const EXTRACTED = {
  invoice_id: "INV-U7001", vendor_id: "VEN-002", po_id: "DRAFT-PO-1001", invoice_date: "2026-10-24",
  delivery_id: "DEL-U7001", currency: "INR", subtotal: 72500, tax: 3625, total: 76125,
  line_items: [
    { description: "Lunch", quantity: 100, unit_price: 700, line_total: 70000 },
    { description: "Delivery & setup", quantity: 1, unit_price: 2500, line_total: 2500 },
  ],
};
const DOC = {
  doc_id: "INVDOC-AAAAAAAAAAAA", status: "pending_review", filename: "invoice.pdf", extracted: EXTRACTED,
  extracted_delivery: { delivery_id: "DEL-U7001", delivered_on: "2026-10-23", items: { Lunch: 100, "Delivery & setup": 1 }, signed_by: "Facilities Desk" },
  warnings: ["missing:tax_id"], injection_flags: ["ignore all previous"], extraction: { method: "regex", untrusted: true },
  extracted_text: "Invoice No: INV-U7001", extracted_text_untrusted: true,
};
const VERIFIED = { ...DOC, doc_id: "INVDOC-BBBBBBBBBBBB", filename: "old.pdf", status: "verified", warnings: [], injection_flags: [], verified: { ...EXTRACTED, invoice_id: "INV-OLD1" } };

function routes(extra = []) {
  return [
    { method: "GET", path: BASE, body: { documents: [DOC, VERIFIED] } },
    { method: "GET", path: `${BASE}/${DOC.doc_id}`, body: DOC },
    ...extra,
  ];
}

const render = (extra) => renderWithApp(<InvoiceIntakePage />, { role: "admin", route: "/admin/invoices", routes: routes(extra) });

describe("InvoiceIntakePage", () => {
  it("lists documents with status badges and is in the admin nav", async () => {
    render();
    const table = await screen.findByRole("table", { name: "Invoice documents" });
    expect(within(table).getByText("Pending review")).toBeInTheDocument();
    expect(within(table).getByText("Verified")).toBeInTheDocument();
    expect(within(table).getByText("INV-OLD1")).toBeInTheDocument();
    expect(within(table).getByText("Injection signal")).toBeInTheDocument();
    expect(navForRole("admin").find((s) => s.title === "Operations").items.map((i) => i.to)).toContain("/admin/invoices");
    expect(navForRole("user").flatMap((s) => s.items.map((i) => i.to))).not.toContain("/admin/invoices");
    expect(pageTitle("/admin/invoices")).toBe("Invoice intake");
  });

  it("uploads a PDF as multipart and rejects non-PDF files client side", async () => {
    const { fetchMock } = render([{ method: "POST", path: BASE, status: 201, body: DOC }]);
    const input = await screen.findByLabelText("Invoice PDF");
    fireEvent.change(input, { target: { files: [new File(["hello"], "notes.txt", { type: "text/plain" })] } });
    expect(await screen.findByText("Only PDF files are accepted.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Upload invoice" }));
    expect(callsTo(fetchMock, BASE, "POST")).toHaveLength(0);

    const file = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d])], "invoice.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [file] } });
    await userEvent.click(screen.getByRole("button", { name: "Upload invoice" }));
    await waitFor(() => expect(callsTo(fetchMock, BASE, "POST")).toHaveLength(1));
    const [call] = callsTo(fetchMock, BASE, "POST");
    expect(call.init.body).toBeInstanceOf(FormData);
    expect(call.init.body.get("file").name).toBe("invoice.pdf");
    expect(await screen.findByRole("heading", { name: /Review invoice.pdf/ })).toBeInTheDocument();
  });

  it("pre-fills the verify form, shows warnings and injection flags, and blocks bad arithmetic", async () => {
    const { fetchMock } = render([{ method: "POST", path: `${BASE}/${DOC.doc_id}/verify`, body: { ...DOC, status: "verified", verified: EXTRACTED, warnings: [], injection_flags: [] } }]);
    const table = await screen.findByRole("table", { name: "Invoice documents" });
    await userEvent.click(within(table).getByRole("button", { name: "Review" }));
    expect(await screen.findByLabelText("Invoice ID")).toHaveValue("INV-U7001");
    expect(screen.getByLabelText("PO ID")).toHaveValue("DRAFT-PO-1001");
    expect(screen.getByLabelText("Line 1 line total")).toHaveValue("70000");
    expect(screen.getByText(/Possible prompt-injection text/)).toBeInTheDocument();
    expect(within(screen.getByTestId("extraction-warnings")).getByText("missing:tax_id")).toBeInTheDocument();

    const total = screen.getByLabelText("Total");
    await userEvent.clear(total);
    await userEvent.type(total, "80000");
    expect(screen.getByText(/Total should be/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify invoice" })).toBeDisabled();
    await userEvent.clear(total);
    await userEvent.type(total, "76125");
    await userEvent.click(screen.getByRole("button", { name: "Verify invoice" }));
    await waitFor(() => expect(callsTo(fetchMock, `${BASE}/${DOC.doc_id}/verify`, "POST")).toHaveLength(1));
    const body = callsTo(fetchMock, `${BASE}/${DOC.doc_id}/verify`, "POST")[0].json;
    expect(body).toMatchObject({ invoice_id: "INV-U7001", total: 76125, subtotal: 72500, currency: "INR" });
    expect(body.line_items[0]).toEqual({ description: "Lunch", quantity: 100, unit_price: 700, line_total: 70000 });
    expect(body.delivery).toEqual({ delivery_id: "DEL-U7001", delivered_on: "2026-10-23", signed_by: "Facilities Desk", items: { Lunch: 100, "Delivery & setup": 1 } });
    expect(await screen.findByText(/is available for invoice matching/)).toBeInTheDocument();
  });

  it("rejects with a reason", async () => {
    const { fetchMock } = render([{ method: "POST", path: `${BASE}/${DOC.doc_id}/reject`, body: { ...DOC, status: "rejected", rejected_reason: "Wrong vendor" } }]);
    const table = await screen.findByRole("table", { name: "Invoice documents" });
    await userEvent.click(within(table).getByRole("button", { name: "Review" }));
    const reject = await screen.findByRole("button", { name: "Reject document" });
    expect(reject).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Rejection reason/), "Wrong vendor");
    await userEvent.click(reject);
    await waitFor(() => expect(callsTo(fetchMock, `${BASE}/${DOC.doc_id}/reject`, "POST")[0]?.json).toEqual({ reason: "Wrong vendor" }));
    expect(await screen.findByText("Wrong vendor")).toBeInTheDocument();
  });
});
