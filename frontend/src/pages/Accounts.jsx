import { useEffect, useState } from "react";
import { Link as RouterLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, Box, Button, Card, CardContent, Chip, Link, MenuItem, Paper, Stack, Switch, FormControlLabel, TextField, Typography } from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import { api } from "../api";
import { DataTable, ErrorState, fieldError, formatDate, formatMoney, FormActions, LoadingState, Page, PageLoading, useApiData } from "../components/Common";
import { useApp } from "../context";

const subtypeTypes = {
  bank: "asset", cash: "asset", accounts_receivable: "asset", fixed_asset: "asset", other_asset: "asset",
  credit_card: "liability", accounts_payable: "liability", owner_loan_payable: "liability", other_liability: "liability",
  owner_contribution: "equity", owner_draw: "equity", retained_earnings: "equity",
  sales: "revenue", other_revenue: "revenue",
  cost_of_goods: "expense", operating_expense: "expense", other_expense: "expense",
};

export function AccountsList() {
  const { session } = useApp();
  const { data, loading, error } = useApiData("/api/accounts/", []);
  return (
    <Page title="Accounts" eyebrow="Chart of accounts" crumbs={[{ label: "Accounts" }]} actions={session.permissions.administer && <Button component={RouterLink} to="/accounts/new/" variant="contained" startIcon={<AddRounded />}>New account</Button>}>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.accounts} emptyTitle="No accounts yet" columns={[
        { key: "code", label: "Code" },
        { key: "name", label: "Name", sx: { minWidth: 180 }, render: (row) => <Link sx={{ fontWeight: 700 }} component={RouterLink} to={`/accounts/${row.id}/`}>{row.name}</Link> },
        { key: "description", label: "Description", sx: { minWidth: 220 }, render: (row) => row.description || "—" },
        { key: "type", label: "Type", render: (row) => row.type_label },
        { key: "subtype", label: "Subtype", render: (row) => row.subtype_label || "—" },
        { key: "status", label: "Status", render: (row) => <Chip size="small" color={row.is_active ? "success" : "default"} variant="outlined" label={row.is_active ? "Active" : "Inactive"} /> },
        { key: "balance", label: "Balance", align: "right", render: (row) => <Typography sx={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.balance)}</Typography> },
      ]} />}
    </Page>
  );
}

export function AccountDetail() {
  const { id } = useParams();
  const { session } = useApp();
  const { data, loading, error } = useApiData(`/api/accounts/${id}/`, [id]);
  if (loading) return <PageLoading title="Account" />;
  if (error) return <Page title="Account" crumbs={[{ label: "Accounts", to: "/accounts/" }, { label: "Unavailable" }]}><ErrorState error={error} /></Page>;
  const account = data.account;
  return (
    <Page title={account.name} eyebrow={`${account.code} · ${account.type_label}`} subtitle={account.description} crumbs={[{ label: "Accounts", to: "/accounts/" }, { label: account.name }]} actions={<>{session.permissions.edit_books && <><Button component={RouterLink} to={`/transactions/new/?account=${account.id}&side=credit`} variant="outlined">Credit</Button><Button component={RouterLink} to={`/transactions/new/?account=${account.id}&side=debit`} variant="contained">Debit</Button></>}{session.permissions.administer && <Button component={RouterLink} to={`/accounts/${account.id}/edit/`} color="inherit" startIcon={<EditRounded />}>Edit</Button>}</>}>
      <Stack direction={{ xs: "column", md: "row" }} spacing={3} sx={{ alignItems: { md: "flex-start" }, mb: 3 }}>
        <Card sx={{ width: { xs: "100%", sm: 360 }, flexShrink: 0, borderLeft: 4, borderLeftColor: "primary.main" }}><CardContent><Typography variant="overline" color="text.secondary">Current balance</Typography><Typography sx={{ fontSize: 28, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{formatMoney(account.balance)}</Typography><Typography variant="caption" color="text.secondary">{account.normal_balance} normal balance</Typography></CardContent></Card>
        {data.sync && <SyncPanel key={account.id} accountId={account.id} isCard={account.subtype === "credit_card"} initial={data.sync} canEdit={session.permissions.edit_books} />}
      </Stack>
      <Typography variant="h2" sx={{ mb: 1.5 }}>Account ledger</Typography>
      <DataTable rows={data.ledger} emptyTitle="No posted activity" columns={[
        { key: "date", label: "Date", render: (row) => formatDate(row.date) },
        { key: "entry", label: "Entry", render: (row) => <Link component={RouterLink} to={`/transactions/${row.entry.id}/`}>{row.entry.number}</Link> },
        { key: "description", label: "Description", sx: { minWidth: 220 } },
        { key: "debit", label: "Debit", align: "right", render: (row) => Number(row.debit) ? formatMoney(row.debit) : "—" },
        { key: "credit", label: "Credit", align: "right", render: (row) => Number(row.credit) ? formatMoney(row.credit) : "—" },
        { key: "balance", label: "Balance", align: "right", render: (row) => <Typography sx={{ fontWeight: 600 }}>{formatMoney(row.balance)}</Typography> },
      ]} />
    </Page>
  );
}

function formatDateTime(value) {
  return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)) : "Never";
}

