import { useEffect, useMemo, useState } from "react";
import { Link as RouterLink, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  Alert, Box, Button, Chip, Divider, FormControlLabel, IconButton, Link, MenuItem, Paper, Stack,
  Switch, TextField, Tooltip, Typography,
} from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import AttachFileRounded from "@mui/icons-material/AttachFileRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import PostAddRounded from "@mui/icons-material/PostAddRounded";
import { api, formDataFrom } from "../api";
import { DataTable, DateField, ErrorState, fieldError, formatDate, formatMoney, FormActions, LoadingState, localToday, Page, PageLoading, useApiData } from "../components/Common";
import { useApp } from "../context";

const blankLine = () => ({ account_id: "", description: "", debit: "", credit: "", owner_id: "" });

export function TransactionsList() {
  const { session, options } = useApp();
  const location = useLocation();
  const initial = useMemo(() => new URLSearchParams(location.search), []);
  const [filters, setFilters] = useState({ q: initial.get("q") || "", account: initial.get("account") || "", source: initial.get("source") || "", start: initial.get("start") || "", end: initial.get("end") || "" });
  const [query, setQuery] = useState(location.search);
  const { data, loading, error } = useApiData(`/api/transactions/${query}`, [query]);
  const apply = (event) => { event.preventDefault(); const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value)); const value = params.toString() ? `?${params}` : ""; history.replaceState({}, "", `/transactions/${value}`); setQuery(value); };
  const clear = () => { setFilters({ q: "", account: "", source: "", start: "", end: "" }); history.replaceState({}, "", "/transactions/"); setQuery(""); };
  return (
    <Page title="All transactions" eyebrow="General journal" crumbs={[{ label: "Transactions" }]} actions={session.permissions.edit_books && <><Button component={RouterLink} to="/transactions/new/split/" variant="outlined" startIcon={<PostAddRounded />}>Split entry</Button><Button component={RouterLink} to="/transactions/new/" variant="contained" startIcon={<AddRounded />}>New transaction</Button></>}>
      <Paper component="form" variant="outlined" onSubmit={apply} sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "minmax(220px, 1.5fr) 1fr", lg: "minmax(240px, 1.4fr) repeat(4, minmax(130px, .7fr)) auto" }, gap: 1.5, alignItems: "center" }}>
          <TextField label="Search" value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} placeholder="Number, description, or contact" />
          <TextField select label="Account" value={filters.account} onChange={(event) => setFilters({ ...filters, account: event.target.value })}><MenuItem value="">All accounts</MenuItem>{options?.accounts.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
          <TextField select label="Source" value={filters.source} onChange={(event) => setFilters({ ...filters, source: event.target.value })}><MenuItem value="">All sources</MenuItem>{options?.choices.entry_sources.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField>
          <DateField label="From" value={filters.start} onChange={(value) => setFilters({ ...filters, start: value })} />
          <DateField label="Through" value={filters.end} onChange={(value) => setFilters({ ...filters, end: value })} />
          <Stack direction="row" spacing={1}><Button type="submit" variant="outlined">Filter</Button><Button onClick={clear} color="inherit">Clear</Button></Stack>
        </Box>
      </Paper>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.entries} emptyTitle="No matching transactions" columns={[
        { key: "date", label: "Date", render: (row) => formatDate(row.date) },
        { key: "number", label: "Entry", render: (row) => <Box><Link component={RouterLink} to={`/transactions/${row.id}/`} fontWeight={700}>{row.number}</Link>{row.version > 1 && <Typography variant="caption" color="text.secondary" display="block">Version {row.version}</Typography>}</Box> },
        { key: "description", label: "Description", sx: { minWidth: 220 }, render: (row) => <Box><Typography variant="body2">{row.description}</Typography>{row.linked_entry && <Typography variant="caption" color="text.secondary">Linked to {row.linked_entry.number}</Typography>}</Box> },
        { key: "accounts", label: "Accounts", sx: { minWidth: 170 }, render: (row) => <Stack>{row.accounts.map((name, index) => <Typography key={`${name}-${index}`} variant="caption" color="text.secondary">{name}</Typography>)}</Stack> },
        { key: "contact", label: "Contact", render: (row) => row.contact?.name || "—" },
        { key: "source", label: "Source", render: (row) => <Chip size="small" label={row.source_label} variant="outlined" /> },
        { key: "total", label: "Amount", align: "right", render: (row) => <Typography fontWeight={600} sx={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.total)}</Typography> },
      ]} />}
    </Page>
  );
}

