import { useState } from "react";
import { Link as RouterLink, useParams } from "react-router-dom";
import { Box, Button, Card, CardActionArea, CardContent, Link, Paper, Stack, TableCell, TableRow, Typography } from "@mui/material";
import AccountBalanceWalletRounded from "@mui/icons-material/AccountBalanceWalletRounded";
import AssessmentRounded from "@mui/icons-material/AssessmentRounded";
import DownloadRounded from "@mui/icons-material/DownloadRounded";
import PrintRounded from "@mui/icons-material/PrintRounded";
import ReceiptLongRounded from "@mui/icons-material/ReceiptLongRounded";
import ScheduleRounded from "@mui/icons-material/ScheduleRounded";
import { DataTable, DateField, ErrorState, formatDate, formatMoney, LoadingState, localToday, Page, useApiData } from "../components/Common";

const reports = [
  ["balance-sheet", "Balance sheet", "Assets, liabilities, and equity as of a date", AccountBalanceWalletRounded],
  ["income-statement", "Profit and loss", "Revenue and expenses over a period", AssessmentRounded],
  ["trial-balance", "Trial balance", "Net debit and credit balances by account", AssessmentRounded],
  ["general-ledger", "General ledger", "Every posted journal line", ReceiptLongRounded],
  ["receivables-aging", "Receivables aging", "Open customer invoices by age", ScheduleRounded],
  ["payables-aging", "Payables aging", "Open vendor bills by age", ScheduleRounded],
  ["owner-balances", "Owner balances", "Contributions, draws, and loans payable", AccountBalanceWalletRounded],
];

export function ReportsIndex() {
  return (
    <Page title="Reports" eyebrow="Financial statements and ledgers" crumbs={[{ label: "Reports" }]} actions={<Button component="a" href="/export/excel/" variant="contained" startIcon={<DownloadRounded />}>Export Excel workbook</Button>}>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", xl: "repeat(3, 1fr)" }, gap: 2 }}>
        {reports.map(([slug, title, detail, Icon]) => <Card key={slug}><CardActionArea component={RouterLink} to={`/reports/${slug}/`} sx={{ height: "100%" }}><CardContent><Icon color="primary" sx={{ mb: 1 }} /><Typography variant="h2">{title}</Typography><Typography variant="body2" color="text.secondary" sx={{ mt: .75 }}>{detail}</Typography></CardContent></CardActionArea></Card>)}
      </Box>
    </Page>
  );
}

function StatementGroup({ title, rows, totalLabel, total, extra }) {
  return <Box sx={{ maxWidth: 820, mb: 3 }}><Typography variant="h2" sx={{ pb: 1, borderBottom: 2, borderColor: "text.secondary" }}>{title}</Typography>{rows.map((row) => <Stack key={row.account.id} direction="row" justifyContent="space-between" spacing={3} sx={{ py: 1, px: 1 }}><Link component={RouterLink} to={`/accounts/${row.account.id}/`}>{row.account.name}</Link><Typography sx={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(row.balance)}</Typography></Stack>)}{extra}<Stack direction="row" justifyContent="space-between" sx={{ py: 1.25, px: 1, borderTop: 1, borderColor: "divider" }}><Typography fontWeight={700}>{totalLabel}</Typography><Typography fontWeight={700}>{formatMoney(total)}</Typography></Stack></Box>;
}

export function ReportDetail() {
  const { report } = useParams();
  const dateNow = localToday();
  const [filters, setFilters] = useState({ as_of: dateNow, start: `${dateNow.slice(0, 4)}-01-01`, end: dateNow });
  const [query, setQuery] = useState("");
  const { data, loading, error } = useApiData(`/api/reports/${report}/${query}`, [report, query]);
  const ranged = ["income-statement", "general-ledger"].includes(report);
  const run = (event) => { event.preventDefault(); const params = new URLSearchParams(ranged ? { start: filters.start, end: filters.end } : { as_of: filters.as_of }); setQuery(`?${params}`); };
  const title = data?.title || reports.find(([slug]) => slug === report)?.[1] || "Report";
  return (
    <Page title={title} eyebrow="Financial report" crumbs={[{ label: "Reports", to: "/reports/" }, { label: title }]} actions={<Button variant="outlined" startIcon={<PrintRounded />} onClick={() => window.print()}>Print</Button>}>
      <Paper component="form" onSubmit={run} variant="outlined" sx={{ p: 2, mb: 3, display: "flex", gap: 1.5, alignItems: "center", flexWrap: "wrap", "@media print": { display: "none" } }}>
        {ranged ? <><DateField label="From" value={filters.start} onChange={(value) => setFilters({ ...filters, start: value })} sx={{ width: 180 }} /><DateField label="Through" value={filters.end} onChange={(value) => setFilters({ ...filters, end: value })} sx={{ width: 180 }} /></> : <DateField label="As of" value={filters.as_of} onChange={(value) => setFilters({ ...filters, as_of: value })} sx={{ width: 180 }} />}
        <Button type="submit" variant="contained">Run report</Button>
      </Paper>
      {loading ? <LoadingState /> : error ? <ErrorState error={error} /> : <ReportBody data={data} />}
    </Page>
  );
}

