import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Avatar, Box, Button, CircularProgress, Paper, Stack, TextField, Typography } from "@mui/material";
import LockRounded from "@mui/icons-material/LockRounded";
import { api } from "../api";
import { useApp } from "../context";

export default function LoginPage() {
  const { session, refreshSession } = useApp();
  const navigate = useNavigate();
  const [values, setValues] = useState({ username: "", password: "" });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => { document.title = "Sign in · LiteBooks"; if (session?.authenticated) navigate("/"); }, [session?.authenticated]);
  const submit = async (event) => { event.preventDefault(); setSaving(true); setError(""); try { await api("/api/auth/login/", { method: "POST", body: values }); await refreshSession(); navigate("/"); } catch (requestError) { setError(requestError.message); } finally { setSaving(false); } };
  return (
    <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center", p: 2, bgcolor: "#EDF1EF" }}>
      <Paper component="form" onSubmit={submit} variant="outlined" sx={{ width: "100%", maxWidth: 410, p: { xs: 3, sm: 4 } }}>
        <Stack direction="row" alignItems="center" spacing={1.5}><Avatar variant="rounded" sx={{ bgcolor: "secondary.main", fontWeight: 900 }}>LB</Avatar><Box><Typography variant="h1" sx={{ fontSize: 24 }}>LiteBooks</Typography><Typography color="text.secondary" variant="body2">Business books, kept close.</Typography></Box></Stack>
        {error && <Alert severity="error" sx={{ mt: 3 }}>{error}</Alert>}
        <Stack spacing={2} sx={{ mt: 3 }}><TextField required autoFocus autoComplete="username" label="Username" value={values.username} onChange={(event) => setValues({ ...values, username: event.target.value })} /><TextField required type="password" autoComplete="current-password" label="Password" value={values.password} onChange={(event) => setValues({ ...values, password: event.target.value })} /><Button type="submit" variant="contained" size="large" disabled={saving} startIcon={saving ? <CircularProgress color="inherit" size={16} /> : <LockRounded />}>Sign in</Button></Stack>
      </Paper>
    </Box>
  );
}
