import { useEffect, useState } from "react";
import { Link as RouterLink, useNavigate, useParams } from "react-router-dom";
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, Divider,
  FormControlLabel, MenuItem, Paper, Stack, Switch, TextField, Typography,
} from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import LockOpenRounded from "@mui/icons-material/LockOpenRounded";
import LockRounded from "@mui/icons-material/LockRounded";
import { api } from "../api";
import { DataTable, ErrorState, fieldError, formatDate, FormActions, LoadingState, Page, PageLoading, useApiData } from "../components/Common";
import { useApp } from "../context";

const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

export function PeriodsPage() {
  const { notify } = useApp();
  const { data, loading, error, reload } = useApiData("/api/periods/", []);
  const [values, setValues] = useState({ year: new Date().getFullYear(), month: new Date().getMonth() + 1 });
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const add = async (event) => { event.preventDefault(); setSaving(true); try { const response = await api("/api/periods/", { method: "POST", body: values }); notify(response.created ? "Accounting period added." : "That period already exists.", response.created ? "success" : "info"); await reload(); } catch (error) { notify(error.message, "error"); } finally { setSaving(false); } };
  const toggle = async () => { try { const response = await api(`/api/periods/${confirm.id}/toggle/`, { method: "POST" }); notify(`${confirm.label} ${response.action}.`); setConfirm(null); await reload(); } catch (error) { notify(error.message, "error"); } };
  return (
    <Page title="Monthly period locks" eyebrow="Accounting controls" crumbs={[{ label: "Settings" }, { label: "Period locks" }]}>
      <Paper component="form" onSubmit={add} variant="outlined" sx={{ p: 2, mb: 3, display: "flex", gap: 1.5, alignItems: "center", flexWrap: "wrap" }}>
        <TextField label="Year" type="number" value={values.year} onChange={(event) => setValues({ ...values, year: event.target.value })} sx={{ width: 140 }} />
        <TextField select label="Month" value={values.month} onChange={(event) => setValues({ ...values, month: event.target.value })} sx={{ width: 180 }}>{months.map((label, index) => <MenuItem key={label} value={index + 1}>{label}</MenuItem>)}</TextField>
        <Button type="submit" variant="outlined" disabled={saving} startIcon={<AddRounded />}>Add period</Button>
      </Paper>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.periods} emptyTitle="No explicit periods yet" emptyDetail="Months remain open until a period is added and closed." columns={[
        { key: "label", label: "Period", render: (row) => <Typography fontWeight={700}>{row.label}</Typography> },
        { key: "status", label: "Status", render: (row) => <Chip size="small" color={row.is_closed ? "error" : "success"} variant="outlined" icon={row.is_closed ? <LockRounded /> : <LockOpenRounded />} label={row.is_closed ? "Closed" : "Open"} /> },
        { key: "closed_by", label: "Changed by", render: (row) => row.closed_by || "—" },
        { key: "closed_at", label: "Changed at", render: (row) => row.closed_at ? formatDate(row.closed_at) : "—" },
        { key: "actions", label: "", align: "right", render: (row) => <Button size="small" color={row.is_closed ? "primary" : "error"} onClick={() => setConfirm(row)}>{row.is_closed ? "Reopen" : "Close"}</Button> },
      ]} />}
      <Dialog open={Boolean(confirm)} onClose={() => setConfirm(null)}><DialogTitle>{confirm?.is_closed ? "Reopen" : "Close"} {confirm?.label}?</DialogTitle><DialogContent><DialogContentText>{confirm?.is_closed ? "Transactions in this month will become editable again." : "New postings and edits dated in this month will be blocked."}</DialogContentText></DialogContent><DialogActions><Button onClick={() => setConfirm(null)} color="inherit">Cancel</Button><Button onClick={toggle} variant="contained" color={confirm?.is_closed ? "primary" : "error"}>{confirm?.is_closed ? "Reopen period" : "Close period"}</Button></DialogActions></Dialog>
    </Page>
  );
}

export function UsersPage() {
  const { options, notify } = useApp();
  const { data, loading, error, reload } = useApiData("/api/users/", []);
  const [values, setValues] = useState({ username: "", first_name: "", last_name: "", role: "viewer", password: "", is_active: true });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { await api("/api/users/", { method: "POST", body: values }); notify("User created."); setValues({ username: "", first_name: "", last_name: "", role: "viewer", password: "", is_active: true }); await reload(); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  return (
    <Page title="Users" eyebrow="Access control" crumbs={[{ label: "Settings" }, { label: "Users" }]}>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", xl: "minmax(0, 1.5fr) 380px" }, gap: 3, alignItems: "start" }}>
        <Box>{loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.users} columns={[
          { key: "username", label: "Username", render: (row) => <Typography fontWeight={700}>{row.username}</Typography> }, { key: "name", label: "Name", render: (row) => row.name || "—" },
          { key: "role", label: "Role", render: (row) => <Chip size="small" label={row.role_label} variant="outlined" /> }, { key: "status", label: "Status", render: (row) => row.is_active ? "Active" : "Inactive" },
          { key: "actions", label: "", align: "right", render: (row) => <Button component={RouterLink} to={`/settings/users/${row.id}/edit/`} size="small" startIcon={<EditRounded />}>Edit</Button> },
        ]} />}</Box>
        <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: 2.5 }}><Typography variant="h2" sx={{ mb: 2 }}>New user</Typography>{errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}<Stack spacing={2}><TextField required label="Username" value={values.username} onChange={set("username")} error={Boolean(fieldError(errors, "username"))} helperText={fieldError(errors, "username")} /><TextField label="First name" value={values.first_name} onChange={set("first_name")} /><TextField label="Last name" value={values.last_name} onChange={set("last_name")} /><TextField required select label="Role" value={values.role} onChange={set("role")}>{options?.choices.roles.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField><TextField required type="password" label="Temporary password" value={values.password} onChange={set("password")} error={Boolean(fieldError(errors, "password"))} helperText={fieldError(errors, "password") || "At least 8 characters"} /><FormControlLabel control={<Switch checked={values.is_active} onChange={(event) => setValues({ ...values, is_active: event.target.checked })} />} label="Active login" /><Button type="submit" variant="contained" disabled={saving}>Create user</Button></Stack></Paper>
      </Box>
    </Page>
  );
}