function SyncPanel({ accountId, isCard, initial, canEdit }) {
  const { notify } = useApp();
  const [sync, setSync] = useState(initial);
  const [values, setValues] = useState({ statement_end_date: "", statement_ending_balance: "" });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const statement = sync.latest_statement;
  const difference = sync.difference_vs_statement;
  const submit = async (event) => {
    event.preventDefault(); setSaving(true); setErrors({});
    try { const response = await api(`/api/accounts/${accountId}/statements/`, { method: "POST", body: values }); setSync(response.sync); setValues({ statement_end_date: "", statement_ending_balance: "" }); notify("Statement balance saved."); }
    catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); }
    finally { setSaving(false); }
  };
  return (
    <Card variant="outlined" sx={{ flex: 1, minWidth: 0 }}><CardContent>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(3, 1fr)" }, gap: 2, mb: canEdit ? 2 : 0 }}>
        <Box><Typography variant="overline" color="text.secondary">Last synced</Typography><Typography sx={{ fontWeight: 600 }}>{formatDateTime(sync.last_synced_at)}</Typography><Typography variant="caption" color="text.secondary">Latest transaction {formatDate(sync.last_transaction_date)}</Typography></Box>
        <Box><Typography variant="overline" color="text.secondary">Latest statement</Typography>{statement ? <><Typography sx={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{formatMoney(statement.statement_ending_balance)}</Typography><Typography variant="caption" color="text.secondary">Ending {formatDate(statement.statement_end_date)}</Typography></> : <Typography color="text.secondary">None entered</Typography>}</Box>
        <Box><Typography variant="overline" color="text.secondary">Book vs. statement</Typography>{statement ? <><Typography color={Number(difference) === 0 ? "success.main" : "warning.main"} sx={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{Number(difference) === 0 ? "Matches" : formatMoney(difference)}</Typography><Typography variant="caption" color="text.secondary">Books show {formatMoney(sync.book_balance_at_statement_date)} on that date</Typography></> : <Typography color="text.secondary">—</Typography>}</Box>
      </Box>
      {canEdit && <Box component="form" onSubmit={submit}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Typography variant="subtitle2" sx={{ mb: 1 }}>Enter statement balance</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ alignItems: { sm: "flex-start" } }}>
          <TextField required size="small" type="date" label="Statement end date" slotProps={{ inputLabel: { shrink: true } }} value={values.statement_end_date} onChange={(event) => setValues({ ...values, statement_end_date: event.target.value })} error={Boolean(fieldError(errors, "statement_end_date"))} helperText={fieldError(errors, "statement_end_date")} />
          <TextField required size="small" label={isCard ? "Ending balance owed" : "Ending balance"} slotProps={{ htmlInput: { inputMode: "decimal" } }} value={values.statement_ending_balance} onChange={(event) => setValues({ ...values, statement_ending_balance: event.target.value })} error={Boolean(fieldError(errors, "statement_ending_balance"))} helperText={fieldError(errors, "statement_ending_balance")} />
          <Button type="submit" variant="outlined" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        </Stack>
      </Box>}
    </CardContent></Card>
  );
}

export function AccountForm() {
  const { id } = useParams();
  const editing = Boolean(id);
  const navigate = useNavigate();
  const location = useLocation();
  const { options, refreshOptions, notify } = useApp();
  const detail = useApiData(editing ? `/api/accounts/${id}/` : null, [id]);
  const requested = new URLSearchParams(location.search).get("subtype") || "";
  const seeded = subtypeTypes[requested] ? { type: subtypeTypes[requested], subtype: requested } : { type: "asset", subtype: "bank" };
  const [values, setValues] = useState({ code: "", name: "", description: "", is_active: true, ...seeded });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (editing && detail.data?.account) { const account = detail.data.account; setValues({ code: account.code, name: account.name, type: account.type, subtype: account.subtype, description: account.description, is_active: account.is_active }); } }, [editing, detail.data]);
  if (editing && detail.loading) return <PageLoading title="Edit account" />;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { const response = await api(editing ? `/api/accounts/${id}/` : "/api/accounts/", { method: "POST", body: values }); await refreshOptions(); notify(`Account ${editing ? "updated" : "created"}.`); navigate(`/accounts/${response.account.id || id}/`); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  const subtypes = options?.choices.account_subtypes.filter((item) => subtypeTypes[item.value] === values.type) || [];
  return (
    <Page title={editing ? `Edit ${detail.data?.account.name || "account"}` : "New account"} eyebrow="Chart of accounts" crumbs={[{ label: "Accounts", to: "/accounts/" }, ...(editing ? [{ label: detail.data?.account.name, to: `/accounts/${id}/` }, { label: "Edit" }] : [{ label: "New account" }])]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 760 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)" }, gap: 2 }}>
          <TextField required label="Code" value={values.code} onChange={set("code")} error={Boolean(fieldError(errors, "code"))} helperText={fieldError(errors, "code")} />
          <TextField required label="Name" value={values.name} onChange={set("name")} error={Boolean(fieldError(errors, "name"))} helperText={fieldError(errors, "name")} />
          <TextField required select label="Type" value={values.type} onChange={(event) => { const type = event.target.value; const first = options?.choices.account_subtypes.find((item) => subtypeTypes[item.value] === type); setValues({ ...values, type, subtype: first?.value || "" }); }}>{options?.choices.account_types.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField>
          <TextField select label="Subtype" value={values.subtype} onChange={set("subtype")} error={Boolean(fieldError(errors, "subtype"))} helperText={fieldError(errors, "subtype")}><MenuItem value="">No subtype</MenuItem>{subtypes.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField>
          <TextField multiline minRows={3} label="Description" value={values.description} onChange={set("description")} sx={{ gridColumn: { sm: "1 / -1" } }} />
          <FormControlLabel control={<Switch checked={values.is_active} onChange={(event) => setValues({ ...values, is_active: event.target.checked })} />} label="Active account" />
        </Box>
        <FormActions saving={saving} submitLabel="Save account" cancelTo={editing ? `/accounts/${id}/` : "/accounts/"} />
      </Paper>
    </Page>
  );
}