function ReportBody({ data }) {
  if (data.report === "trial-balance") return <DataTable rows={data.rows} getKey={(row) => row.account.id} columns={[
    { key: "account", label: "Account", render: (row) => <Link component={RouterLink} to={`/accounts/${row.account.id}/`}>{row.account.display_name}</Link> },
    { key: "debit", label: "Debit balance", align: "right", render: (row) => Number(row.debit) ? formatMoney(row.debit) : "—" },
    { key: "credit", label: "Credit balance", align: "right", render: (row) => Number(row.credit) ? formatMoney(row.credit) : "—" },
  ]} footer={<TableRow><TableCell sx={{ fontWeight: 700 }}>Totals</TableCell><TableCell align="right" sx={{ fontWeight: 700 }}>{formatMoney(data.total_debits)}</TableCell><TableCell align="right" sx={{ fontWeight: 700 }}>{formatMoney(data.total_credits)}</TableCell></TableRow>} />;
  if (data.report === "balance-sheet") return <Box>{["asset", "liability", "equity"].map((key) => <StatementGroup key={key} title={`${key[0].toUpperCase()}${key.slice(1)}`} rows={data.groups[key].rows} totalLabel={`Total ${key}`} total={data.groups[key].total} extra={key === "equity" && Number(data.groups[key].current_earnings) !== 0 ? <Stack direction="row" justifyContent="space-between" sx={{ py: 1, px: 1 }}><Typography>Current earnings</Typography><Typography>{formatMoney(data.groups[key].current_earnings)}</Typography></Stack> : null} />)}</Box>;
  if (data.report === "income-statement") return <Box><StatementGroup title="Revenue" rows={data.revenue} totalLabel="Total revenue" total={data.revenue_total} /><StatementGroup title="Expenses" rows={data.expenses} totalLabel="Total expenses" total={data.expense_total} /><Stack direction="row" justifyContent="space-between" sx={{ maxWidth: 820, p: 1.5, borderTop: 3, borderBottom: 3, borderStyle: "double", borderColor: "text.primary" }}><Typography variant="h2">Net income</Typography><Typography variant="h2">{formatMoney(data.net_income)}</Typography></Stack></Box>;
  if (data.report === "general-ledger") return <DataTable rows={data.lines} emptyTitle="No activity in this period" columns={[
    { key: "date", label: "Date", render: (row) => formatDate(row.date) }, { key: "entry", label: "Entry", render: (row) => <Link component={RouterLink} to={`/transactions/${row.entry.id}/`}>{row.entry.number}</Link> },
    { key: "account", label: "Account", render: (row) => row.account.display_name }, { key: "description", label: "Description" },
    { key: "debit", label: "Debit", align: "right", render: (row) => Number(row.debit) ? formatMoney(row.debit) : "—" }, { key: "credit", label: "Credit", align: "right", render: (row) => Number(row.credit) ? formatMoney(row.credit) : "—" },
  ]} />;
  if (["receivables-aging", "payables-aging"].includes(data.report)) return <Box><Box sx={{ display: "grid", gridTemplateColumns: { xs: "repeat(2, 1fr)", md: "repeat(5, 1fr)" }, gap: 1.5, mb: 2 }}>{Object.entries(data.buckets).map(([label, value]) => <Card key={label}><CardContent><Typography variant="overline" color="text.secondary">{label}</Typography><Typography variant="h2">{formatMoney(value)}</Typography></CardContent></Card>)}</Box><DataTable rows={data.rows} getKey={(row) => row.document.id} emptyTitle="No open items" columns={[
    { key: "document", label: "Document", render: (row) => <Link component={RouterLink} to={`/documents/${row.document.id}/`}>{row.document.number}</Link> }, { key: "contact", label: "Contact", render: (row) => row.document.contact.name },
    { key: "due", label: "Due", render: (row) => formatDate(row.document.due_date) }, { key: "days", label: "Days overdue" }, { key: "bucket", label: "Bucket" }, { key: "balance", label: "Balance", align: "right", render: (row) => formatMoney(row.balance) },
  ]} /></Box>;
  if (data.report === "owner-balances") return <DataTable rows={data.rows} getKey={(row) => row.owner.id} columns={[
    { key: "owner", label: "Owner", render: (row) => row.owner.name }, { key: "contributions", label: "Contributions", align: "right", render: (row) => formatMoney(row.contributions) }, { key: "draws", label: "Draws", align: "right", render: (row) => formatMoney(row.draws) }, { key: "owed", label: "Amount owed", align: "right", render: (row) => formatMoney(row.owed) },
  ]} />;
  return null;
}