export function TransactionDetail() {
  const { id } = useParams();
  const { session } = useApp();
  const { data, loading, error } = useApiData(`/api/transactions/${id}/`, [id]);
  if (loading) return <PageLoading title="Transaction" />;
  if (error) return <Page title="Transaction" crumbs={[{ label: "Transactions", to: "/transactions/" }, { label: "Unavailable" }]}><ErrorState error={error} /></Page>;
  const entry = data.entry;
  return (
    <Page title={entry.number} eyebrow={`${entry.source_label} · ${entry.status}`} subtitle={entry.description} crumbs={[{ label: "Transactions", to: "/transactions/" }, { label: entry.number }]} actions={session.permissions.edit_books && entry.can_edit && <Button component={RouterLink} to={`/transactions/${entry.id}/edit/`} variant="outlined" startIcon={<EditRounded />}>Edit transaction</Button>}>
      <Paper variant="outlined" sx={{ mb: 3 }}><Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", lg: "repeat(4, 1fr)" } }}>{[
        ["Date", formatDate(entry.date, { month: "long", day: "numeric", year: "numeric" })], ["Contact", entry.contact?.name || "—"], ["Version", entry.version], ["Created by", entry.created_by],
      ].map(([label, value], index) => <Box key={label} sx={{ p: 2, borderRight: { lg: index < 3 ? 1 : 0 }, borderBottom: { xs: index < 3 ? 1 : 0, lg: 0 }, borderColor: "divider" }}><Typography variant="overline" color="text.secondary">{label}</Typography><Typography>{value}</Typography></Box>)}</Box></Paper>
      {entry.linked_entry && <Alert severity="info" sx={{ mb: 3 }}>Linked to <Link component={RouterLink} to={`/transactions/${entry.linked_entry.id}/`}>{entry.linked_entry.number}</Link></Alert>}
      {entry.memo && <Paper variant="outlined" sx={{ p: 2, mb: 3, borderLeft: 3, borderLeftColor: "secondary.main" }}><Typography variant="overline" color="text.secondary">Memo</Typography><Typography sx={{ whiteSpace: "pre-wrap" }}>{entry.memo}</Typography></Paper>}
      <Typography variant="h2" sx={{ mb: 1.5 }}>Journal entry</Typography>
      <DataTable rows={entry.lines} columns={[
        { key: "account", label: "Account", sx: { minWidth: 180 }, render: (row) => <Link component={RouterLink} to={`/accounts/${row.account.id}/`}>{row.account.display_name}</Link> },
        { key: "description", label: "Description", render: (row) => row.description || "—" },
        { key: "owner", label: "Owner", render: (row) => row.owner?.name || "—" },
        { key: "debit", label: "Debit", align: "right", render: (row) => Number(row.debit) ? formatMoney(row.debit) : "—" },
        { key: "credit", label: "Credit", align: "right", render: (row) => Number(row.credit) ? formatMoney(row.credit) : "—" },
      ]} />
      {entry.attachments.length > 0 && <Box sx={{ mt: 3 }}><Typography variant="h2" sx={{ mb: 1.5 }}>Attachments</Typography><Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">{entry.attachments.map((item) => <Button key={item.id} component="a" href={item.download_url} variant="outlined" startIcon={<AttachFileRounded />}>{item.name}</Button>)}</Stack></Box>}
      {data.audits.length > 0 && <Box sx={{ mt: 3 }}><Typography variant="h2" sx={{ mb: 1.5 }}>Change history</Typography><Stack divider={<Divider flexItem />} component={Paper} variant="outlined">{data.audits.map((item) => <Stack key={item.id} direction={{ xs: "column", sm: "row" }} justifyContent="space-between" sx={{ p: 1.5 }}><Typography fontWeight={600} textTransform="capitalize">{item.action}</Typography><Typography variant="body2" color="text.secondary">{formatDate(item.created_at)} by {item.actor}</Typography></Stack>)}</Stack></Box>}
    </Page>
  );
}

