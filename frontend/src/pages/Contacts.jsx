import { useEffect, useState } from "react";
import { Link as RouterLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, Box, Button, Chip, FormControlLabel, Link, MenuItem, Paper, Stack, Switch, TextField, Typography } from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import EditRounded from "@mui/icons-material/EditRounded";
import { api } from "../api";
import { DataTable, ErrorState, fieldError, FormActions, LoadingState, Page, PageLoading, useApiData } from "../components/Common";
import { useApp } from "../context";

export function ContactsList() {
  const { session, options } = useApp();
  const location = useLocation();
  const [kind, setKind] = useState(new URLSearchParams(location.search).get("kind") || "");
  const { data, loading, error } = useApiData(`/api/contacts/${kind ? `?kind=${kind}` : ""}`, [kind]);
  return (
    <Page title="Contacts" eyebrow="Customers, vendors, and owners" crumbs={[{ label: "Contacts" }]} actions={session.permissions.edit_books && <Button component={RouterLink} to="/contacts/new/" variant="contained" startIcon={<AddRounded />}>New contact</Button>}>
      <Box sx={{ width: { xs: "100%", sm: 280 }, mb: 2 }}><TextField select label="Contact type" value={kind} onChange={(event) => setKind(event.target.value)}><MenuItem value="">All contact types</MenuItem>{options?.choices.contact_kinds.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField></Box>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.contacts} emptyTitle="No contacts yet" columns={[
        { key: "name", label: "Name", sx: { minWidth: 180 }, render: (row) => <Typography fontWeight={700}>{row.name}</Typography> },
        { key: "kind", label: "Type", render: (row) => <Chip size="small" label={row.kind_label} variant="outlined" /> },
        { key: "email", label: "Email", render: (row) => row.email || "—" },
        { key: "phone", label: "Phone", render: (row) => row.phone || "—" },
        { key: "status", label: "Status", render: (row) => row.is_active ? "Active" : "Inactive" },
        { key: "actions", label: "", align: "right", render: (row) => session.permissions.edit_books && <Button component={RouterLink} to={`/contacts/${row.id}/edit/`} size="small" startIcon={<EditRounded />}>Edit</Button> },
      ]} />}
    </Page>
  );
}

export function ContactForm() {
  const { id } = useParams();
  const editing = Boolean(id);
  const navigate = useNavigate();
  const { options, refreshOptions, notify } = useApp();
  const detail = useApiData(editing ? `/api/contacts/${id}/` : null, [id]);
  const [values, setValues] = useState({ name: "", kind: "customer", email: "", phone: "", address: "", notes: "", is_active: true });
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (editing && detail.data?.contact) { const item = detail.data.contact; setValues({ name: item.name, kind: item.kind, email: item.email, phone: item.phone, address: item.address, notes: item.notes, is_active: item.is_active }); } }, [editing, detail.data]);
  if (editing && detail.loading) return <PageLoading title="Edit contact" />;
  const set = (key) => (event) => setValues({ ...values, [key]: event.target.value });
  const submit = async (event) => { event.preventDefault(); setSaving(true); setErrors({}); try { await api(editing ? `/api/contacts/${id}/` : "/api/contacts/", { method: "POST", body: values }); await refreshOptions(); notify(`Contact ${editing ? "updated" : "created"}.`); navigate("/contacts/"); } catch (error) { setErrors({ ...(error.fields || {}), __all__: error.message }); } finally { setSaving(false); } };
  const label = editing ? detail.data?.contact.name || "contact" : "New contact";
  return (
    <Page title={editing ? `Edit ${label}` : label} eyebrow="Contacts" crumbs={[{ label: "Contacts", to: "/contacts/" }, { label: editing ? label : "New contact" }]}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ p: { xs: 2, sm: 3 }, maxWidth: 760 }}>
        {errors.__all__ && <Alert severity="error" sx={{ mb: 2 }}>{errors.__all__}</Alert>}
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)" }, gap: 2 }}>
          <TextField required label="Name" value={values.name} onChange={set("name")} error={Boolean(fieldError(errors, "name"))} helperText={fieldError(errors, "name")} />
          <TextField required select label="Contact type" value={values.kind} onChange={set("kind")}><MenuItem value="">Choose type</MenuItem>{options?.choices.contact_kinds.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}</TextField>
          <TextField label="Email" type="email" value={values.email} onChange={set("email")} error={Boolean(fieldError(errors, "email"))} helperText={fieldError(errors, "email")} />
          <TextField label="Phone" value={values.phone} onChange={set("phone")} />
          <TextField multiline minRows={3} label="Address" value={values.address} onChange={set("address")} />
          <TextField multiline minRows={3} label="Notes" value={values.notes} onChange={set("notes")} />
          <FormControlLabel control={<Switch checked={values.is_active} onChange={(event) => setValues({ ...values, is_active: event.target.checked })} />} label="Active contact" />
        </Box>
        <FormActions saving={saving} submitLabel="Save contact" cancelTo="/contacts/" />
      </Paper>
    </Page>
  );
}
