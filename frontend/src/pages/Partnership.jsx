import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, MenuItem, Paper, Stack,
  TableCell, TableRow, TextField, Typography,
} from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import DeleteOutlineRounded from "@mui/icons-material/DeleteOutlineRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import EventBusyRounded from "@mui/icons-material/EventBusyRounded";
import { api } from "../api";
import { DataTable, DateField, ErrorState, fieldError, formatDate, formatMoney, LoadingState, Page, PageLoading, SubmitButton, useApiData } from "../components/Common";
import { useApp } from "../context";

const blankPeriod = { label: "", effective_start: "", effective_end: "", percentage: "", notes: "", source_document: "" };

function formatPct(value) {
  return `${Number(value).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 4 })}%`;
}

// One table of dated percentages, either an owner's interests ("owner") or the unissued pools ("reserved").
function InterestTable({ kind, ownerId, title, detail }) {
  const { session, notify } = useApp();
  const canEdit = session.permissions.administer;
  const listUrl = kind === "owner" ? `/api/ownership/owners/?owner_id=${ownerId}` : "/api/ownership/reserved/";
  const baseUrl = kind === "owner" ? "/api/ownership/owners/" : "/api/ownership/reserved/";
  const { data, loading, error, reload } = useApiData(listUrl, [listUrl]);
  const [editing, setEditing] = useState(null);
  const [values, setValues] = useState(blankPeriod);
  const [ending, setEnding] = useState(null);
  const [endDate, setEndDate] = useState("");
  const [removing, setRemoving] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);

  const open = (row) => { setErrors({}); setEditing(row || "new"); setValues(row ? { ...blankPeriod, ...row, effective_end: row.effective_end || "" } : blankPeriod); };
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const fail = (failure) => setErrors({ ...(failure.fields || {}), __all__: failure.message });

  const save = async (event) => {
    event.preventDefault();
    setSaving(true); setErrors({});
    const body = { ...values, effective_end: values.effective_end || null, ...(kind === "owner" ? { owner_id: ownerId } : {}) };
    try {
      await api(editing === "new" ? baseUrl : `${baseUrl}${editing.id}/`, { method: "POST", body });
      notify(editing === "new" ? "Ownership period added." : "Ownership period updated.");
      setEditing(null); await reload();
    } catch (failure) { fail(failure); } finally { setSaving(false); }
  };
  const end = async (event) => {
    event.preventDefault();
    setSaving(true); setErrors({});
    try {
      await api(`/api/ownership/${kind}/${ending.id}/end/`, { method: "POST", body: { effective_end: endDate } });
      notify("Ownership period ended."); setEnding(null); await reload();
    } catch (failure) { fail(failure); } finally { setSaving(false); }
  };
  const remove = async () => {
    try { await api(`${baseUrl}${removing.id}/`, { method: "DELETE" }); notify("Ownership period deleted."); setRemoving(null); await reload(); } catch (failure) { notify(failure.message, "error"); }
  };

  const columns = [
    ...(kind === "reserved" ? [{ key: "label", label: "Interest", render: (row) => <Typography sx={{ fontWeight: 700 }}>{row.label}</Typography> }] : []),
    { key: "percentage", label: "Percentage", align: "right", render: (row) => <Typography sx={{ fontWeight: kind === "owner" ? 700 : 400 }}>{formatPct(row.percentage)}</Typography> },
    { key: "effective_start", label: "From", render: (row) => formatDate(row.effective_start) },
    { key: "effective_end", label: "Through", render: (row) => row.effective_end ? formatDate(row.effective_end) : "Open" },
    { key: "source_document", label: "Source", render: (row) => row.source_document || "—" },
    { key: "notes", label: "Notes", sx: { maxWidth: 260 }, render: (row) => row.notes || "—" },
    ...(canEdit ? [{ key: "actions", label: "", align: "right", render: (row) => (
      <Stack sx={{ justifyContent: "flex-end" }} direction="row" spacing={0.5}>
        <Button size="small" startIcon={<EditRounded />} onClick={() => open(row)}>Edit</Button>
        {!row.effective_end && <Button size="small" startIcon={<EventBusyRounded />} onClick={() => { setErrors({}); setEndDate(""); setEnding(row); }}>End</Button>}
        <Button size="small" color="error" startIcon={<DeleteOutlineRounded />} onClick={() => setRemoving(row)}>Delete</Button>
      </Stack>
    ) }] : []),
  ];

  return (
    <Box>
      <Stack direction="row" spacing={2} sx={{ alignItems: "flex-end", justifyContent: "space-between", mb: 1.5 }}>
        <Box><Typography variant="h2">{title}</Typography>{detail && <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{detail}</Typography>}</Box>
        {canEdit && <Button variant="outlined" startIcon={<AddRounded />} onClick={() => open(null)} sx={{ flexShrink: 0, whiteSpace: "nowrap" }}>Add period</Button>}
      </Stack>
      {loading ? <LoadingState rows={2} /> : error ? <ErrorState error={error} /> : <DataTable rows={data.periods} columns={columns} emptyTitle="No ownership recorded" emptyDetail={kind === "owner" ? "Add the percentage this owner holds and the date it took effect." : "Add unissued interests, such as an option pool, that belong to no owner."} />}

      <Dialog open={Boolean(editing)} onClose={() => setEditing(null)} fullWidth maxWidth="sm">
        <Box component="form" onSubmit={save}>
          <DialogTitle>{editing === "new" ? "Add ownership period" : "Edit ownership period"}</DialogTitle>
          <DialogContent>
            {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
            <Stack spacing={2} sx={{ pt: 1 }}>
              {kind === "reserved" && <TextField required label="Name" placeholder="Reserved pool" value={values.label} onChange={set("label")} error={Boolean(fieldError(errors, "label"))} helperText={fieldError(errors, "label")} />}
              <TextField required label="Percentage" value={values.percentage} onChange={set("percentage")} slotProps={{ htmlInput: { inputMode: "decimal" } }} error={Boolean(fieldError(errors, "percentage"))} helperText={fieldError(errors, "percentage") || "0 to 100, up to four decimal places"} />
              <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" }, gap: 2 }}>
                <DateField required label="Effective from" value={values.effective_start} onChange={(value) => setValues({ ...values, effective_start: value })} error={Boolean(fieldError(errors, "effective_start"))} helperText={fieldError(errors, "effective_start")} />
                <DateField label="Effective through" value={values.effective_end} onChange={(value) => setValues({ ...values, effective_end: value })} error={Boolean(fieldError(errors, "effective_end"))} helperText={fieldError(errors, "effective_end") || "Leave empty while still held"} />
              </Box>
              <TextField label="Source document" placeholder="Operating Agreement v2" value={values.source_document} onChange={set("source_document")} />
              <TextField multiline minRows={2} label="Notes" value={values.notes} onChange={set("notes")} />
            </Stack>
          </DialogContent>
          <DialogActions><Button onClick={() => setEditing(null)} color="inherit">Cancel</Button><SubmitButton saving={saving}>Save period</SubmitButton></DialogActions>
        </Box>
      </Dialog>

      <Dialog open={Boolean(ending)} onClose={() => setEnding(null)} fullWidth maxWidth="xs">
        <Box component="form" onSubmit={end}>
          <DialogTitle>End this period</DialogTitle>
          <DialogContent>
            <DialogContentText sx={{ mb: 2 }}>The {ending && formatPct(ending.percentage)} interest stops after the last day you choose. Add a new period for any percentage that replaces it.</DialogContentText>
            {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
            <DateField required label="Last effective day" value={endDate} onChange={setEndDate} error={Boolean(fieldError(errors, "effective_end"))} helperText={fieldError(errors, "effective_end")} />
          </DialogContent>
          <DialogActions><Button onClick={() => setEnding(null)} color="inherit">Cancel</Button><SubmitButton saving={saving}>End period</SubmitButton></DialogActions>
        </Box>
      </Dialog>

      <Dialog open={Boolean(removing)} onClose={() => setRemoving(null)}>
        <DialogTitle>Delete this ownership period?</DialogTitle>
        <DialogContent><DialogContentText>Use this only for a period entered by mistake. To record that an interest stopped, end it instead so the history stays intact.</DialogContentText></DialogContent>
        <DialogActions><Button onClick={() => setRemoving(null)} color="inherit">Cancel</Button><Button onClick={remove} variant="contained" color="error">Delete period</Button></DialogActions>
      </Dialog>
    </Box>
  );
}

export function OwnershipHistory({ ownerId }) {
  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, sm: 3 }, mt: 3, maxWidth: 1100 }}>
      <InterestTable kind="owner" ownerId={ownerId} title="Ownership history" detail="This owner's percentage over time, used to split income among owners when ownership changes." />
    </Paper>
  );
}

