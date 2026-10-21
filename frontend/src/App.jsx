import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import AppLayout from "./components/AppLayout";
import { LoadingState } from "./components/States";
import { useAuth } from "./lib/auth";
import { homeFor } from "./routes";
import LoginPage from "./pages/LoginPage";

const MyRequestsPage = lazy(() => import("./pages/MyRequestsPage"));
const NewRequestPage = lazy(() => import("./pages/NewRequestPage"));
const RequestDetailPage = lazy(() => import("./pages/RequestDetailPage"));
const AdminOverviewPage = lazy(() => import("./pages/AdminOverviewPage"));
const RequestQueuePage = lazy(() => import("./pages/RequestQueuePage"));
const AdminRequestDetailPage = lazy(() => import("./pages/AdminRequestDetailPage"));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage"));
const ApprovalDetailPage = lazy(() => import("./pages/ApprovalDetailPage"));
const VendorSourcingPage = lazy(() => import("./pages/VendorSourcingPage"));
const RfqsPage = lazy(() => import("./pages/RfqsPage"));
const RfqDetailPage = lazy(() => import("./pages/RfqDetailPage"));
const PolicyLibraryPage = lazy(() => import("./pages/PolicyLibraryPage"));
const PolicyRulesPage = lazy(() => import("./pages/PolicyRulesPage"));
const SimulatedPaymentsPage = lazy(() => import("./pages/SimulatedPaymentsPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

/** Route guard for UX only: the backend still authorizes every API call. */
function RequireRole({ roles, children }) {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to={homeFor(user.role)} replace />;
  return children;
}

export default function App() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <LoadingState label="Checking your session…" />;
  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname }} />} />
      </Routes>
    );
  }
  const g = (roles, el) => <RequireRole roles={roles}>{el}</RequireRole>;
  return (
    <Suspense fallback={<LoadingState />}>
      <Routes>
        <Route path="/login" element={<Navigate to={homeFor(user.role)} replace />} />
        <Route element={<AppLayout />}>
          <Route path="/" element={<Navigate to={homeFor(user.role)} replace />} />
          {/* Requester */}
          <Route path="/requests" element={g(["user"], <MyRequestsPage />)} />
          <Route path="/requests/new" element={g(["user"], <NewRequestPage />)} />
          <Route path="/requests/:ticketId" element={g(["user"], <RequestDetailPage />)} />
          {/* Admin */}
          <Route path="/admin" element={g(["admin"], <AdminOverviewPage />)} />
          <Route path="/admin/requests" element={g(["admin"], <RequestQueuePage />)} />
          <Route path="/admin/requests/:ticketId" element={g(["admin"], <AdminRequestDetailPage />)} />
          <Route path="/admin/vendors" element={g(["admin"], <VendorSourcingPage />)} />
          <Route path="/admin/rfqs" element={g(["admin"], <RfqsPage />)} />
          <Route path="/admin/rfqs/:rfqId" element={g(["admin"], <RfqDetailPage />)} />
          <Route path="/admin/policy-library" element={g(["admin"], <PolicyLibraryPage />)} />
          <Route path="/admin/policy-rules" element={g(["admin"], <PolicyRulesPage />)} />
          <Route path="/admin/approvals" element={g(["admin"], <ApprovalsPage mode="pending" readOnly />)} />
          <Route path="/admin/approvals/:approvalId" element={g(["admin"], <ApprovalDetailPage readOnly />)} />
          <Route path="/admin/payments" element={g(["admin"], <SimulatedPaymentsPage />)} />
          {/* Approver */}
          <Route path="/approvals" element={g(["approver"], <ApprovalsPage mode="pending" />)} />
          <Route path="/approvals/history" element={g(["approver"], <ApprovalsPage mode="history" />)} />
          <Route path="/approvals/:approvalId" element={g(["approver"], <ApprovalDetailPage />)} />
          <Route path="/policy-review" element={g(["approver"], <PolicyRulesPage reviewer />)} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
