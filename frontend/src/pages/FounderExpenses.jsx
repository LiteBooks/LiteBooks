import { useState } from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, IconButton,
  Link, MenuItem, Paper, Stack, Switch, Table, TableBody, TableCell, TableHead, TableRow, TextField, Tooltip, Typography,
} from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import AttachFileRounded from "@mui/icons-material/AttachFileRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import DownloadRounded from "@mui/icons-material/DownloadRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import UndoRounded from "@mui/icons-material/UndoRounded";
import UploadFileRounded from "@mui/icons-material/UploadFileRounded";
import { api } from "../api";
import { DataTable, DateField, ErrorState, fieldError, formatDate, formatMoney, LoadingState, Page, SubmitButton, useApiData } from "../components/Common";
import { useApp } from "../context";

const BANNER = "Not company expenses. Not included in company books or tax returns.";
const blank = { owner_id: "", date_paid: "", vendor: "", description: "", amount: "", business_purpose: "", property_stays_personal: false, category: "", receipt_url: "" };

function queryString(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== "" && value !== null && value !== undefined && value !== false) search.set(key, value === true ? "1" : value); });
  const text = search.toString();
  return text ? `?${text}` : "";
}

function Flags({ row }) {
  return (
    <Stack sx={{ flexWrap: "wrap" }} direction="row" spacing={0.5} useFlexGap>
      {row.late && <Tooltip title={`Logged ${row.days_to_log} days after payment (more than 30).`}><Chip size="small" color="warning" variant="outlined" label="Late" /></Tooltip>}
      {row.special_window_late && <Tooltip title="Paid in the special logging window and logged after its deadline."><Chip size="small" color="warning" label="Past window deadline" /></Tooltip>}
      {row.approval_reminder && <Tooltip title={row.approval_note}><Chip size="small" color="info" variant="outlined" label="$250+ review" /></Tooltip>}
      {!row.has_receipt && <Chip size="small" variant="outlined" label="No receipt" />}
    </Stack>
  );
}

