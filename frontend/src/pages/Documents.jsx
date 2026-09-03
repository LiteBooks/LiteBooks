import { useEffect, useState } from "react";
import { Link as RouterLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, Box, Button, Card, CardContent, Chip, Link, MenuItem, Paper, Stack, Tab, Tabs, TextField, Typography } from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import AttachFileRounded from "@mui/icons-material/AttachFileRounded";
import PaymentsRounded from "@mui/icons-material/PaymentsRounded";
import { api, formDataFrom } from "../api";
import { DataTable, DateField, ErrorState, fieldError, formatDate, formatMoney, FormActions, LoadingState, localToday, Page, PageLoading, useApiData } from "../components/Common";
import { useApp } from "../context";

export function DocumentsList() {
  const { session } = useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const kind = new URLSearchParams(location.search).get("kind") === "bill" ? "bill" : "invoice";
  const { data, loading, error } = useApiData(`/api/documents/?kind=${kind}`, [kind]);
  return (
    <Page title="Invoices & bills" eyebrow="Accounts receivable and payable" crumbs={[{ label: "Invoices & bills" }]} actions={session.permissions.edit_books && <><Button component={RouterLink} to="/documents/new/bill/" variant="outlined" startIcon={<AddRounded />}>New bill</Button><Button component={RouterLink} to="/documents/new/invoice/" variant="contained" startIcon={<AddRounded />}>New invoice</Button></>}>
      <Tabs value={kind} onChange={(_, value) => navigate(`/documents/?kind=${value}`)} sx={{ mb: 2, borderBottom: 1, borderColor: "divider" }}><Tab value="invoice" label="Invoices" /><Tab value="bill" label="Bills" /></Tabs>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.documents} emptyTitle={`No ${kind === "invoice" ? "invoices" : "bills"} yet`} columns={[
        { key: "number", label: "Number", sx: { minWidth: 170 }, render: (row) => <Box><Link component={RouterLink} to={`/documents/${row.id}/`} fontWeight={700}>{row.number}</Link><Typography display="block" variant="caption" color="text.secondary">{row.description}</Typography></Box> },
        { key: "contact", label: "Contact", render: (row) => row.contact.name },
        { key: "issue_date", label: "Issued", render: (row) => formatDate(row.issue_date) },
        { key: "due_date", label: "Due", render: (row) => formatDate(row.due_date) },
        { key: "status", label: "Status", render: (row) => <Chip size="small" label={row.status_label} color={row.status === "paid" ? "success" : row.status === "partial" ? "warning" : "default"} variant={row.status === "open" ? "outlined" : "filled"} /> },
        { key: "amount", label: "Total", align: "right", render: (row) => formatMoney(row.amount) },
        { key: "balance_due", label: "Balance", align: "right", render: (row) => <Typography fontWeight={700}>{formatMoney(row.balance_due)}</Typography> },
      ]} />}
    </Page>
  );
}

