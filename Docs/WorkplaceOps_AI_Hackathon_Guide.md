# WorkplaceOps AI --- Hackathon Proposal & Implementation Guide

## 1. Executive summary

**WorkplaceOps AI** is a multi-agent workplace operations copilot that
coordinates an internal request from intake through policy validation,
vendor planning, procurement preparation, and invoice verification.

The project targets a common operational problem: requests, policies,
vendor information, purchase orders, delivery records, and invoices are
often spread across different documents and tools. Staff must manually
cross-check them, which can cause delays, policy violations, duplicate
payments, and budget overruns.

The hackathon prototype will demonstrate one end-to-end workflow using
synthetic data. It will produce a traceable recommendation, a draft
purchase order (PO), an invoice-reconciliation report, and an audit
trail. It will **not** make real purchases, issue real POs, approve
payments, or connect to a company's production systems.

## 2. Why this use case?

The selected use case is **Use Case 6: GWS Workplace Operations Copilot
--- Request-to-Payment in One Flow**.

It was selected because it demonstrates business value and agentic
behavior in a single coherent story. It also gives judges concrete
evidence to evaluate: policy citations, vendor selection, budget
calculations, PO details, invoice matching, exception handling, and a
human-approval boundary.

### Ranking of the six candidate topics

  ------------------------------------------------------------------------
                          Rank Use case              Assessment
  ---------------------------- --------------------- ---------------------
                             1 UC6 --- GWS Workplace Strong business
                               Operations Copilot    impact and a
                                                     compelling end-to-end
                                                     workflow; scope must
                                                     be controlled
                                                     carefully.

                             2 UC5 --- LegalTech     Lower-risk fallback;
                               Contract Compliance   well suited to
                               Drift & Redline       document processing
                               Drafter               and RAG.

                             3 UC2 --- QBR Pack      Useful business
                               Builder & Customer    reporting use case
                               Q&A Coach             with a clear demo.

                             4 UC3 --- AML Fraud     Potentially
                               Ring Investigation    impactful, but
                               Assistant             investigation logic
                                                     and evaluation can be
                                                     complex.

                             5 UC1 --- B2B           Good procurement
                               Sourcing/RFQ          story, but
                               Negotiation Copilot   negotiation
                                                     simulation can expand
                                                     scope.

                             6 UC4 --- AI            Possible bias and
                               First-Round Screening fairness concerns;
                               Interviewer           harder to validate
                                                     responsibly in a
                                                     short hackathon.
  ------------------------------------------------------------------------

**Fallback rule:** Continue with UC6 if the team can keep the flow
simulated and focused. If integration and workflow orchestration are
still blocking progress around hour 16, switch to UC5 rather than
delivering a fragile demo.

## 3. Problem statement

Workplace operations teams handle requests such as employee events,
catering, facilities, and vendor services. To complete a request,
someone may need to:

1.  Extract requirements from a request.
2.  Identify the applicable policy and approval limits.
3.  Find eligible vendors and available dates.
4.  Calculate costs and check the budget.
5.  Prepare procurement paperwork.
6.  Compare an invoice with the PO, agreed rates, and evidence of
    delivery.
7.  Record the decisions and exceptions for audit.

When these steps are handled manually across separate tools, errors and
delays can occur. WorkplaceOps AI brings these checks into one auditable
workflow.

## 4. Demo scenario

Use a fully synthetic scenario such as:

> "Organize lunch for 100 employees next Friday. Keep the total under
> ₹1,00,000, use an approved vendor, and follow the workplace event
> policy."

The system should extract the request, check policy requirements,
compare available vendors, calculate the estimated cost, identify any
conflict, and prepare a **draft** PO. Then, in the second part of the
demo, it should compare a sample invoice against the draft PO and a
delivery record.

The exact dates, vendor names, prices, policies, invoices, and delivery
records in the demo should be clearly labelled as synthetic examples.

## 5. Agent design

