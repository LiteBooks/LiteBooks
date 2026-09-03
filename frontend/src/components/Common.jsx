import { useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import {
  Alert, Box, Breadcrumbs, Button, CircularProgress, Link, Paper, Skeleton, Stack,
  Table, TableBody, TableCell, TableContainer, TableFooter, TableHead, TableRow, Typography, useMediaQuery,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import ChevronRightRounded from "@mui/icons-material/ChevronRightRounded";
import { DatePicker } from "@mui/x-date-pickers/DatePicker";
import dayjs from "dayjs";
import { api } from "../api";

export function formatMoney(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(number);
}

export function formatDate(value, options = { month: "short", day: "numeric", year: "numeric" }) {
  if (!value) return "—";
  const normalized = value.length === 10 ? `${value}T12:00:00` : value;
  return new Intl.DateTimeFormat("en-US", options).format(new Date(normalized));
}

export function localToday() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function DateField({ value, onChange, required = false, error = false, helperText = "", ...props }) {
  const parsed = value ? dayjs(value) : null;
  return (
    <DatePicker
      value={parsed?.isValid() ? parsed : null}
      onChange={(nextValue) => onChange(nextValue?.isValid() ? nextValue.format("YYYY-MM-DD") : "")}
      slotProps={{ textField: { required, error, helperText, fullWidth: true, size: "small" } }}
      {...props}
    />
  );
}

export function fieldError(errors, key) {
  const value = errors?.[key];
  return Array.isArray(value) ? value.join(" ") : value || "";
}

export function useApiData(url, dependencies = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const load = async () => {
    if (!url) {
      setState({ loading: false, data: null, error: null });
      return null;
    }
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const data = await api(url);
      setState({ loading: false, data, error: null });
      return data;
    } catch (error) {
      setState({ loading: false, data: null, error });
      return null;
    }
  };
  useEffect(() => { load(); }, dependencies);
  return { ...state, reload: load };
}

export function LoadingState({ rows = 5 }) {
  return <Stack spacing={1.5}>{Array.from({ length: rows }, (_, index) => <Skeleton key={index} variant="rounded" height={index === 0 ? 48 : 36} />)}</Stack>;
}

export function ErrorState({ error }) {
  return <Alert severity="error">{error?.message || "Something went wrong while loading this page."}</Alert>;
}

export function EmptyState({ title = "Nothing here yet", detail, action }) {
  return (
    <Stack alignItems="center" spacing={0.5} sx={{ py: 5, px: 2, textAlign: "center" }}>
      <Typography color="text.secondary" variant="body2">{title}</Typography>
      {detail && <Typography color="text.secondary" variant="caption">{detail}</Typography>}
      {action}
    </Stack>
  );
}

export function Page({ title, eyebrow, subtitle, crumbs = [], actions, children }) {
  const theme = useTheme();
  const compactBreadcrumbs = useMediaQuery(theme.breakpoints.down("sm"));
  useEffect(() => { document.title = `${title} · LiteBooks`; }, [title]);
  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ minHeight: 28, mb: 1.5 }}>
        <Breadcrumbs
          separator={<ChevronRightRounded sx={{ fontSize: 16 }} />}
          aria-label="Breadcrumb"
          maxItems={compactBreadcrumbs ? 2 : 4}
          itemsBeforeCollapse={1}
          itemsAfterCollapse={1}
          sx={{ minWidth: 0, "& .MuiBreadcrumbs-ol": { flexWrap: "nowrap" }, "& .MuiBreadcrumbs-li": { whiteSpace: "nowrap" } }}
        >
          <Link component={RouterLink} to="/" underline="hover" color="text.secondary">Overview</Link>
          {crumbs.map((crumb, index) => index === crumbs.length - 1 || !crumb.to
            ? <Typography key={`${crumb.label}-${index}`} color="text.primary" variant="body2">{crumb.label}</Typography>
            : <Link key={`${crumb.label}-${index}`} component={RouterLink} to={crumb.to} underline="hover" color="text.secondary">{crumb.label}</Link>)}
        </Breadcrumbs>
      </Stack>
      <Stack sx={{ width: "100%", flexDirection: { xs: "column", sm: "row" }, alignItems: { xs: "flex-start", sm: "center" }, gap: 1.5, mb: 2.5 }}>
        <Box sx={{ minWidth: 0 }}>
          {eyebrow && <Typography variant="overline" color="text.secondary">{eyebrow}</Typography>}
          <Typography variant="h1" sx={{ overflowWrap: "anywhere" }}>{title}</Typography>
          {subtitle && <Typography color="text.secondary" sx={{ mt: 0.5 }}>{subtitle}</Typography>}
        </Box>
        {actions && <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" sx={{ alignItems: "center", alignSelf: { xs: "stretch", sm: "auto" }, justifyContent: { xs: "flex-end", sm: "initial" }, ml: { sm: "auto" }, flexShrink: 0 }}>{actions}</Stack>}
      </Stack>
      {children}
    </Box>
  );
}

export function DataTable({ columns, rows, emptyTitle = "No records found", emptyDetail, footer, getKey = (row) => row.id }) {
  if (!rows?.length) return <Paper variant="outlined"><EmptyState title={emptyTitle} detail={emptyDetail} /></Paper>;
  return (
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
        <TableHead><TableRow>{columns.map((column) => <TableCell key={column.key} align={column.align || "left"} sx={column.sx}>{column.label}</TableCell>)}</TableRow></TableHead>
        <TableBody>{rows.map((row) => <TableRow hover key={getKey(row)}>{columns.map((column) => <TableCell key={column.key} align={column.align || "left"} sx={column.sx}>{column.render ? column.render(row) : row[column.key]}</TableCell>)}</TableRow>)}</TableBody>
        {footer && <TableFooter>{footer}</TableFooter>}
      </Table>
    </TableContainer>
  );
}

export function SubmitButton({ saving, children = "Save", ...props }) {
  return <Button type="submit" variant="contained" disabled={saving} startIcon={saving ? <CircularProgress color="inherit" size={16} /> : undefined} {...props}>{children}</Button>;
}

export function FormActions({ saving, submitLabel = "Save", cancelTo, onCancel }) {
  return <Stack direction="row" spacing={1} justifyContent="flex-end" sx={{ mt: 3 }}><Button component={cancelTo ? RouterLink : "button"} to={cancelTo} onClick={onCancel} color="inherit">Cancel</Button><SubmitButton saving={saving}>{submitLabel}</SubmitButton></Stack>;
}

export function PageLoading({ title = "Loading" }) {
  return <Page title={title}><LoadingState /></Page>;
}