export function PartnershipSettingsPage() {
  const { session, notify } = useApp();
  const canEdit = session.permissions.administer;
  const detail = useApiData("/api/partnership/settings/", []);
  const [values, setValues] = useState(null);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (detail.data?.settings) setValues({ ...detail.data.settings, written_statement_date: detail.data.settings.written_statement_date || "" }); }, [detail.data]);
  if (detail.loading || (!values && !detail.error)) return <PageLoading title="Partnership" />;
  if (detail.error) return <Page title="Partnership"><ErrorState error={detail.error} /></Page>;
  const choices = detail.data.settings.choices;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const select = (key, label, helper) => (
    <TextField select label={label} value={values[key]} onChange={set(key)} disabled={!canEdit} error={Boolean(fieldError(errors, key))} helperText={fieldError(errors, key) || helper}>
      {choices[key].map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}
    </TextField>
  );
  const submit = async (event) => {
    event.preventDefault();
    setSaving(true); setErrors({});
    const { choices: _, updated_at: __, ...body } = values;
    try { await api("/api/partnership/settings/", { method: "POST", body: { ...body, written_statement_date: body.written_statement_date || null } }); notify("Partnership settings saved."); await detail.reload(); } catch (failure) { setErrors({ ...(failure.fields || {}), __all__: failure.message }); } finally { setSaving(false); }
  };
  return (
    <Page title="Partnership" eyebrow="Company settings" subtitle="How income is split among owners when ownership changes during the year (IRC §706(d)). These settings drive the partner allocation and capital account reports; nothing is posted to the ledger." crumbs={[{ label: "Settings" }, { label: "Partnership" }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 1100 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        {values.allocation_method === "proration" && values.convention !== "calendar_day" && <Alert severity="warning" sx={{ mb: 2 }}>Treas. Reg. §1.706-4 pairs the proration method with the calendar day convention. Confirm this combination with your tax advisor.</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2 }}>
          {select("allocation_method", "Allocation method", "Interim closing allocates each segment's actual income; proration spreads the year's income by days.")}
          {select("convention", "Convention", "When a mid-month change is treated as taking effect.")}
          {select("change_day_belongs_to", "Activity on a change date goes to", "Calendar day convention only: who gets income booked on the day new percentages take effect.")}
          {select("unissued_allocation_policy", "Unissued interests", "Reserved pools and any unissued percentage are shared by issued owners in proportion to their holdings.")}
        </Box>
        <Typography variant="h2" sx={{ mt: 3, mb: 1 }}>Written statement</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>The partnership's records of the method and convention it chose, and the date that choice was made.</Typography>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "220px 1fr" }, gap: 2, alignItems: "start" }}>
          <DateField label="Statement date" value={values.written_statement_date} onChange={(value) => setValues({ ...values, written_statement_date: value })} disabled={!canEdit} error={Boolean(fieldError(errors, "written_statement_date"))} helperText={fieldError(errors, "written_statement_date")} />
          <TextField multiline minRows={3} label="Statement" value={values.written_statement} onChange={set("written_statement")} disabled={!canEdit} />
        </Box>
        {canEdit && <Stack direction="row" sx={{ justifyContent: "flex-end", mt: 3 }}><SubmitButton saving={saving}>Save settings</SubmitButton></Stack>}
      </Paper>
      <Paper variant="outlined" sx={{ p: { xs: 2, sm: 3 }, mt: 3, maxWidth: 1100 }}>
        <InterestTable kind="reserved" title="Reserved interests" detail="Authorized interests that belong to no owner, such as an option pool. Issued and reserved interests may not exceed 100% on any day." />
      </Paper>
    </Page>
  );
}