function EntryDialog({ entry, owners, categories, onClose, onSaved }) {
  const { notify } = useApp();
  const editing = entry !== "new";
  const [values, setValues] = useState(editing ? { ...blank, ...entry, category: entry.category || "", owner_id: entry.owner_id } : blank);
  const [files, setFiles] = useState([]);
  const [receipts, setReceipts] = useState(editing ? entry.receipts : []);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const save = async (event) => {
    event.preventDefault(); setSaving(true); setErrors({});
    const body = new FormData();
    ["owner_id", "date_paid", "vendor", "description", "amount", "business_purpose", "category", "receipt_url"].forEach((key) => body.append(key, values[key] ?? ""));
    body.append("property_stays_personal", values.property_stays_personal ? "true" : "false");
    files.forEach((file) => body.append("receipts", file));
    try {
      await api(editing ? `/api/founder-expenses/${entry.id}/` : "/api/founder-expenses/", { method: "POST", body });
      notify(editing ? "Entry updated." : "Entry logged."); onSaved();
    } catch (failure) { setErrors({ ...(failure.fields || {}), __all__: failure.message }); } finally { setSaving(false); }
  };
  const removeReceipt = async (receipt) => {
    try { const response = await api(`/api/founder-expenses/receipts/${receipt.id}/`, { method: "DELETE" }); setReceipts(response.entry.receipts); notify("Receipt removed."); }
    catch (failure) { notify(failure.message, "error"); }
  };
  const field = (key) => ({ error: Boolean(fieldError(errors, key)), helperText: fieldError(errors, key) });
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="md" component="form" onSubmit={save}>
      <DialogTitle>{editing ? "Edit founder expense" : "Log a founder expense"}</DialogTitle>
      <DialogContent>
        <Alert severity="info" sx={{ mb: 2 }}>{BANNER} Logging it here does not post anything to the ledger.</Alert>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)" }, gap: 2, pt: 0.5 }}>
          <TextField select required size="small" label="Owner who paid" value={values.owner_id} onChange={set("owner_id")} {...field("owner_id")}>{owners.map((owner) => <MenuItem key={owner.id} value={owner.id}>{owner.name}</MenuItem>)}</TextField>
          <DateField required label="Date paid" value={values.date_paid} onChange={(value) => setValues({ ...values, date_paid: value })} disableFuture {...field("date_paid")} />
          <TextField required size="small" label="Vendor" value={values.vendor} onChange={set("vendor")} {...field("vendor")} />
          <TextField required size="small" label="Amount" slotProps={{ htmlInput: { inputMode: "decimal" } }} value={values.amount} onChange={set("amount")} {...field("amount")} />
          <TextField required size="small" label="Description" value={values.description} onChange={set("description")} sx={{ gridColumn: { sm: "1 / -1" } }} {...field("description")} />
          <TextField required multiline minRows={2} size="small" label="Business purpose" value={values.business_purpose} onChange={set("business_purpose")} sx={{ gridColumn: { sm: "1 / -1" } }} {...field("business_purpose")} />
          <TextField select size="small" label="Category (label only)" value={values.category} onChange={set("category")} {...field("category")}><MenuItem value="">Uncategorized</MenuItem>{categories.map((account) => <MenuItem key={account.code} value={account.code}>{account.code} · {account.name}</MenuItem>)}</TextField>
          <TextField size="small" label="Receipt link (optional)" placeholder="https://" value={values.receipt_url} onChange={set("receipt_url")} {...field("receipt_url")} />
          <FormControlLabel control={<Switch checked={values.property_stays_personal} onChange={(event) => setValues({ ...values, property_stays_personal: event.target.checked })} />} label="Purchased property stays personal" sx={{ gridColumn: { sm: "1 / -1" } }} />
        </Box>
        <Typography variant="subtitle2" sx={{ mt: 2, mb: 1 }}>Receipts</Typography>
        {receipts.length > 0 && <Stack spacing={0.5} sx={{ mb: 1 }}>{receipts.map((receipt) => <Stack sx={{ alignItems: "center" }} key={receipt.id} direction="row" spacing={1}><AttachFileRounded fontSize="small" color="action" /><Link href={receipt.download_url}>{receipt.name}</Link><IconButton size="small" aria-label={`Remove ${receipt.name}`} onClick={() => removeReceipt(receipt)}><DeleteOutlineRounded fontSize="small" /></IconButton></Stack>)}</Stack>}
        <Button component="label" variant="outlined" size="small" startIcon={<AttachFileRounded />}>Add receipt files<input hidden multiple type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(event) => setFiles([...files, ...event.target.files])} /></Button>
        {files.length > 0 && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>To upload: {files.map((file) => file.name).join(", ")}</Typography>}
        {fieldError(errors, "receipts") && <Typography variant="body2" color="error" sx={{ mt: 1 }}>{fieldError(errors, "receipts")}</Typography>}
      </DialogContent>
      <DialogActions><Button onClick={onClose} color="inherit">Cancel</Button><SubmitButton saving={saving}>{editing ? "Save changes" : "Log expense"}</SubmitButton></DialogActions>
    </Dialog>
  );
}

function WithdrawDialog({ entry, onClose, onSaved }) {
  const { notify } = useApp();
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const save = async (event) => {
    event.preventDefault(); setSaving(true); setErrors({});
    try { await api(`/api/founder-expenses/${entry.id}/withdraw/`, { method: "POST", body: { reason } }); notify("Entry withdrawn."); onSaved(); }
    catch (failure) { setErrors({ ...(failure.fields || {}), __all__: failure.message }); } finally { setSaving(false); }
  };
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm" component="form" onSubmit={save}>
      <DialogTitle>Withdraw this entry?</DialogTitle>
      <DialogContent>
        <Typography color="text.secondary" sx={{ mb: 2 }}>{entry.vendor}, {formatMoney(entry.amount)} paid {formatDate(entry.date_paid)}. It stays in the log with your reason but no longer counts toward totals. This cannot be undone.</Typography>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <TextField required fullWidth multiline minRows={2} label="Reason" value={reason} onChange={(event) => setReason(event.target.value)} error={Boolean(fieldError(errors, "withdrawn_reason"))} helperText={fieldError(errors, "withdrawn_reason")} />
      </DialogContent>
      <DialogActions><Button onClick={onClose} color="inherit">Cancel</Button><SubmitButton saving={saving} color="warning">Withdraw</SubmitButton></DialogActions>
    </Dialog>
  );
}

