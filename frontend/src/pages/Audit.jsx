import { useState } from "react";
import { Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from "@mui/material";
import VisibilityRounded from "@mui/icons-material/VisibilityRounded";
import { DataTable, ErrorState, formatDate, LoadingState, Page, useApiData } from "../components/Common";

export default function AuditPage() {
  const { data, loading, error } = useApiData("/api/audit/", []);
  const [selected, setSelected] = useState(null);
  return (
    <Page title="Audit history" eyebrow="Change log" crumbs={[{ label: "Audit history" }]}>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <DataTable rows={data.events} emptyTitle="No audited changes yet" columns={[
        { key: "created_at", label: "When", render: (row) => formatDate(row.created_at, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) },
        { key: "actor", label: "User" }, { key: "action", label: "Action", render: (row) => <Chip size="small" label={row.action} sx={{ textTransform: "capitalize" }} variant="outlined" /> },
        { key: "object_type", label: "Object" }, { key: "object_id", label: "Record" }, { key: "details", label: "", align: "right", render: (row) => <Button size="small" startIcon={<VisibilityRounded />} onClick={() => setSelected(row)}>Details</Button> },
      ]} />}
      <Dialog open={Boolean(selected)} onClose={() => setSelected(null)} maxWidth="md" fullWidth><DialogTitle>Audit details</DialogTitle><DialogContent><Stack spacing={2}><Typography variant="body2" color="text.secondary">{selected && `${selected.action} by ${selected.actor}`}</Typography>{selected && <><Typography variant="h3">Before</Typography><Typography component="pre" sx={{ p: 1.5, bgcolor: "grey.100", borderRadius: 1, overflow: "auto", fontSize: 12 }}>{JSON.stringify(selected.before, null, 2)}</Typography><Typography variant="h3">After</Typography><Typography component="pre" sx={{ p: 1.5, bgcolor: "grey.100", borderRadius: 1, overflow: "auto", fontSize: 12 }}>{JSON.stringify(selected.after, null, 2)}</Typography></>}</Stack></DialogContent><DialogActions><Button onClick={() => setSelected(null)}>Close</Button></DialogActions></Dialog>
    </Page>
  );
}