### 5.1 Manager / Orchestrator

**Responsibility** - Own the overall request state. - Delegate work to
specialist agents. - Track which checks have passed or failed. - Re-plan
when a specialist reports a conflict. - Produce the final workflow
summary.

**Tools / actions** - Read and update request state. - Call specialist
agents. - Track workflow status and outstanding approvals.

### 5.2 Intake & Triage Agent

**Responsibility** - Parse the user's request. - Extract category, date,
headcount, budget, urgency, location, and requirements. - Ask for
missing critical information rather than inventing it.

**Output example** - Request category: employee event - Attendees: 100 -
Date: next Friday - Budget ceiling: ₹1,00,000 - Service: lunch - Vendor
requirement: approved vendor only

### 5.3 Policy Advisor Agent

**Responsibility** - Retrieve relevant clauses from a synthetic
workplace handbook or event policy. - Check eligibility, spending caps,
lead times, and approval requirements. - Return the applicable clauses
and citations.

**Implementation** - Use retrieval-augmented generation (RAG) over a
small set of policy documents. - Require citations for policy
conclusions. - If no applicable clause is found, mark the check as
uncertain and request human review rather than fabricating a policy.

### 5.4 Planner & Vendor Matcher Agent

**Responsibility** - Compare approved vendors. - Check availability and
constraints from synthetic vendor/calendar data. - Build a feasible plan
and explain why the chosen vendor fits.

**Implementation** - Use structured vendor records with service types,
rates, approval status, capacity, and availability. - Use deterministic
filtering and ranking where possible. - If the first choice is
unavailable, select the next eligible option or report that no feasible
option exists.

### 5.5 Procurement Agent

**Responsibility** - Calculate the estimated cost. - Check the budget
and applicable approval threshold. - Create a draft PO with line items,
quantities, rates, and total.

**Important** - Arithmetic must be performed in code, not left to an
LLM. - The prototype creates a **draft only**. No real purchase or
approval is executed.

### 5.6 Billing Guardian Agent

**Responsibility** - Compare a sample invoice with the draft PO, rate
card, and delivery record. - Detect amount or quantity mismatches and
possible duplicate invoices. - Report exceptions and evidence.

**Example checks** - Vendor and PO identifiers match. - Invoice
line-item quantities and rates match the PO. - Invoice total matches
deterministic calculations. - Delivery evidence supports the billed
quantities. - Invoice identifier has not appeared previously in the
synthetic invoice dataset.

### 5.7 Critic / Final Review Agent

**Responsibility** - Conduct a read-only final review. - Check for
unresolved policy issues, budget overruns, date conflicts, missing
citations, and invoice mismatches. - Return a clear pass, warning, or
blocked status with reasons.

The critic should not silently modify the underlying records. It should
report findings so the orchestrator can request a correction or human
decision.

## 6. End-to-end workflow

1.  **Intake:** Receive the request and extract structured requirements.
2.  **Clarification:** Ask for missing mandatory fields, if any.
3.  **Policy check:** Retrieve relevant policy clauses and record
    citations.
4.  **Planning:** Search synthetic vendor, rate-card, and availability
    data.
5.  **Costing:** Calculate estimated line items and total in code.
6.  **Budget and approval check:** Compare the total with the request
    budget and policy thresholds.
7.  **Draft PO:** Create a draft purchase order only when required
    checks pass.
8.  **Invoice reconciliation:** Compare a sample invoice with the PO,
    rate card, and delivery record.
9.  **Critic review:** Identify unresolved conflicts and
    inconsistencies.
10. **Audit report:** Show the decision, evidence, tool calls,
    exceptions, and whether human approval is required.

### High-level architecture