function Warnings({ warnings }) {
  return warnings?.length ? <Stack spacing={1} sx={{ mb: 2 }}>{warnings.map((text) => <Alert key={text} severity="warning">{text}</Alert>)}</Stack> : null;
}

const labels = { interim_closing: "Interim closing", proration: "Proration", calendar_day: "Calendar day", semi_monthly: "Semi-monthly", monthly: "Monthly", outgoing: "outgoing owners", incoming: "incoming owners" };

export function PartnerAllocationBody({ data }) {
  return (
    <Box>
      <Warnings warnings={data.warnings} />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {labels[data.method]} method, {labels[data.convention]?.toLowerCase()} convention{data.convention === "calendar_day" ? `; activity on a change date goes to ${labels[data.change_day_belongs_to]}` : ""}. Unissued interests are shared pro rata among issued owners. This report posts nothing.
      </Typography>
      {data.segments.map((segment) => (
        <Box key={segment.start} sx={{ mb: 3 }}>
          <Stack direction={{ xs: "column", sm: "row" }} sx={{ justifyContent: "space-between", pb: 1, mb: 1, borderBottom: 2, borderColor: "text.secondary" }}>
            <Typography variant="h2">{formatDate(segment.start)} – {formatDate(segment.end)} <Typography component="span" color="text.secondary">({segment.days} days)</Typography></Typography>
            <Typography variant="h2">{data.method === "proration" ? `${formatMoney(segment.allocated_income)} allocated · ${formatMoney(segment.net_income)} booked` : formatMoney(segment.net_income)}</Typography>
          </Stack>
          <DataTable rows={segment.allocations} getKey={(row) => row.owner_id} emptyTitle="No owners in this segment" emptyDetail="Income on these days is not allocated to anyone." columns={[
            { key: "name", label: "Owner" },
            { key: "stated_pct", label: "Stated", align: "right", render: (row) => formatPct(row.stated_pct) },
            { key: "effective_pct", label: "Effective", align: "right", render: (row) => formatPct(row.effective_pct) },
            { key: "amount", label: "Allocated", align: "right", render: (row) => formatMoney(row.amount) },
          ]} />
          {segment.by_account.length > 0 && <Box sx={{ mt: 1, px: 1 }}>{segment.by_account.map((row) => <Stack sx={{ justifyContent: "space-between" }} key={row.account_id} direction="row"><Typography variant="body2" color="text.secondary">{row.code} {row.name}</Typography><Typography variant="body2" color="text.secondary">{formatMoney(row.net_income_effect)}</Typography></Stack>)}</Box>}
        </Box>
      ))}
      <Typography variant="h2" sx={{ pb: 1, borderBottom: 2, borderColor: "text.secondary" }}>Totals by owner</Typography>
      <DataTable rows={data.totals_by_owner} getKey={(row) => row.owner_id} emptyTitle="No owners" columns={[
        { key: "name", label: "Owner" },
        { key: "amount", label: "Allocated income (loss)", align: "right", render: (row) => formatMoney(row.amount) },
      ]} footer={<>
        {Number(data.unallocated) !== 0 && <TableRow><TableCell>Unallocated</TableCell><TableCell align="right">{formatMoney(data.unallocated)}</TableCell></TableRow>}
        <TableRow><TableCell sx={{ fontWeight: 700 }}>Net income for the period</TableCell><TableCell align="right" sx={{ fontWeight: 700 }}>{formatMoney(data.net_income)}</TableCell></TableRow>
      </>} />
    </Box>
  );
}

export function CapitalRollforwardBody({ data }) {
  const money = (key) => ({ key, align: "right", render: (row) => formatMoney(row[key]) });
  return (
    <Box>
      <Warnings warnings={data.warnings} />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Allocated income uses the {labels[data.method]?.toLowerCase()} method and {labels[data.convention]?.toLowerCase()} convention. The beginning balance includes income allocated in earlier years. Other movements are capital moved between owners and owner-tagged entries to other equity accounts.</Typography>
      <DataTable rows={data.owners} getKey={(row) => row.owner_id} emptyTitle="No owner capital in this period" columns={[
        { key: "owner_name", label: "Owner", render: (row) => <Typography sx={{ fontWeight: 700 }}>{row.owner_name}</Typography> },
        { ...money("beginning"), label: "Beginning" },
        { ...money("contributions"), label: "Contributions" },
        { ...money("draws"), label: "Draws" },
        { ...money("allocated_income_loss"), label: "Allocated income (loss)" },
        { ...money("other"), label: "Other" },
        { ...money("ending"), label: "Ending", render: (row) => <Typography sx={{ fontWeight: 700 }}>{formatMoney(row.ending)}</Typography> },
      ]} />
    </Box>
  );
}