export function UserEditPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { options, notify, refreshSession } = useApp();
  const detail = useApiData(`/api/users/${id}/`, [id]);
  const [values, setValues] = useState({ first_name: "", last_name: "", role: "viewer", is_active: true });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (detail.data?.user) { const item = detail.data.user; setValues({ first_name: item.first_name, last_name: item.last_name, role: item.role, is_active: item.is_active }); } }, [detail.data]);
  if (detail.loading) return <PageLoading title="Edit user" />;
  if (detail.error) return <Page title="Edit user"><ErrorState error={detail.error} /></Page>;
  const item = detail.data.user;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { await api(`/api/users/${id}/`, { method: "POST", body: values }); await refreshSession(); notify("User updated."); navigate("/settings/users/"); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  return (
    <Page title={`Edit ${item.username}`} eyebrow="Access control" crumbs={[{ label: "Settings" }, { label: "Users", to: "/settings/users/" }, { label: item.username }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 650 }}>{errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}<Stack spacing={2}><TextField label="Username" value={item.username} disabled /><TextField label="First name" value={values.first_name} onChange={set("first_name")} /><TextField label="Last name" value={values.last_name} onChange={set("last_name")} /><TextField required select label="Role" value={values.role} onChange={set("role")}>{options?.choices.roles.map((option) => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}</TextField><FormControlLabel control={<Switch checked={values.is_active} onChange={(event) => setValues({ ...values, is_active: event.target.checked })} />} label="Active login" /></Stack><FormActions saving={saving} submitLabel="Save user" cancelTo="/settings/users/" /></Paper>
    </Page>
  );
}

export function ProfilePage() {
  const { notify, refreshSession } = useApp();
  const detail = useApiData("/api/profile/", []);
  const [values, setValues] = useState({ username: "", first_name: "", last_name: "", current_password: "", new_password: "", confirm_password: "" });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (detail.data?.profile) {
      setValues((current) => ({ ...current, ...detail.data.profile }));
    }
  }, [detail.data]);
  if (detail.loading) return <PageLoading title="Profile settings" />;
  if (detail.error) return <Page title="Profile settings" crumbs={[{ label: "Profile settings" }]}><ErrorState error={detail.error} /></Page>;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => {
    event.preventDefault();
    setSaving(true);
    setErrors({});
    try {
      const response = await api("/api/profile/", { method: "POST", body: values });
      setValues((current) => ({ ...current, current_password: "", new_password: "", confirm_password: "" }));
      await refreshSession();
      notify(response.password_changed ? "Profile and password updated." : "Profile updated.");
    } catch (error) {
      setErrors({ ...(error.fields || {}), __all__: error.message });
    } finally {
      setSaving(false);
    }
  };
  return (
    <Page title="Profile settings" eyebrow="Personal account" crumbs={[{ label: "Profile settings" }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 680 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Stack spacing={2}>
          <TextField required autoComplete="username" label="Username" value={values.username} onChange={set("username")} error={Boolean(fieldError(errors, "username"))} helperText={fieldError(errors, "username")} />
          <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" }, gap: 2 }}>
            <TextField autoComplete="given-name" label="First name" value={values.first_name} onChange={set("first_name")} />
            <TextField autoComplete="family-name" label="Last name" value={values.last_name} onChange={set("last_name")} />
          </Box>
          <Divider />
          <Typography variant="h2">Change password</Typography>
          <TextField type="password" autoComplete="current-password" label="Current password" value={values.current_password} onChange={set("current_password")} error={Boolean(fieldError(errors, "current_password"))} helperText={fieldError(errors, "current_password")} />
          <TextField type="password" autoComplete="new-password" label="New password" value={values.new_password} onChange={set("new_password")} error={Boolean(fieldError(errors, "new_password"))} helperText={fieldError(errors, "new_password")} />
          <TextField type="password" autoComplete="new-password" label="Confirm new password" value={values.confirm_password} onChange={set("confirm_password")} error={Boolean(fieldError(errors, "confirm_password"))} helperText={fieldError(errors, "confirm_password")} />
        </Stack>
        <FormActions saving={saving} submitLabel="Save profile" cancelTo="/" />
      </Paper>
    </Page>
  );
}