``` text
User Request
    |
    v
Manager / Orchestrator
    |
    +--> Intake & Triage
    |
    +--> Policy Advisor ----> Policy documents / RAG
    |
    +--> Planner & Vendor Matcher ----> Vendor, rate-card, availability data
    |
    +--> Procurement ----> Deterministic cost engine ----> Draft PO
    |
    +--> Billing Guardian <---- Invoice + PO + delivery record
    |
    +--> Critic / Final Review
    |
    v
Traceable Result + Audit Trail + Human Approval Gate
```

## 7. Recommended technology stack

The team already has access to Python, Docker, VS Code, Jenkins, API
keys, and Azure AI Foundry. For a 24-hour hackathon, prioritize tools
that reduce setup time.

  -----------------------------------------------------------------------
  Layer                   Recommended choice      Purpose
  ----------------------- ----------------------- -----------------------
  Backend API             Python + FastAPI        Request endpoints and
                                                  workflow coordination

  LLM                     A deployment available  Extraction,
                          in Azure AI Foundry     summarization, policy
                                                  explanation, and agent
                                                  decisions

  RAG                     Azure AI Search if      Retrieve policy clauses
                          already provisioned;    with citations
                          otherwise a lightweight 
                          local vector index      

  PDF processing          PyMuPDF or `pypdf`      Extract policy and
                                                  invoice text from PDFs

  Structured data         JSON files or SQLite    Synthetic vendors, rate
                                                  cards, POs, invoices,
                                                  and delivery records

  Validation and          Python functions +      Deterministic
  calculations            Pydantic                calculations and
                                                  structured schemas

  Frontend                Simple HTML/JavaScript  Request entry, workflow
                          or Streamlit            status, evidence, and
                                                  reports

  Packaging               Docker                  Reproducible local demo

  Source control          Git                     Collaboration and
                                                  version history

  CI                      Jenkins only if already Optional test/build
                          configured              automation; do not
                                                  spend hackathon time
                                                  setting it up from
                                                  scratch
  -----------------------------------------------------------------------

### Model guidance

Use an Azure AI Foundry model deployment that is already available to
the team and supports the required structured-output or tool-calling
workflow. Choose based on availability, latency, cost, and reliability
rather than assuming a particular model name will be enabled.

Keep the model responsible for language tasks and decision explanations.
Keep totals, thresholds, duplicate detection, and field comparisons in
deterministic code.

## 8. Scope for a 24-hour build

### Must-have

-   One request type: employee event with catering.
-   Synthetic policy documents and vendor/rate-card data.
-   Structured request extraction.
-   Policy retrieval with citations.
-   Vendor selection and availability check.
-   Deterministic budget calculation.
-   Draft PO generation.
-   One invoice comparison against the PO and delivery record.
-   Critic/final review.
-   Visible workflow steps, evidence, and audit log.
-   One planted conflict that the system can detect and recover from or
    escalate.

### Nice-to-have --- only after the core works

-   Multiple request categories.
-   More advanced agent memory.
-   Real calendar or procurement integrations.
-   User authentication and role-based permissions.
-   Dashboard analytics.
-   Export to PDF or Excel.
-   Automated CI/CD.

### Explicitly out of scope

-   Real procurement transactions.
-   Sending POs to suppliers.
-   Approving or issuing payments.
-   Connecting to production company systems without authorization.
-   Treating an LLM-generated answer as proof that a policy or invoice
    check passed.

## 9. Suggested 24-hour timeline

  -----------------------------------------------------------------------
  Time                                Deliverable
  ----------------------------------- -----------------------------------
  Hours 0--2                          Agree on demo story, data schema,
                                      API contract, and success criteria.

  Hours 2--5                          Create synthetic policy, vendor,
                                      rate-card, PO, invoice, and
                                      delivery data.

  Hours 5--9                          Build FastAPI endpoints, request
                                      schema, and deterministic
                                      cost/validation functions.

  Hours 9--13                         Add policy RAG, citations, and
                                      vendor matching.

  Hours 13--16                        Implement orchestration, draft PO,
                                      invoice reconciliation, and critic
                                      review.

  Hours 16--19                        Build the UI with workflow state,
                                      evidence, exceptions, and audit
                                      history.

  Hours 19--21                        Add a planted conflict and verify
                                      recovery/escalation.

  Hours 21--23                        Run test cases, fix high-impact
                                      bugs, and prepare demo data.

  Hours 23--24                        Rehearse a short demo and prepare a
                                      concise explanation of impact and
                                      limitations.
  -----------------------------------------------------------------------