function ImportDialog({ onClose, onSaved }) {
  const { notify } = useApp();
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (commit) => {
    setBusy(true); setError("");
    const body = new FormData(); body.append("file", file); body.append("commit", commit ? "true" : "false");
    try {
      const result = await api("/api/founder-expenses/import/", { method: "POST", body });
      setPreview(result);
      if (result.committed) { notify(result.outcome); onSaved(); }
    } catch (failure) { setError(failure.message); } finally { setBusy(false); }
  };
  const ready = preview && !preview.file_errors.length && !preview.counts.errors;
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="lg">
      <DialogTitle>Import founder expenses from CSV</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Columns: owner email, date_paid (YYYY-MM-DD), vendor, description, amount, business_purpose, property_stays_personal (Y/N), receipt_url. Preview first; a file with any error is not imported at all.</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ alignItems: { sm: "center" }, mb: 2 }}>
          <Button component="label" variant="outlined" startIcon={<UploadFileRounded />}>Choose CSV<input hidden type="file" accept=".csv,text/csv" onChange={(event) => { setFile(event.target.files[0] || null); setPreview(null); }} /></Button>
          <Typography color="text.secondary">{file ? file.name : "No file chosen"}</Typography>
          <Box sx={{ flex: 1 }} />
          <Button variant="outlined" disabled={!file || busy} onClick={() => send(false)}>Preview</Button>
        </Stack>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        {preview && <Alert severity={preview.committed ? "success" : ready ? "info" : "error"} sx={{ mb: 2 }}>{preview.outcome}{preview.file_errors.map((item) => <div key={item}>{item}</div>)}</Alert>}
        {preview?.rows.length > 0 && <Paper variant="outlined" sx={{ overflowX: "auto" }}><Table size="small">
          <TableHead><TableRow><TableCell>Line</TableCell><TableCell>Owner email</TableCell><TableCell>Date paid</TableCell><TableCell>Vendor</TableCell><TableCell align="right">Amount</TableCell><TableCell>Personal</TableCell><TableCell sx={{ minWidth: 260 }}>Result</TableCell></TableRow></TableHead>
          <TableBody>{preview.rows.map((row) => <TableRow key={row.line} sx={row.errors.length ? { bgcolor: "rgba(211,47,47,.06)" } : undefined}>
            <TableCell>{row.line}</TableCell><TableCell>{row.values.owner_email}</TableCell><TableCell>{row.values.date_paid}</TableCell><TableCell>{row.values.vendor}</TableCell><TableCell align="right">{row.values.amount}</TableCell><TableCell>{row.values.property_stays_personal}</TableCell>
            <TableCell>{row.errors.length ? row.errors.map((item) => <Typography key={item} variant="body2" color="error">{item}</Typography>) : <><Typography variant="body2" color="success.main">Ready</Typography>{row.flags.map((item) => <Typography sx={{ display: "block" }} key={item} variant="caption" color="text.secondary">{item}</Typography>)}</>}</TableCell>
          </TableRow>)}</TableBody>
        </Table></Paper>}
      </DialogContent>
      <DialogActions><Button onClick={onClose} color="inherit">{preview?.committed ? "Close" : "Cancel"}</Button>{!preview?.committed && <Button variant="contained" disabled={!ready || busy} onClick={() => send(true)}>Import {ready ? preview.counts.rows : ""} entries</Button>}</DialogActions>
    </Dialog>
  );
}

