import { Link as RouterLink } from "react-router-dom";
import { Box, Button, Card, CardContent, Chip, Link, Stack, Typography } from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import ArrowForwardRounded from "@mui/icons-material/ArrowForwardRounded";
import { DataTable, ErrorState, formatDate, formatMoney, LoadingState, Page, useApiData } from "../components/Common";
import { useApp } from "../context";

export default function Dashboard() {
  const { session } = useApp();
  const { data, loading, error } = useApiData("/api/dashboard/", []);
  return (
    <Page title="Financial snapshot" eyebrow="Company overview" actions={session.permissions.edit_books && <Button component={RouterLink} to="/transactions/new/" variant="contained" startIcon={<AddRounded />}>New transaction</Button>}>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", xl: "repeat(4, 1fr)" }, gap: 2 }}>
          {[
            ["Cash", data.metrics.cash, "Bank and cash accounts"], ["Receivables", data.metrics.receivables, "Open customer invoices"],
            ["Payables", data.metrics.payables, "Open vendor bills"], ["Year-to-date net income", data.metrics.net_income, "Revenue less expenses"],
          ].map(([label, value, caption]) => <Card key={label}><CardContent><Typography variant="overline" color="text.secondary">{label}</Typography><Typography sx={{ fontSize: 25, fontWeight: 700, fontVariantNumeric: "tabular-nums", color: Number(value) < 0 ? "error.main" : "text.primary" }}>{formatMoney(value)}</Typography><Typography variant="caption" color="text.secondary">{caption}</Typography></CardContent></Card>)}
        </Box>
        <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mt: 4, mb: 1.5 }}><Typography variant="h2">Recent transactions</Typography><Button component={RouterLink} to="/transactions/" endIcon={<ArrowForwardRounded />}>View all</Button></Stack>
        <DataTable rows={data.recent_entries} emptyTitle="No transactions yet" columns={[
          { key: "date", label: "Date", render: (row) => formatDate(row.date) },
          { key: "number", label: "Entry", render: (row) => <Link component={RouterLink} to={`/transactions/${row.id}/`} fontWeight={700}>{row.number}</Link> },
          { key: "description", label: "Description", sx: { minWidth: 220 } },
          { key: "source", label: "Source", render: (row) => <Chip size="small" label={row.source_label} variant="outlined" /> },
          { key: "total", label: "Amount", align: "right", render: (row) => <Typography fontWeight={600} sx={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.total)}</Typography> },
        ]} />
      </>}
    </Page>
  );
}

