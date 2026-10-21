import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import VendorSourcingPage from "../VendorSourcingPage";
import { callsTo, renderWithApp } from "../../test/utils";
import { FORBIDDEN } from "./r3helpers";

const BANNER = "Decision support only — no supplier is contacted and nothing is awarded.";

const CAND = {
  candidate_id: "VCAND-ABC123", name: "Fresh Caterers", services: ["catering"], regions: ["Bengaluru"],
  contact_email: "ops@fresh.example", source: "manual", source_reference: "Referral from facilities team",
  notes: "", compliance_documents: [], status: "pending_review", created_by: "admin",
  created_at: "2026-10-20T10:00:00Z", updated_at: "2026-10-20T10:00:00Z",
  compliance_evidence: [{ document_type: "FSSAI licence", reference: "FSSAI-123", expires_on: "2027-01-01", verified: false }],
  status_history: [{ from_status: null, to_status: "pending_review", actor: "admin", reason: "Registered", at: "2026-10-20T10:00:00Z" }],
};
const SEARCH = {
  internal_vendors: [{ vendor_id: "VEN-001", name: "Catalog Caterers", approved: true, services: ["catering"], capacity: 200, rate_per_person: 450, currency: "INR", source: "internal_catalog" }],
  candidates: [CAND], candidates_total: 1, external_discovery_enabled: false,
  note: "Internal catalog and candidate registry searched first; no external search was run.",
};