**Scope checkpoint at hour 16:** If the end-to-end path is not working,
remove optional features and simplify the workflow. If the system
remains unreliable, switch to the lower-risk UC5 contract compliance use
case.

## 10. Team allocation for 3--4 people

### Person 1 --- Orchestration and API

-   FastAPI endpoints and request lifecycle.
-   Orchestrator and workflow state.
-   Integration between modules.

### Person 2 --- RAG and policy agent

-   PDF/text ingestion.
-   Policy retrieval and citations.
-   Policy-check tests and evidence display.

### Person 3 --- Procurement and billing logic

-   Vendor/rate-card schemas.
-   Deterministic cost calculations.
-   Draft PO, invoice matching, duplicate detection, and test data.

### Person 4 --- UI, testing, and demo (if available)

-   Frontend workflow visualization.
-   Audit log and exception display.
-   End-to-end tests, demo script, and presentation.

For a three-person team, combine UI/demo work with orchestration or RAG
depending on individual strengths. Define shared schemas early so
modules integrate cleanly.

## 11. Data contracts

Use JSON/Pydantic schemas for consistent communication between agents
and the backend.

### Request

``` json
{
  "request_id": "REQ-1001",
  "category": "employee_event",
  "date": "2026-10-23",
  "attendees": 100,
  "budget_limit": 100000,
  "currency": "INR",
  "requirements": ["lunch", "approved_vendor_only"]
}
```

### Vendor record

``` json
{
  "vendor_id": "VEN-001",
  "name": "Example Catering Co.",
  "approved": true,
  "service": "lunch",
  "rate_per_person": 650,
  "capacity": 150,
  "available_dates": ["2026-10-23"]
}
```

### Draft purchase order

``` json
{
  "po_id": "DRAFT-PO-1001",
  "request_id": "REQ-1001",
  "vendor_id": "VEN-001",
  "status": "draft",
  "line_items": [
    {
      "description": "Lunch",
      "quantity": 100,
      "unit_price": 650,
      "line_total": 65000
    }
  ],
  "total": 65000,
  "requires_human_approval": true
}
```

These are illustrative schemas only. All identifiers, names, rates, and
dates should be treated as synthetic demo data.

## 12. Demo story and planted conflict

### Happy path

1.  Enter the event request.
2.  Show extracted fields and any clarification request.
3.  Display policy clauses and citations.
4.  Show eligible vendors and why the selected vendor fits.
5.  Display line-item arithmetic and budget status.
6.  Generate a draft PO.
7.  Upload or select a sample invoice.
8.  Show invoice-to-PO and invoice-to-delivery comparisons.
9.  Display critic findings and the final audit report.

### Planted conflict

Choose one controlled failure, such as: - The preferred vendor is
unavailable on the requested date. - A vendor is not on the approved
list. - The estimated total exceeds the budget. - An invoice line rate
differs from the PO. - A duplicate invoice identifier is present. - The
delivery record supports fewer meals than the invoice bills.

The system should identify the conflict, show the evidence, and either
select a valid alternative or stop and request human review. Do not hide
a failure just to make the demo look successful.

## 13. Evaluation and success metrics

