import { useState } from "react";
import { Link as RouterLink, useNavigate } from "react-router-dom";
import { Alert, Box, Button, Link, MenuItem, Paper, Stack, TextField, Typography } from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import AttachFileRounded from "@mui/icons-material/AttachFileRounded";
import { api, formDataFrom } from "../api";
import { DataTable, DateField, ErrorState, formatDate, formatMoney, FormActions, LoadingState, localToday, Page, useApiData } from "../components/Common";
import { useApp } from "../context";

export function OwnersList() {
  const { session } = useApp();
  const { data, loading, error } = useApiData("/api/owners/", []);
  return (
    <Page title="Owner balances" eyebrow="Equity and liabilities" crumbs={[{ label: "Owners" }]} actions={session.permissions.edit_books && <Button component={RouterLink} to="/owners/activity/new/" variant="contained" startIcon={<AddRounded />}>New owner activity</Button>}>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <>
        <DataTable rows={data.owners} getKey={(row) => row.owner.id} emptyTitle="No owner balances yet" emptyDetail="Create an owner contact and post owner activity to begin." columns={[
          { key: "owner", label: "Owner", render: (row) => <Typography fontWeight={700}>{row.owner.name}</Typography> },
          { key: "contributions", label: "Contributions", align: "right", render: (row) => formatMoney(row.contributions) },
          { key: "draws", label: "Draws", align: "right", render: (row) => formatMoney(row.draws) },
          { key: "owed", label: "Amount owed to owner", align: "right", render: (row) => <Typography fontWeight={700}>{formatMoney(row.owed)}</Typography> },
        ]} />
        <Typography variant="h2" sx={{ mt: 4, mb: 1.5 }}>Recent activity</Typography>
        <DataTable rows={data.activities} emptyTitle="No owner activity yet" columns={[
          { key: "date", label: "Date", render: (row) => formatDate(row.date) }, { key: "owner", label: "Owner", render: (row) => row.owner.name },
          { key: "kind", label: "Type", render: (row) => row.kind_label }, { key: "entry", label: "Entry", render: (row) => <Link component={RouterLink} to={`/transactions/${row.entry.id}/`}>{row.entry.number}</Link> },
          { key: "amount", label: "Amount", align: "right", render: (row) => formatMoney(row.amount) },
        ]} />
      </>}
    </Page>
  );
}

export function OwnerActivityForm() {
  const navigate = useNavigate();
  const { options, notify } = useApp();
  const [values, setValues] = useState({ owner_id: "", kind: "contribution", date: localToday(), amount: "", cash_account_id: "", offset_account_id: "", memo: "" });
  const [file, setFile] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const owners = options?.contacts.filter((item) => item.kind === "owner" && item.is_active) || [];
  const cash = options?.accounts.filter((item) => item.is_active && ["bank", "cash"].includes(item.subtype)) || [];
  const expectedSubtype = values.kind === "contribution" ? "owner_contribution" : values.kind === "draw" ? "owner_draw" : "owner_loan_payable";
  const offsets = options?.accounts.filter((item) => item.is_active && item.subtype === expectedSubtype) || [];
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { const response = await api("/api/owners/", { method: "POST", body: formDataFrom(values, file) }); notify("Owner activity posted."); navigate(`/transactions/${response.entry_id}/`); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  return (
    <Page title="New owner activity" eyebrow="Equity and liabilities" crumbs={[{ label: "Owners", to: "/owners/" }, { label: "New activity" }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 760 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" }, gap: 2 }}>
          <TextField required select label="Owner" value={values.owner_id} onChange={set("owner_id")}><MenuItem value="">Choose owner</MenuItem>{owners.map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</TextField>
          <TextField required select label="Activity type" value={values.kind} onChange={(event) => setValues({ ...values, kind: event.target.value, offset_account_id: "" })}>{options?.choices.owner_activity_kinds.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField>
          <DateField required label="Date" value={values.date} onChange={(value) => setValues({ ...values, date: value })} />
          <TextField required type="number" label="Amount" value={values.amount} onChange={set("amount")} slotProps={{ htmlInput: { min: .01, step: .01 } }} />
          <TextField required select label="Bank or cash account" value={values.cash_account_id} onChange={set("cash_account_id")}><MenuItem value="">Choose account</MenuItem>{cash.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
          <TextField required select label="Owner equity or loan account" value={values.offset_account_id} onChange={set("offset_account_id")}><MenuItem value="">Choose account</MenuItem>{offsets.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
          <TextField multiline minRows={3} label="Memo" value={values.memo} onChange={set("memo")} sx={{ gridColumn: { sm: "1 / -1" } }} />
        </Box>
        <Button component="label" variant="outlined" startIcon={<AttachFileRounded />} sx={{ mt: 2 }}>{file ? file.name : "Add attachment"}<input hidden type="file" accept="application/pdf,image/png,image/jpeg,image/webp" onChange={(event) => setFile(event.target.files?.[0] || null)} /></Button>
        <FormActions saving={saving} submitLabel="Post owner activity" cancelTo="/owners/" />
      </Paper>
    </Page>
  );
}