export function TransactionForm() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { options, refreshOptions, notify } = useApp();
  const editing = Boolean(id);
  const split = editing || location.pathname.includes("/split/");
  const detail = useApiData(editing ? `/api/transactions/${id}/` : null, [id]);
  const params = new URLSearchParams(location.search);
  const [values, setValues] = useState({ mode: split ? "split" : "quick", date: localToday(), description: "", amount: "", debit_account_id: params.get("side") === "debit" ? params.get("account") || "" : "", credit_account_id: params.get("side") === "credit" ? params.get("account") || "" : "", contact_id: "", linked_entry_id: "", memo: "", lines: [blankLine(), blankLine()] });
  const [file, setFile] = useState(null);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState({});
  useEffect(() => {
    if (editing && detail.data?.entry) {
      const entry = detail.data.entry;
      setValues({ mode: "split", date: entry.date, description: entry.description, amount: entry.total, debit_account_id: "", credit_account_id: "", contact_id: entry.contact?.id || "", linked_entry_id: entry.linked_entry?.id || "", memo: entry.memo, lines: entry.lines.map((line) => ({ account_id: line.account.id, description: line.description, debit: Number(line.debit) ? line.debit : "", credit: Number(line.credit) ? line.credit : "", owner_id: line.owner?.id || "" })) });
    }
  }, [editing, detail.data]);
  if (editing && detail.loading) return <PageLoading title="Edit transaction" />;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const setLine = (index, key, value) => setValues({ ...values, lines: values.lines.map((line, lineIndex) => lineIndex === index ? { ...line, [key]: value } : line) });
  const submit = async (event) => {
    event.preventDefault(); setSaving(true); setErrors({});
    try {
      const data = formDataFrom(values, file);
      const response = await api(editing ? `/api/transactions/${id}/` : "/api/transactions/", { method: "POST", body: data });
      await refreshOptions(); notify(`Transaction ${response.entry.number} ${editing ? "updated" : "posted"}.`); navigate(`/transactions/${response.entry.id}/`);
    } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); }
  };
  const activeAccounts = options?.accounts.filter((item) => item.is_active) || [];
  const owners = options?.contacts.filter((item) => item.kind === "owner" && item.is_active) || [];
  const title = editing ? `Edit ${detail.data?.entry.number || "transaction"}` : split ? "New split transaction" : "New transaction";
  return (
    <Page title={title} eyebrow="General journal" crumbs={[{ label: "Transactions", to: "/transactions/" }, ...(editing ? [{ label: detail.data?.entry.number, to: `/transactions/${id}/` }, { label: "Edit" }] : [{ label: split ? "New split entry" : "New transaction" }])]} actions={!editing && <Button component={RouterLink} to={split ? "/transactions/new/" : "/transactions/new/split/"} variant="outlined">{split ? "Use simple entry" : "Use split entry"}</Button>}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: split ? 1120 : 820 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)" }, gap: 2 }}>
          <DateField required label="Date" value={values.date} onChange={(value) => setValues({ ...values, date: value })} error={Boolean(fieldError(errors, "date"))} helperText={fieldError(errors, "date")} />
          <TextField select label="Contact" value={values.contact_id} onChange={set("contact_id")}><MenuItem value="">None</MenuItem>{options?.contacts.filter((item) => item.is_active).map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</TextField>
          <TextField required label="Description" value={values.description} onChange={set("description")} error={Boolean(fieldError(errors, "description"))} helperText={fieldError(errors, "description")} sx={{ gridColumn: { sm: "1 / -1" } }} />
          {!split && <><TextField required label="Amount" type="number" value={values.amount} onChange={set("amount")} error={Boolean(fieldError(errors, "amount"))} helperText={fieldError(errors, "amount")} slotProps={{ htmlInput: { min: 0.01, step: 0.01 } }} /><Box />
            <TextField required select label="Debit account" value={values.debit_account_id} onChange={set("debit_account_id")}><MenuItem value="">Choose account</MenuItem>{activeAccounts.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
            <TextField required select label="Credit account" value={values.credit_account_id} onChange={set("credit_account_id")}><MenuItem value="">Choose account</MenuItem>{activeAccounts.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField></>}
          <TextField select label="Linked entry" value={values.linked_entry_id} onChange={set("linked_entry_id")} helperText="Use when this settles or relates to an earlier entry."><MenuItem value="">None</MenuItem>{options?.entries.filter((item) => String(item.id) !== String(id)).map((item) => <MenuItem key={item.id} value={item.id}>{item.number} · {item.description}</MenuItem>)}</TextField>
          <TextField multiline minRows={2} label="Memo" value={values.memo} onChange={set("memo")} />
        </Box>
        {split && <Box sx={{ mt: 3 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}><Typography variant="h2">Journal lines</Typography><Button variant="outlined" size="small" startIcon={<AddRounded />} onClick={() => setValues({ ...values, lines: [...values.lines, blankLine()] })}>Add line</Button></Stack>
          <Stack spacing={1.5}>{values.lines.map((line, index) => <Paper key={index} variant="outlined" sx={{ p: 1.5 }}><Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr 1fr", lg: "minmax(210px, 1.3fr) minmax(180px, 1fr) 120px 120px minmax(150px, .8fr) 40px" }, gap: 1.25, alignItems: "start" }}>
            <TextField required select label="Account" value={line.account_id} onChange={(event) => setLine(index, "account_id", event.target.value)} sx={{ gridColumn: { xs: "1 / -1", lg: "auto" } }}><MenuItem value="">Choose account</MenuItem>{activeAccounts.map((item) => <MenuItem key={item.id} value={item.id}>{item.display_name}</MenuItem>)}</TextField>
            <TextField label="Line description" value={line.description} onChange={(event) => setLine(index, "description", event.target.value)} sx={{ gridColumn: { xs: "1 / -1", lg: "auto" } }} />
            <TextField label="Debit" type="number" value={line.debit} onChange={(event) => setLine(index, "debit", event.target.value)} slotProps={{ htmlInput: { min: 0, step: 0.01 } }} />
            <TextField label="Credit" type="number" value={line.credit} onChange={(event) => setLine(index, "credit", event.target.value)} slotProps={{ htmlInput: { min: 0, step: 0.01 } }} />
            <TextField select label="Owner" value={line.owner_id} onChange={(event) => setLine(index, "owner_id", event.target.value)} sx={{ gridColumn: { xs: "1 / -1", lg: "auto" } }}><MenuItem value="">None</MenuItem>{owners.map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}</TextField>
            <Tooltip title="Remove line"><span><IconButton aria-label="Remove line" disabled={values.lines.length <= 2} onClick={() => setValues({ ...values, lines: values.lines.filter((_, lineIndex) => lineIndex !== index) })}><DeleteOutlineRounded /></IconButton></span></Tooltip>
          </Box></Paper>)}</Stack>
        </Box>}
        <Stack direction={{ xs: "column", sm: "row" }} alignItems={{ xs: "stretch", sm: "center" }} justifyContent="space-between" spacing={2} sx={{ mt: 3 }}>
          <Button component="label" variant="outlined" startIcon={<AttachFileRounded />}>{file ? file.name : "Add attachment"}<input hidden type="file" accept="application/pdf,image/png,image/jpeg,image/webp" onChange={(event) => setFile(event.target.files?.[0] || null)} /></Button>
          <FormActions saving={saving} submitLabel="Post transaction" cancelTo={editing ? `/transactions/${id}/` : "/transactions/"} />
        </Stack>
      </Paper>
    </Page>
  );
}
