import { Fragment, lazy, Suspense, type JSX } from "react";
import { Spin } from "antd";
import { createBrowserRouter, Navigate, useLocation } from "react-router-dom";
import { AppLayout } from "./layout";
import { RecoveryRoute } from "./CrashRecovery";
import { useAuth } from "./auth";
import { can } from "@/utils/permissions";
import { ForbiddenPage } from "@/components";

const Login = lazy(() => import("@/pages/Login"));
const Initialize = lazy(() => import("@/pages/Initialize"));
const Dashboard = lazy(() => import("@/pages/Dashboard"));
const RiskOverview = lazy(() => import("@/pages/RiskOverview"));
const Funds = lazy(() => import("@/pages/Funds"));
const FundDetail = lazy(() => import("@/pages/FundDetail"));
const Imports = lazy(() => import("@/pages/Imports"));
const Reviews = lazy(() => import("@/pages/Reviews"));
const Mail = lazy(() => import("@/pages/Mail"));
const AdminFunds = lazy(() => import("@/pages/AdminFunds"));
const AdminSubjects = lazy(() => import("@/pages/AdminSubjects"));
const AdminRiskRules = lazy(() => import("@/pages/AdminRiskRules"));
const AdminUsers = lazy(() => import("@/pages/AdminUsers"));
const AccountPermissions = lazy(() => import("@/pages/AccountPermissions"));
const AdminAudit = lazy(() => import("@/pages/AdminAudit"));
const AdminSettings = lazy(() => import("@/pages/AdminSettings"));
const AdminRetention = lazy(() => import("@/pages/AdminRetention"));

function Loading() {
  return (
    <div className="fd-state fd-state--page" role="status" aria-live="polite">
      <Spin />
    </div>
  );
}

function Guard({ children }: { children: JSX.Element }) {
  const { loading, initialized, session } = useAuth();
  const location = useLocation();
  if (loading) return <Loading />;
  if (!initialized) return <Navigate to="/initialize" replace />;
  if (!session) {
    // Remember the deep link so the login page can bring the user back.
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
  }
  return <Fragment key={`${session.id}:${session.role}:${session.fund_access_mode}:${session.fund_ids?.join(",")}`}>{children}</Fragment>;
}

function EntryGuard({ mode, children }: { mode: "login" | "initialize"; children: JSX.Element }) {
  const { loading, initialized, session } = useAuth();
  if (loading) return <Loading />;
  if (!initialized && mode !== "initialize") return <Navigate to="/initialize" replace />;
  if (initialized && mode === "initialize") return <Navigate to={session ? "/dashboard" : "/login"} replace />;
  if (initialized && session && mode === "login") return <Navigate to="/dashboard" replace />;
  return children;
}

function RoleGuard({ capability, children }: { capability: Parameters<typeof can>[1]; children: JSX.Element }) {
  const { session } = useAuth();
  return can(session?.role, capability, session?.fund_access_mode) ? children : <ForbiddenPage />;
}

function Page({ children }: { children: JSX.Element }) {
  return <Suspense fallback={<Loading />}>{children}</Suspense>;
}

export const router = createBrowserRouter([
  {
    // Pathless route: it contributes no UI of its own, only the screen React Router
    // falls back to when anything below it throws. Without it the router renders its
    // built-in "Unexpected Application Error!" page, which offers no way out.
    errorElement: <RecoveryRoute />,
    children: [
      { path: "/login", element: <EntryGuard mode="login"><Page><Login /></Page></EntryGuard> },
      { path: "/initialize", element: <EntryGuard mode="initialize"><Page><Initialize /></Page></EntryGuard> },
      {
        element: (
          <Guard>
            <AppLayout />
          </Guard>
        ),
        children: [
          { path: "/", element: <Navigate to="/dashboard" replace /> },
          { path: "/dashboard", element: <Page><Dashboard /></Page> },
          { path: "/risk", element: <RoleGuard capability="risk"><Page><RiskOverview /></Page></RoleGuard> },
          { path: "/funds", element: <Page><Funds /></Page> },
          { path: "/funds/:id", element: <Page><FundDetail /></Page> },
          { path: "/imports", element: <RoleGuard capability="imports"><Page><Imports /></Page></RoleGuard> },
          { path: "/reviews", element: <RoleGuard capability="reviews"><Page><Reviews /></Page></RoleGuard> },
          { path: "/mail", element: <RoleGuard capability="mail"><Page><Mail /></Page></RoleGuard> },
          { path: "/admin/funds", element: <RoleGuard capability="adminFunds"><Page><AdminFunds /></Page></RoleGuard> },
          { path: "/admin/subjects", element: <RoleGuard capability="adminSubjects"><Page><AdminSubjects /></Page></RoleGuard> },
          { path: "/admin/risk-rules", element: <RoleGuard capability="riskConfig"><Page><AdminRiskRules /></Page></RoleGuard> },
          { path: "/admin/users", element: <RoleGuard capability="adminUsers"><Page><AdminUsers /></Page></RoleGuard> },
          { path: "/account-permissions", element: <RoleGuard capability="accountPermissions"><Page><AccountPermissions /></Page></RoleGuard> },
          { path: "/admin/audit", element: <RoleGuard capability="adminAudit"><Page><AdminAudit /></Page></RoleGuard> },
          { path: "/admin/settings", element: <RoleGuard capability="adminSettings"><Page><AdminSettings /></Page></RoleGuard> },
          { path: "/admin/retention", element: <RoleGuard capability="adminRetention"><Page><AdminRetention /></Page></RoleGuard> },
        ],
      },
      { path: "*", element: <Navigate to="/dashboard" replace /> },
    ],
  },
]);
