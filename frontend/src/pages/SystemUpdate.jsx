import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogContentText,
  DialogTitle, Divider, LinearProgress, Paper, Stack, Typography,
} from "@mui/material";
import CheckCircleRounded from "@mui/icons-material/CheckCircleRounded";
import ErrorOutlineRounded from "@mui/icons-material/ErrorOutlineRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import SystemUpdateAltRounded from "@mui/icons-material/SystemUpdateAltRounded";
import { api } from "../api";
import { ErrorState, formatDate, Page, PageLoading } from "../components/Common";
import { useApp } from "../context";

const POLL_INTERVAL = 2000;
const RESTART_GRACE_MS = 5 * 60 * 1000;

const STEP_LABELS = {
  queued: "Queued",
  backup: "Backing up the database",
  pull: "Downloading the new version",
  apply: "Restarting LiteBooks",
  verify: "Verifying the new version",
  done: "Done",
};

function Mono({ children, sx }) {
  return <Box component="code" sx={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13, ...sx }}>{children}</Box>;
}

function LogBlock({ text }) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest" }); }, [text]);
  if (!text) return null;
  return (
    <Box sx={{ mt: 2, p: 1.5, maxHeight: 320, overflow: "auto", bgcolor: "#10231F", color: "#D7E8E2", borderRadius: 1, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {text}
      <div ref={endRef} />
    </Box>
  );
}

function VersionCard({ current, state }) {
  return (
    <Paper variant="outlined" sx={{ p: 2.5 }}>
      <Typography variant="h2" sx={{ mb: 1.5 }}>Installed version</Typography>
      <Stack spacing={0.75}>
        <Typography><strong>LiteBooks {current.version}</strong> <Mono sx={{ color: "text.secondary" }}>{current.short_sha}</Mono></Typography>
        {current.built_at && current.built_at !== "unknown" && <Typography variant="body2" color="text.secondary">Built {formatDate(current.built_at, { dateStyle: "medium", timeStyle: "short" })}</Typography>}
        <Typography variant="body2" color="text.secondary">Tracking <Mono>{state.repo}</Mono> on <Mono>{state.branch}</Mono></Typography>
        {state.last_checked_at && <Typography variant="body2" color="text.secondary">Last checked {formatDate(state.last_checked_at, { dateStyle: "medium", timeStyle: "short" })}</Typography>}
      </Stack>
    </Paper>
  );
}

function CommitList({ commits }) {
  if (!commits?.length) return null;
  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Changes since your version</Typography>
      <Stack spacing={0.75} sx={{ maxHeight: 260, overflow: "auto", pr: 1 }}>
        {commits.map((commit) => (
          <Box key={commit.sha} sx={{ display: "flex", gap: 1.25, alignItems: "baseline" }}>
            <Mono sx={{ color: "text.secondary", flexShrink: 0 }}>{commit.sha}</Mono>
            <Typography variant="body2" sx={{ minWidth: 0 }}>{commit.message}</Typography>
          </Box>
        ))}
      </Stack>
    </Box>
  );
}