export function DocumentDetail() {
  const { id } = useParams();
  const { session } = useApp();
  const { data, loading, error } = useApiData(`/api/documents/${id}/`, [id]);
  if (loading) return <PageLoading title="Document" />;
  if (error) return <Page title="Document" crumbs={[{ label: "Invoices & bills", to: "/documents/" }, { label: "Unavailable" }]}><ErrorState error={error} /></Page>;
  const item = data.document;
  return (
    <Page title={item.number} eyebrow={`${item.kind_label} · ${item.status_label}`} subtitle={item.contact.name} crumbs={[{ label: "Invoices & bills", to: `/documents/?kind=${item.kind}` }, { label: item.number }]} actions={session.permissions.edit_books && Number(item.balance_due) > 0 && item.status !== "void" && <Button component={RouterLink} to={`/documents/${item.id}/payment/`} variant="contained" startIcon={<PaymentsRounded />}>Record payment</Button>}>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr 1fr", lg: "repeat(4, 1fr)" }, gap: 2, mb: 3 }}>{[
        ["Issued", formatDate(item.issue_date)], ["Due", formatDate(item.due_date)], ["Total", formatMoney(item.amount)], ["Balance due", formatMoney(item.balance_due)],
      ].map(([label, value]) => <Card key={label}><CardContent><Typography variant="overline" color="text.secondary">{label}</Typography><Typography variant="h2" sx={{ fontVariantNumeric: "tabular-nums" }}>{value}</Typography></CardContent></Card>)}</Box>
      <Paper variant="outlined" sx={{ mb: 3 }}><Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" } }}>{[
        ["Description", item.description], ["Posted entry", <Link component={RouterLink} to={`/transactions/${item.journal_entry.id}/`}>{item.journal_entry.number}</Link>], ["Control account", item.control_account.display_name], ["Category account", item.category_account.display_name],
      ].map(([label, value], index) => <Box key={label} sx={{ p: 2, borderBottom: index < 2 ? 1 : 0, borderRight: index % 2 === 0 ? { sm: 1 } : 0, borderColor: "divider" }}><Typography variant="overline" color="text.secondary">{label}</Typography><Typography component="div">{value}</Typography></Box>)}</Box></Paper>
      <Typography variant="h2" sx={{ mb: 1.5 }}>Payments</Typography>
      <DataTable rows={item.payments} emptyTitle="No payments recorded" columns={[
        { key: "date", label: "Date", render: (row) => formatDate(row.date) },
        { key: "journal_entry", label: "Entry", render: (row) => <Link component={RouterLink} to={`/transactions/${row.journal_entry.id}/`}>{row.journal_entry.number}</Link> },
        { key: "cash_account", label: "Bank or cash account", render: (row) => row.cash_account.display_name },
        { key: "amount", label: "Amount", align: "right", render: (row) => <Typography fontWeight={700}>{formatMoney(row.amount)}</Typography> },
      ]} />
    </Page>
  );
}