function WindowSettings({ settings, onSaved }) {
  const { notify } = useApp();
  const [values, setValues] = useState({ special_window_start: settings.special_window_start || "", special_window_end: settings.special_window_end || "", special_window_deadline: settings.special_window_deadline || "" });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const save = async (event) => {
    event.preventDefault(); setSaving(true); setErrors({});
    try { await api("/api/founder-expenses/settings/", { method: "POST", body: { special_window_start: values.special_window_start || null, special_window_end: values.special_window_end || null, special_window_deadline: values.special_window_deadline || null } }); notify("Logging window saved."); onSaved(); }
    catch (failure) { setErrors({ ...(failure.fields || {}), __all__: failure.message }); } finally { setSaving(false); }
  };
  const date = (key, label) => <DateField label={label} value={values[key]} onChange={(value) => setValues({ ...values, [key]: value })} error={Boolean(fieldError(errors, key))} helperText={fieldError(errors, key)} />;
  return (
    <Paper component="form" onSubmit={save} variant="outlined" sx={{ p: { xs: 2, sm: 2.5 }, mt: 4, maxWidth: 820 }}>
      <Typography variant="h3" sx={{ mb: 0.5 }}>Special logging window</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Payments made from the first to the last date must be logged by the deadline. Entries logged later are flagged, not rejected. Leave all three empty for no window.</Typography>
      {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(3, 1fr)" }, gap: 2 }}>{date("special_window_start", "First payment date")}{date("special_window_end", "Last payment date")}{date("special_window_deadline", "Log by")}</Box>
      <Stack direction="row" sx={{ justifyContent: "flex-end", mt: 2 }}><SubmitButton saving={saving}>Save window</SubmitButton></Stack>
    </Paper>
  );
}

function Summary({ year, ownerId }) {
  const { data, loading, error } = useApiData(`/api/founder-expenses/summary/${queryString({ year, owner_id: ownerId })}`, [year, ownerId]);
  if (loading) return <LoadingState rows={2} />;
  if (error) return <ErrorState error={error} />;
  if (!data.owners.length) return <Typography color="text.secondary" sx={{ mb: 3 }}>No logged entries paid in {year}.</Typography>;
  return (
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "repeat(2, 1fr)" }, gap: 2, mb: 3 }}>
      {data.owners.map((owner) => <Card key={owner.owner_id} variant="outlined"><CardContent>
        <Stack sx={{ justifyContent: "space-between", alignItems: "baseline" }} direction="row"><Typography variant="h3">{owner.owner_name}</Typography><Typography sx={{ fontSize: 24, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{formatMoney(owner.total)}</Typography></Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{owner.count} entr{owner.count === 1 ? "y" : "ies"} in {data.year} · {owner.late_count} late · {owner.missing_receipts_count} missing {owner.missing_receipts_count === 1 ? "receipt" : "receipts"}</Typography>
        {owner.by_category.map((row) => <Stack key={row.category || "none"} direction="row" sx={{ justifyContent: "space-between", py: 0.25 }}><Typography variant="body2">{row.category ? `${row.category} · ${row.category_name || "Unknown account"}` : "Uncategorized"} ({row.count})</Typography><Typography variant="body2" sx={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.total)}</Typography></Stack>)}
        {owner.missing_receipts.length > 0 && <Box sx={{ mt: 1.5 }}><Typography variant="subtitle2">Missing receipts</Typography>{owner.missing_receipts.map((row) => <Typography key={row.id} variant="body2" color="text.secondary">{formatDate(row.date_paid)} · {row.vendor} · {formatMoney(row.amount)}</Typography>)}</Box>}
      </CardContent></Card>)}
    </Box>
  );
}

