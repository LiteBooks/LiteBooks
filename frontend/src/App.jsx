import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Box, CircularProgress, Typography } from "@mui/material";
import Layout from "./components/Layout";
import { Page } from "./components/Common";
import { useApp } from "./context";
import Dashboard from "./pages/Dashboard";
import { AccountsList, AccountDetail, AccountForm } from "./pages/Accounts";
import AuditPage from "./pages/Audit";
import { ContactsList, ContactForm } from "./pages/Contacts";
import { DocumentsList, DocumentDetail, DocumentForm, PaymentForm } from "./pages/Documents";
import LoginPage from "./pages/Login";
import { OwnersList, OwnerActivityForm } from "./pages/Owners";
import { ReportDetail, ReportsIndex } from "./pages/Reports";
import { PeriodsPage, ProfilePage, UserEditPage, UsersPage } from "./pages/Settings";
import { TransactionDetail, TransactionForm, TransactionsList } from "./pages/Transactions";

function FullPageLoading() {
  return <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center" }}><Box sx={{ textAlign: "center" }}><CircularProgress size={32} /><Typography color="text.secondary" sx={{ mt: 1.5 }}>Loading LiteBooks</Typography></Box></Box>;
}

function NotFound() {
  return <Page title="Page not found" crumbs={[{ label: "Not found" }]}><Typography color="text.secondary">The page you requested does not exist.</Typography></Page>;
}

export default function App() {
  const { session, options } = useApp();
  const location = useLocation();
  if (!session) return <FullPageLoading />;
  if (!session.authenticated) return location.pathname === "/login/" ? <LoginPage /> : <Navigate to="/login/" replace state={{ from: location.pathname }} />;
  if (!options) return <FullPageLoading />;
  if (location.pathname === "/login/") return <Navigate to="/" replace />;
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/transactions/" element={<TransactionsList />} />
        <Route path="/transactions/new/" element={<TransactionForm />} />
        <Route path="/transactions/new/split/" element={<TransactionForm />} />
        <Route path="/transactions/:id/" element={<TransactionDetail />} />
        <Route path="/transactions/:id/edit/" element={<TransactionForm />} />
        <Route path="/accounts/" element={<AccountsList />} />
        <Route path="/accounts/new/" element={<AccountForm />} />
        <Route path="/accounts/:id/" element={<AccountDetail />} />
        <Route path="/accounts/:id/edit/" element={<AccountForm />} />
        <Route path="/contacts/" element={<ContactsList />} />
        <Route path="/contacts/new/" element={<ContactForm />} />
        <Route path="/contacts/:id/edit/" element={<ContactForm />} />
        <Route path="/documents/" element={<DocumentsList />} />
        <Route path="/documents/new/:kind/" element={<DocumentForm />} />
        <Route path="/documents/:id/" element={<DocumentDetail />} />
        <Route path="/documents/:id/payment/" element={<PaymentForm />} />
        <Route path="/owners/" element={<OwnersList />} />
        <Route path="/owners/activity/new/" element={<OwnerActivityForm />} />
        <Route path="/reports/" element={<ReportsIndex />} />
        <Route path="/reports/:report/" element={<ReportDetail />} />
        <Route path="/settings/periods/" element={<PeriodsPage />} />
        <Route path="/settings/profile/" element={<ProfilePage />} />
        <Route path="/settings/users/" element={<UsersPage />} />
        <Route path="/settings/users/:id/edit/" element={<UserEditPage />} />
        <Route path="/audit/" element={<AuditPage />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}