export default function SystemUpdatePage() {
  const { session, notify } = useApp();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [applying, setApplying] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const restartSince = useRef(null);

  const canApply = Boolean(session?.permissions?.update_system);
  const run = data?.run;
  const active = Boolean(run?.active);

  const load = useCallback(async () => {
    try {
      const response = await api("/api/system/update/");
      setData(response);
      setError(null);
    } catch (requestError) {
      setError(requestError);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // While a run is in flight the web container is replaced underneath us, so
  // failed requests are the expected mid-update state rather than an error.
  useEffect(() => {
    if (!active) {
      restartSince.current = null;
      setRestarting(false);
      return undefined;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const response = await api("/api/system/update/status/");
        if (cancelled) return;
        restartSince.current = null;
        setRestarting(false);
        setData((current) => ({ ...current, ...response }));
        if (response.run && !response.run.active) {
          if (response.run.status === "success") {
            // The frontend bundle changed with the image; drop the old one.
            window.location.reload();
          } else {
            notify("The update did not complete. See the log below.", "error");
          }
        }
      } catch {
        if (cancelled) return;
        restartSince.current = restartSince.current || Date.now();
        setRestarting(true);
        if (Date.now() - restartSince.current > RESTART_GRACE_MS) {
          setRestarting(false);
          notify("Lost contact with LiteBooks while updating. Check the server.", "error");
        }
      }
    };
    const timer = setInterval(tick, POLL_INTERVAL);
    return () => { cancelled = true; clearInterval(timer); };
  }, [active, notify]);

  const check = async () => {
    setChecking(true);
    try {
      const response = await api("/api/system/update/check/", { method: "POST" });
      setData((current) => ({ ...current, ...response }));
      notify(response.state.update_available ? "An update is available." : "LiteBooks is up to date.", "info");
    } catch (requestError) {
      notify(requestError.message, "error");
    } finally {
      setChecking(false);
    }
  };

  const apply = async () => {
    setApplying(true);
    try {
      const response = await api("/api/system/update/apply/", { method: "POST", body: { target_sha: data.state.latest_sha } });
      setConfirming(false);
      setData((current) => ({ ...current, run: response.run }));
    } catch (requestError) {
      notify(requestError.message, "error");
    } finally {
      setApplying(false);
    }
  };

  if (!data && !error) return <PageLoading title="Software update" />;
  if (error) return <Page title="Software update" crumbs={[{ label: "Settings" }, { label: "Updates" }]}><ErrorState error={error} /></Page>;

  const { current, state } = data;
  const failed = run && run.status === "failed";

  return (
    <Page title="Software update" eyebrow="System" crumbs={[{ label: "Settings" }, { label: "Updates" }]}>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", xl: "minmax(0, 1.6fr) 360px" }, gap: 3, alignItems: "start" }}>
        <Stack spacing={3}>
          {!state.enabled && <Alert severity="info">Software updates are disabled on this installation (<Mono>LITEBOOKS_UPDATES_ENABLED=false</Mono>).</Alert>}

          {active && (
            <Paper variant="outlined" sx={{ p: 2.5, borderColor: "primary.main" }}>
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1.5 }}>
                <CircularProgress size={20} />
                <Typography variant="h2" sx={{ m: 0 }}>
                  {restarting ? "Restarting LiteBooks…" : STEP_LABELS[run.step] || "Updating"}
                </Typography>
                <Chip size="small" variant="outlined" label={`${run.from_sha} → ${run.to_sha}`} />
              </Stack>
              <LinearProgress />
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
                {restarting
                  ? "The application is being replaced. This page will come back on its own — do not close it or power off the server."
                  : "A database backup is taken before anything changes."}
              </Typography>
              <LogBlock text={run.log} />
            </Paper>
          )}

          {failed && (
            <Paper variant="outlined" sx={{ p: 2.5, borderColor: "error.main" }}>
              <Stack direction="row" spacing={1.25} alignItems="center" sx={{ mb: 1 }}>
                <ErrorOutlineRounded color="error" />
                <Typography variant="h2" sx={{ m: 0 }}>Update failed</Typography>
                <Chip size="small" color="error" variant="outlined" label={STEP_LABELS[run.step] || run.step} />
              </Stack>
              <Typography variant="body2" color="text.secondary">
                LiteBooks was not rolled back automatically: database migrations may already have run, and reverting the
                application without reverting the database can leave the two out of step. Use the backup below to restore if needed.
              </Typography>
              {run.backup_path && <Typography variant="body2" sx={{ mt: 1 }}>Pre-update backup: <Mono>{run.backup_path}</Mono></Typography>}
              <LogBlock text={run.log} />
            </Paper>
          )}

          {!active && !failed && state.update_available && (
            <Paper variant="outlined" sx={{ p: 2.5, borderColor: "primary.main" }}>
              <Stack direction="row" spacing={1.25} alignItems="center" sx={{ mb: 1 }}>
                <SystemUpdateAltRounded color="primary" />
                <Typography variant="h2" sx={{ m: 0 }}>Update available</Typography>
                <Chip size="small" color="primary" variant="outlined" label={`${state.commits_behind} commit${state.commits_behind === 1 ? "" : "s"} behind`} />
              </Stack>
              <Typography variant="body2" color="text.secondary">
                <Mono>{current.short_sha}</Mono> → <Mono>{state.latest_short_sha}</Mono>
                {state.latest_committed_at && ` · ${formatDate(state.latest_committed_at, { dateStyle: "medium" })}`}
              </Typography>
              <CommitList commits={state.commit_log} />
              <Divider sx={{ my: 2 }} />
              {canApply ? (
                <Button variant="contained" startIcon={<SystemUpdateAltRounded />} onClick={() => setConfirming(true)}>Update now</Button>
              ) : (
                <Alert severity="info">Only an owner can install updates.</Alert>
              )}
            </Paper>
          )}

          {!active && !failed && !state.update_available && state.enabled && (
            <Paper variant="outlined" sx={{ p: 2.5 }}>
              <Stack direction="row" spacing={1.25} alignItems="center">
                <CheckCircleRounded color="success" />
                <Typography variant="h2" sx={{ m: 0 }}>LiteBooks is up to date</Typography>
              </Stack>
              {state.latest_sha && !state.image_available && state.latest_sha !== current.sha && (
                <Alert severity="info" sx={{ mt: 2 }}>
                  Commit <Mono>{state.latest_short_sha}</Mono> is newer than your version, but its image has not finished
                  publishing yet. Check again in a few minutes.
                </Alert>
              )}
            </Paper>
          )}

          {state.check_error && <Alert severity="warning">{state.check_error}</Alert>}
        </Stack>

        <Stack spacing={2}>
          <VersionCard current={current} state={state} />
          <Button fullWidth variant="outlined" startIcon={<RefreshRounded />} disabled={checking || active || !state.enabled} onClick={check}>
            {checking ? "Checking…" : "Check for updates"}
          </Button>
        </Stack>
      </Box>

      <Dialog open={confirming} onClose={() => setConfirming(false)}>
        <DialogTitle>Update LiteBooks to {state.latest_short_sha}?</DialogTitle>
        <DialogContent>
          <DialogContentText component="div">
            <p>LiteBooks will back up the database, download the new version, and restart. This usually takes under a minute.</p>
            <p>Everyone will be signed out briefly while the application restarts. Do not power off the server during the update.</p>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirming(false)} color="inherit">Cancel</Button>
          <Button onClick={apply} variant="contained" disabled={applying}>{applying ? "Starting…" : "Back up and update"}</Button>
        </DialogActions>
      </Dialog>
    </Page>
  );
}