describe("VendorSourcingPage — directory", () => {
  it("separates the read-only catalog from the candidate registry and shows the banner", async () => {
    const { fetchMock } = renderWithApp(<VendorSourcingPage />, {
      role: "admin", routes: [{ method: "GET", path: "/api/vendor-sourcing/vendors", body: SEARCH }],
    });
    expect(screen.getByRole("heading", { level: 1, name: "Vendor sourcing" })).toBeInTheDocument();
    expect(screen.getByText(BANNER)).toBeInTheDocument();
    expect(await screen.findByText("Catalog Caterers")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Internal catalog (read-only)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Candidate registry" })).toBeInTheDocument();
    expect(screen.getByText(/No external search is run/)).toBeInTheDocument();
    expect(screen.getByText("Fresh Caterers")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search"), "cater");
    await userEvent.type(screen.getByLabelText("Service"), "catering");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "approved");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(callsTo(fetchMock, "/api/vendor-sourcing/vendors", "GET").length).toBe(2));
    const url = callsTo(fetchMock, "/api/vendor-sourcing/vendors", "GET")[1].url;
    expect(url.searchParams.get("q")).toBe("cater");
    expect(url.searchParams.get("service")).toBe("catering");
    expect(url.searchParams.get("status")).toBe("approved");
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows an error with retry", async () => {
    let n = 0;
    renderWithApp(<VendorSourcingPage />, {
      role: "admin", routes: [{ method: "GET", path: "/api/vendor-sourcing/vendors",
        status: 503, body: () => { n += 1; return { detail: "Vendor registry temporarily unavailable." }; } }],
    });
    expect(await screen.findByText("Vendor registry temporarily unavailable.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(n).toBe(2));
  });

  it("shows empty states when nothing matches", async () => {
    renderWithApp(<VendorSourcingPage />, {
      role: "admin", routes: [{ method: "GET", path: "/api/vendor-sourcing/vendors", body: { internal_vendors: [], candidates: [] } }],
    });
    expect(await screen.findByText("No catalog vendors match")).toBeInTheDocument();
    expect(screen.getByText("No candidates match")).toBeInTheDocument();
  });
});

function candidateRoutes(extra = []) {
  return [
    ...extra,
    { method: "GET", path: "/api/vendor-sourcing/vendors", body: SEARCH },
    { method: "GET", path: "/api/vendor-sourcing/candidates", body: (url) => {
      const offset = Number(url.searchParams.get("offset") || 0);
      return offset === 0
        ? { items: [CAND], total: 21, limit: 20, offset: 0, next_offset: 20 }
        : { items: [{ ...CAND, candidate_id: "VCAND-ZZZ999", name: "Last Vendor" }], total: 21, limit: 20, offset: 20, next_offset: null };
    } },
    { method: "GET", path: "/api/vendor-sourcing/candidates/VCAND-ABC123", body: CAND },
    { method: "GET", path: "/api/vendor-sourcing/candidates/VCAND-ABC123/events",
      body: { candidate_id: CAND.candidate_id, events: [{ event_id: "VCAND-ABC123:1", event_type: "candidate_submitted", actor: "admin", created_at: "2026-10-20T10:00:00Z" }] } },
  ];
}

describe("VendorSourcingPage — candidates", () => {
  it("lists candidates with status filter and limit/offset pagination", async () => {
    const { fetchMock } = renderWithApp(<VendorSourcingPage />, { role: "admin", routes: candidateRoutes() });
    await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
    expect(await screen.findByText("Showing 1–1 of 21")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Last Vendor")).toBeInTheDocument();
    const last = callsTo(fetchMock, "/api/vendor-sourcing/candidates", "GET").at(-1).url;
    expect(last.searchParams.get("offset")).toBe("20");
    expect(last.searchParams.get("limit")).toBe("20");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "approved");
    await waitFor(() => {
      const u = callsTo(fetchMock, "/api/vendor-sourcing/candidates", "GET").at(-1).url;
      expect(u.searchParams.get("status")).toBe("approved");
      expect(u.searchParams.get("offset")).toBe("0");
    });
  });

  it("registers a candidate with compliance evidence rows", async () => {
    const created = { ...CAND, candidate_id: "VCAND-NEW001" };
    const { fetchMock } = renderWithApp(<VendorSourcingPage />, {
      role: "admin",
      routes: candidateRoutes([
        { method: "POST", path: "/api/vendor-sourcing/candidates", status: 201, body: created },
        { method: "GET", path: "/api/vendor-sourcing/candidates/VCAND-NEW001", body: created },
        { method: "GET", path: "/api/vendor-sourcing/candidates/VCAND-NEW001/events", body: { events: [] } },
      ]),
    });
    await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Register candidate" }));
    await userEvent.click(screen.getByRole("button", { name: "Register candidate" }));
    expect(screen.getByText("Enter a vendor name (at least 2 characters).")).toBeInTheDocument();
    expect(screen.getByText("Enter at least one service.")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/vendor-sourcing/candidates", "POST")).toHaveLength(0);

    await userEvent.type(screen.getByLabelText(/Vendor name/), "Fresh Caterers");
    await userEvent.type(screen.getByLabelText(/^Services/), "catering, snacks");
    await userEvent.type(screen.getByLabelText(/Source \/ provenance/), "Referral");
    await userEvent.click(screen.getByRole("button", { name: "Add evidence" }));
    await userEvent.type(screen.getByLabelText(/Document type 1/), "FSSAI licence");
    await userEvent.type(screen.getByLabelText(/Reference 1/), "FSSAI-123");
    await userEvent.click(screen.getByRole("button", { name: "Register candidate" }));
    expect(await screen.findByText(/VCAND-NEW001 registered as pending review/)).toBeInTheDocument();
    const body = callsTo(fetchMock, "/api/vendor-sourcing/candidates", "POST")[0].json;
    expect(body).toMatchObject({
      name: "Fresh Caterers", services: ["catering", "snacks"], source: "manual", source_reference: "Referral",
      compliance_evidence: [{ document_type: "FSSAI licence", reference: "FSSAI-123", verified: false }],
    });
  });

  it("renders a 409 duplicate and keeps the form values", async () => {
    renderWithApp(<VendorSourcingPage />, {
      role: "admin",
      routes: candidateRoutes([{ method: "POST", path: "/api/vendor-sourcing/candidates", status: 409,
        body: { detail: "A candidate with this name is already pending, approved, or suspended." } }]),
    });
    await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Register candidate" }));
    await userEvent.type(screen.getByLabelText(/Vendor name/), "Fresh Caterers");
    await userEvent.type(screen.getByLabelText(/^Services/), "catering");
    await userEvent.click(screen.getByRole("button", { name: "Register candidate" }));
    expect(await screen.findByText("Possible duplicate")).toBeInTheDocument();
    expect(screen.getByText(/already pending, approved, or suspended/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Vendor name/)).toHaveValue("Fresh Caterers");
  });

  it("shows candidate detail and approves with a required reason", async () => {
    const approved = { ...CAND, status: "approved", reviewed_by: "admin", review_reason: "Evidence checked" };
    const { fetchMock } = renderWithApp(<VendorSourcingPage />, {
      role: "admin",
      routes: candidateRoutes([{ method: "POST", path: "/api/vendor-sourcing/candidates/VCAND-ABC123/decision", body: approved }]),
    });
    await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
    await userEvent.click(await screen.findByText("Fresh Caterers"));
    const detail = (await screen.findByRole("heading", { name: "Fresh Caterers", level: 2 })).closest("section");
    expect(within(detail).getByText("Provenance")).toBeInTheDocument();
    expect(within(detail).getByText("Referral from facilities team")).toBeInTheDocument();
    expect(within(detail).getByText("FSSAI licence")).toBeInTheDocument();
    expect(within(detail).getByText("Not verified")).toBeInTheDocument();
    expect(within(detail).getByText("Status history")).toBeInTheDocument();
    expect(await within(detail).findByText("Candidate submitted")).toBeInTheDocument();
    expect(within(detail).getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(within(detail).getByRole("button", { name: "Suspend" })).toBeInTheDocument();

    await userEvent.click(within(detail).getByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Approve" });
    await userEvent.type(within(dialog).getByLabelText(/Reason for this decision/), "ok");
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Reason for this decision/), " checked");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(callsTo(fetchMock, "/api/vendor-sourcing/candidates/VCAND-ABC123/decision", "POST")[0].json)
      .toEqual({ decision: "approve", reason: "ok checked" });
    expect(await screen.findByText(/is now approved/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("keeps the decision dialog open and shows the server error", async () => {
    renderWithApp(<VendorSourcingPage />, {
      role: "admin",
      routes: candidateRoutes([{ method: "POST", path: "/api/vendor-sourcing/candidates/VCAND-ABC123/decision", status: 409,
        body: { detail: "Decision 'reject' is not allowed for candidate status 'rejected'." } }]),
    });
    await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
    await userEvent.click(await screen.findByText("Fresh Caterers"));
    const detail = (await screen.findByRole("heading", { name: "Fresh Caterers", level: 2 })).closest("section");
    await userEvent.click(within(detail).getByRole("button", { name: "Reject" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Missing licence");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(await within(dialog).findByText(/is not allowed/)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
