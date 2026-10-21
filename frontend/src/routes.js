import {
  BookOpen, Building2, ClipboardCheck, ClipboardList, FilePlus2, FileText, History, Inbox, LayoutDashboard,
  ListChecks, Receipt, ScrollText, Users,
} from "lucide-react";

/** Role-based navigation (presentation only — the backend enforces every permission). */
export function navForRole(role, { paymentsEnabled = false } = {}) {
  if (role === "admin") {
    const ops = [
      { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
      { to: "/admin/requests", label: "All requests", icon: ClipboardList },
    ];
    const sourcing = [
      { to: "/admin/vendors", label: "Vendor sourcing", icon: Building2 },
      { to: "/admin/rfqs", label: "RFQs & quotes", icon: FileText },
    ];
    const policy = [
      { to: "/admin/policy-library", label: "Policy library", icon: BookOpen },
      { to: "/admin/policy-rules", label: "Policy rules", icon: ScrollText },
      { to: "/admin/approvals", label: "Approvals (view only)", icon: ClipboardCheck },
    ];
    if (paymentsEnabled) ops.push({ to: "/admin/payments", label: "Payments (Simulation)", icon: Receipt });
    return [
      { title: "Operations", items: ops },
      { title: "Sourcing", items: sourcing },
      { title: "Policy", items: policy },
    ];
  }
  if (role === "approver") {
    return [
      { title: "Approvals", items: [
        { to: "/approvals", label: "Pending approvals", icon: Inbox, end: true },
        { to: "/approvals/history", label: "Approval history", icon: History },
      ] },
      { title: "Policy", items: [
        { to: "/policy-review", label: "Policy review", icon: ListChecks },
      ] },
    ];
  }
  return [
    { title: "Requests", items: [
      { to: "/requests", label: "My requests", icon: Users, end: true },
      { to: "/requests/new", label: "New request", icon: FilePlus2 },
    ] },
  ];
}

export function homeFor(role) {
  if (role === "admin") return "/admin";
  if (role === "approver") return "/approvals";
  return "/requests";
}

const TITLES = [
  [/^\/requests\/new$/, "New request"],
  [/^\/requests\/[^/]+$/, "Request details"],
  [/^\/requests$/, "My requests"],
  [/^\/admin$/, "Overview"],
  [/^\/admin\/requests\/[^/]+$/, "Request details"],
  [/^\/admin\/requests$/, "All requests"],
  [/^\/admin\/vendors/, "Vendor sourcing"],
  [/^\/admin\/rfqs/, "RFQs & quotes"],
  [/^\/admin\/policy-library/, "Policy library"],
  [/^\/admin\/policy-rules/, "Policy rules"],
  [/^\/admin\/approvals/, "Approvals"],
  [/^\/admin\/payments/, "Payments (Simulation)"],
  [/^\/approvals\/history/, "Approval history"],
  [/^\/approvals/, "Pending approvals"],
  [/^\/policy-review/, "Policy review"],
];
export function pageTitle(pathname) {
  const hit = TITLES.find(([re]) => re.test(pathname));
  return hit ? hit[1] : "WorkplaceOps AI";
}