export default function FounderExpensesPage() {
  const { session, options } = useApp();
  const canEdit = session.permissions.edit_books;
  const thisYear = new Date().getFullYear();
  const [ownerId, setOwnerId] = useState("");
  const [year, setYear] = useState(String(thisYear));
  const [includeWithdrawn, setIncludeWithdrawn] = useState(false);
  const [editing, setEditing] = useState(null);
  const [withdrawing, setWithdrawing] = useState(null);
  const [importing, setImporting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const filters = { owner_id: ownerId, year, include_withdrawn: includeWithdrawn };
  const { data, loading, error, reload } = useApiData(`/api/founder-expenses/${queryString(filters)}`, [ownerId, year, includeWithdrawn, refresh]);
  const owners = options.contacts.filter((contact) => contact.kind === "owner");
  const categories = options.accounts.filter((account) => account.type === "expense");
  const years = Array.from({ length: 8 }, (_, index) => String(thisYear - index));
  const saved = () => { setEditing(null); setWithdrawing(null); setRefresh((value) => value + 1); };

  const columns = [
    { key: "date_paid", label: "Paid", sx: { whiteSpace: "nowrap" }, render: (row) => formatDate(row.date_paid) },
    { key: "owner", label: "Owner", render: (row) => row.owner_name },
    { key: "vendor", label: "Vendor and purpose", sx: { minWidth: 200 }, render: (row) => <><Typography sx={{ fontWeight: 700 }} sx={row.status === "withdrawn" ? { textDecoration: "line-through" } : undefined}>{row.vendor}</Typography><Typography variant="body2">{row.description}</Typography><Typography variant="caption" color="text.secondary">{row.business_purpose}{row.property_stays_personal ? " · Property stays personal" : ""}</Typography>{row.status === "withdrawn" && <Typography sx={{ display: "block" }} variant="caption" color="warning.main">Withdrawn: {row.withdrawn_reason}</Typography>}</> },
    { key: "category", label: "Category", render: (row) => row.category ? `${row.category} · ${row.category_name || "?"}` : "—" },
    { key: "amount", label: "Amount", align: "right", render: (row) => <Typography sx={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.amount)}</Typography> },
    { key: "receipts", label: "Receipt", render: (row) => <Stack spacing={0.25}>{row.receipts.map((item) => <Link key={item.id} href={item.download_url} variant="body2">{item.name}</Link>)}{row.receipt_url && <Link href={row.receipt_url} target="_blank" rel="noreferrer noopener" variant="body2">Link</Link>}{!row.has_receipt && "—"}</Stack> },
    { key: "logged", label: "Logged", sx: { whiteSpace: "nowrap" }, render: (row) => <><Typography variant="body2">{formatDate(row.logged_at)}</Typography><Typography variant="caption" color="text.secondary">{row.days_to_log} {row.days_to_log === 1 ? "day" : "days"} after{row.source === "csv_import" ? " · CSV" : ""}</Typography></> },
    { key: "flags", label: "Flags", sx: { minWidth: 130 }, render: (row) => <Flags row={row} /> },
    ...(canEdit ? [{ key: "actions", label: "", align: "right", render: (row) => row.status === "logged" && <Stack sx={{ justifyContent: "flex-end" }} direction="row" spacing={0.5}><Button size="small" startIcon={<EditRounded />} onClick={() => setEditing(row)}>Edit</Button><Button size="small" color="warning" startIcon={<UndoRounded />} onClick={() => setWithdrawing(row)}>Withdraw</Button></Stack> }] : []),
  ];

  return (
    <Page title="Founder Expense Log" eyebrow="Outside the books" subtitle="Business expenses owners paid personally and agreed not to be reimbursed for." crumbs={[{ label: "Founder Expense Log" }]} actions={<>
      <Button component="a" href={`/api/founder-expenses/export/${queryString(filters)}`} startIcon={<DownloadRounded />} color="inherit">Export CSV</Button>
      {canEdit && <Button variant="outlined" startIcon={<UploadFileRounded />} onClick={() => setImporting(true)}>Import CSV</Button>}
      {canEdit && <Button variant="contained" startIcon={<AddRounded />} onClick={() => setEditing("new")} disabled={!owners.length}>Log expense</Button>}
    </>}>
      <Alert severity="warning" variant="filled" sx={{ mb: 3, fontWeight: 600 }}>{BANNER}</Alert>
      {!owners.length && <Alert severity="info" sx={{ mb: 3 }}>Add an owner contact before logging founder expenses.</Alert>}
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ alignItems: { sm: "center" }, mb: 3 }}>
        <TextField select size="small" label="Owner" value={ownerId} onChange={(event) => setOwnerId(event.target.value)} sx={{ minWidth: 200 }}><MenuItem value="">All owners</MenuItem>{owners.map((owner) => <MenuItem key={owner.id} value={owner.id}>{owner.name}</MenuItem>)}</TextField>
        <TextField select size="small" label="Year paid" value={year} onChange={(event) => setYear(event.target.value)} sx={{ minWidth: 140 }}><MenuItem value="">All years</MenuItem>{years.map((item) => <MenuItem key={item} value={item}>{item}</MenuItem>)}</TextField>
        <FormControlLabel control={<Switch checked={includeWithdrawn} onChange={(event) => setIncludeWithdrawn(event.target.checked)} />} label="Show withdrawn" />
      </Stack>
      {year && <><Typography variant="h2" sx={{ mb: 1.5 }}>{year} summary</Typography><Summary key={refresh} year={year} ownerId={ownerId} /></>}
      <Typography variant="h2" sx={{ mb: 1.5 }}>Entries</Typography>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.entries} columns={columns} emptyTitle="No founder expenses" emptyDetail="Nothing matches these filters." />}
      {session.permissions.administer && data?.settings && <WindowSettings key={JSON.stringify(data.settings)} settings={data.settings} onSaved={reload} />}
      {editing && <EntryDialog entry={editing} owners={owners} categories={categories} onClose={() => setEditing(null)} onSaved={saved} />}
      {withdrawing && <WithdrawDialog entry={withdrawing} onClose={() => setWithdrawing(null)} onSaved={saved} />}
      {importing && <ImportDialog onClose={() => setImporting(false)} onSaved={() => setRefresh((value) => value + 1)} />}
    </Page>
  );
}