Measure the prototype rather than relying only on a persuasive demo.

  -----------------------------------------------------------------------
  Metric                              How to measure
  ----------------------------------- -----------------------------------
  Request extraction accuracy         Percentage of required fields
                                      correctly extracted from a labelled
                                      test set

  Policy citation accuracy            Percentage of policy conclusions
                                      supported by the cited source
                                      clause

  Vendor eligibility correctness      Percentage of selected vendors that
                                      satisfy approval, capacity, date,
                                      and service constraints

  Budget calculation correctness      Exact agreement between displayed
                                      totals and deterministic expected
                                      totals

  Invoice mismatch detection          Precision and recall on a small
                                      labelled set of matching and
                                      mismatching invoices

  Duplicate detection                 Correctly identified duplicate
                                      invoice IDs divided by known
                                      duplicates

  Workflow completion                 Percentage of valid scenarios that
                                      reach the intended final state

  Audit completeness                  Percentage of decisions with
                                      evidence, status, and an
                                      explanation recorded
  -----------------------------------------------------------------------

For the demo, create a small labelled test set with both valid and
invalid cases. Report actual measured results; do not invent performance
percentages.

## 14. Human approval and safety boundaries

The system should clearly distinguish between: - **Informational
checks:** extracting fields and finding policy clauses. -
**Recommendations:** vendor ranking and proposed plans. - **Draft
actions:** preparing a draft PO or reconciliation report. -
**Human-controlled actions:** final approvals, issuing POs, and
authorizing payment.

The hackathon prototype must not execute irreversible financial actions.
When information is missing, policy evidence is weak, the budget is
exceeded, or records conflict, the system should stop, explain the
issue, and request human review.

## 15. Main risks and mitigations

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Too much scope                      Implement one event/catering
                                      workflow before adding more
                                      categories.

  Hallucinated policies               Require retrieved source citations
                                      and flag unsupported conclusions.

  Incorrect arithmetic                Use deterministic Python
                                      calculations and tests.

  Unreliable agent coordination       Use explicit workflow states and
                                      structured agent outputs.

  Integration delays                  Use JSON/SQLite and local synthetic
                                      data before adding external
                                      integrations.

  Demo data looks like real company   Label it synthetic and use
  data                                fictional vendors.

  Silent failure on a conflict        Include a planted failure test and
                                      make escalation visible.

  Unsafe autonomous action            Generate drafts only; keep
                                      approvals and payments under human
                                      control.
  -----------------------------------------------------------------------

## 16. What has already been created

A **proposal website** was created for the project:

-   **Name:** WorkplaceOps AI --- Hackathon Proposal
-   **Project slug:** `workplaceops-ai`
-   **Website:** https://workplaceops-ai.hatchable.site
-   **Purpose:** Explains the project idea, business impact, workflow,
    architecture, stack, timeline, team roles, metrics, and demo story.

Important distinction: the proposal website is **not the completed
working AI application**. Its illustrative budget simulator is a
client-side demonstration, not connected to a real agent backend. The
working hackathon MVP still needs the backend, data, agent/workflow
logic, invoice reconciliation, tests, and end-to-end UI described in
this guide.

## 17. Recommended build order

Build in this order to maximize the chance of a complete demo:

1.  Define Pydantic schemas and synthetic JSON data.
2.  Implement and test deterministic cost, budget, eligibility,
    duplicate, and invoice-match functions.
3.  Build a single FastAPI endpoint that accepts a request and returns a
    structured result.
4.  Add policy retrieval and citation display.
5.  Add vendor matching and availability checks.
6.  Add draft PO generation.
7.  Add invoice reconciliation.
8.  Add orchestrator state tracking and critic review.
9.  Build the UI and audit trail.
10. Run happy-path and conflict-path tests, then rehearse the demo.

## 18. Final recommendation

Deliver a small, dependable **Request-to-Draft-PO-to-Invoice-Check**
workflow, not a large collection of loosely connected agents. The
strongest version of the project will show each decision, the data or
policy evidence behind it, the calculations used, and how the system
handles conflicts safely.

The key judging message is:

> WorkplaceOps AI turns a fragmented workplace request process into one
> traceable workflow---from intake and policy checks to vendor planning,
> draft procurement, and invoice verification---while keeping financial
> approvals under human control.