export function DocumentForm() {
  const { kind } = useParams();
  const navigate = useNavigate();
  const { options, refreshOptions, notify } = useApp();
  const isInvoice = kind === "invoice";
  const [values, setValues] = useState({ kind, number: "", contact: "", issue_date: localToday(), due_date: localToday(), description: "", amount: "", control_account: "", category_account: "" });
  const [file, setFile] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const contacts = options?.contacts.filter((item) => item.is_active && (isInvoice ? ["customer", "both"].includes(item.kind) : ["vendor", "both"].includes(item.kind))) || [];
  const controls = options?.accounts.filter((item) => item.is_active && item.subtype === (isInvoice ? "accounts_receivable" : "accounts_payable")) || [];
  const categories = options?.accounts.filter((item) => item.is_active && item.type === (isInvoice ? "revenue" : "expense")) || [];
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { const response = await api("/api/documents/", { method: "POST", body: formDataFrom(values, file) }); await refreshOptions(); notify(`${isInvoice ? "Invoice" : "Bill"} posted.`); navigate(`/documents/${response.document.id}/`); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  const label = isInvoice ? "invoice" : "bill";
  return (
    <Page title={`New ${label}`} eyebrow={isInvoice ? "Accounts receivable" : "Accounts payable"} crumbs={[{ label: "Invoices & bills", to: `/documents/?kind=${kind}` }, { label: `New ${label}` }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 820 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)" }, gap: 2 }}>
          <TextField required label={`${isInvoice ? "Invoice" : "Bill"} number`} value={values.number} onChange={set("number")} error={Boolean(fieldError(errors, "number"))} helperText={fieldError(errors, "number")} />
          <TextField required select label={isInvoice ? "Customer" : "Vendor"} value={values.contact} onChange={set("contact")} error={Boolean(fieldError(errors, "contact"))} helperText={fieldError(errors, "contact")}><MenuItem value="">Choose contact</MenuItem>{contacts.map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</TextField>
          <DateField required label="Issue date" value={values.issue_date} onChange={(value) => setValues({ ...values, issue_date: value })} />
          <DateField required label="Due date" value={values.due_date} onChange={(value) => setValues({ ...values, due_date: value })} error={Boolean(fieldError(errors, "due_date"))} helperText={fieldError(errors, "due_date")} />
          <TextField required label="Description" value={values.description} onChange={set("description")} sx={{ gridColumn: { sm: "1 / -1" } }} />
          <TextField required type="number" label="Amount" value={values.amount} onChange={set("amount")} slotProps={{ htmlInput: { min: .01, step: .01 } }} />
          <Box />
          <TextField required select label={isInvoice ? "Accounts receivable account" : "Accounts payable account"} value={values.control_account} onChange={set("control_account")}><MenuItem value="">Choose control account</MenuItem>{controls.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
          <TextField required select label={isInvoice ? "Revenue account" : "Expense account"} value={values.category_account} onChange={set("category_account")}><MenuItem value="">Choose category account</MenuItem>{categories.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
        </Box>
        <Button component="label" variant="outlined" startIcon={<AttachFileRounded />} sx={{ mt: 2 }}>{file ? file.name : "Add attachment"}<input hidden type="file" accept="application/pdf,image/png,image/jpeg,image/webp" onChange={(event) => setFile(event.target.files?.[0] || null)} /></Button>
        <FormActions saving={saving} submitLabel={`Post ${label}`} cancelTo={`/documents/?kind=${kind}`} />
      </Paper>
    </Page>
  );
}

export function PaymentForm() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { options, notify } = useApp();
  const detail = useApiData(`/api/documents/${id}/`, [id]);
  const [values, setValues] = useState({ date: localToday(), amount: "", cash_account_id: "", memo: "" });
  const [file, setFile] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (detail.data?.document) {
      setValues((current) => current.amount ? current : { ...current, amount: detail.data.document.balance_due });
    }
  }, [detail.data]);
  if (detail.loading) return <PageLoading title="Record payment" />;
  if (detail.error) return <Page title="Record payment"><ErrorState error={detail.error} /></Page>;
  const item = detail.data.document;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const cashAccounts = options?.accounts.filter((account) => account.is_active && ["bank", "cash"].includes(account.subtype)) || [];
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { await api(`/api/documents/${id}/payment/`, { method: "POST", body: formDataFrom(values, file) }); notify("Payment posted and linked to the original entry."); navigate(`/documents/${id}/`); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  return (
    <Page title={`Pay ${item.kind_label.toLowerCase()} ${item.number}`} eyebrow="Linked payment" subtitle={`Remaining balance ${formatMoney(item.balance_due)}`} crumbs={[{ label: "Invoices & bills", to: `/documents/?kind=${item.kind}` }, { label: item.number, to: `/documents/${id}/` }, { label: "Payment" }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 720 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Stack spacing={2}><DateField required label="Payment date" value={values.date} onChange={(value) => setValues({ ...values, date: value })} /><TextField required type="number" label="Amount" value={values.amount} onChange={set("amount")} slotProps={{ htmlInput: { min: .01, max: item.balance_due, step: .01 } }} /><TextField required select label="Bank or cash account" value={values.cash_account_id} onChange={set("cash_account_id")}><MenuItem value="">Choose account</MenuItem>{cashAccounts.map((account) => <MenuItem key={account.id} value={account.id}>{account.display_name}</MenuItem>)}</TextField><TextField multiline minRows={3} label="Memo" value={values.memo} onChange={set("memo")} /><Button component="label" variant="outlined" startIcon={<AttachFileRounded />} sx={{ alignSelf: "flex-start" }}>{file ? file.name : "Add attachment"}<input hidden type="file" accept="application/pdf,image/png,image/jpeg,image/webp" onChange={(event) => setFile(event.target.files?.[0] || null)} /></Button></Stack>
        <FormActions saving={saving} submitLabel="Post payment" cancelTo={`/documents/${id}/`} />
      </Paper>
    </Page>
  );
}
