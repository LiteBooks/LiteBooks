import { createTheme } from "@mui/material/styles";

export const drawerWidth = 244;

export const theme = createTheme({
  cssVariables: true,
  palette: {
    mode: "light",
    primary: { main: "#176B52", dark: "#104C3B", light: "#DDEFE8", contrastText: "#FFFFFF" },
    secondary: { main: "#A75B27", dark: "#773E18", light: "#F6E6D8" },
    info: { main: "#356F95" },
    success: { main: "#2F7D5E" },
    warning: { main: "#AD6B18" },
    error: { main: "#B4433D" },
    background: { default: "#F5F7F6", paper: "#FFFFFF" },
    text: { primary: "#18262C", secondary: "#647278" },
    divider: "#DDE3E1",
  },
  shape: { borderRadius: 6 },
  typography: {
    fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    h1: { fontSize: "1.75rem", lineHeight: 1.25, fontWeight: 700, letterSpacing: 0 },
    h2: { fontSize: "1.15rem", lineHeight: 1.35, fontWeight: 700, letterSpacing: 0 },
    h3: { fontSize: "1rem", lineHeight: 1.4, fontWeight: 700, letterSpacing: 0 },
    button: { fontWeight: 700, textTransform: "none", letterSpacing: 0 },
    overline: { fontSize: "0.7rem", lineHeight: 1.6, fontWeight: 700, letterSpacing: 0 },
  },
  components: {
    MuiCssBaseline: { styleOverrides: { body: { minWidth: 320 }, "*:focus-visible": { outlineOffset: 2 } } },
    MuiButton: {
      defaultProps: { disableElevation: true, size: "small" },
      styleOverrides: {
        root: { minHeight: 30, borderRadius: 5, padding: "3px 9px", fontSize: "0.78rem", lineHeight: 1.5 },
        sizeLarge: { minHeight: 42, padding: "8px 16px", fontSize: "0.875rem" },
        startIcon: { marginRight: 5 },
        endIcon: { marginLeft: 5 },
      },
    },
    MuiIconButton: { styleOverrides: { root: { borderRadius: 5 } } },
    MuiPaper: { defaultProps: { elevation: 0 }, styleOverrides: { root: { backgroundImage: "none" } } },
    MuiCard: { defaultProps: { variant: "outlined" }, styleOverrides: { root: { borderRadius: 6 } } },
    MuiTableCell: {
      styleOverrides: {
        head: { color: "#56666C", backgroundColor: "#F7F9F8", fontSize: "0.72rem", fontWeight: 700, textTransform: "uppercase" },
        root: { borderColor: "#E2E7E5" },
      },
    },
    MuiTextField: { defaultProps: { size: "small", fullWidth: true } },
    MuiFormControl: { defaultProps: { size: "small", fullWidth: true } },
    MuiChip: { styleOverrides: { root: { borderRadius: 4, fontWeight: 600 } } },
    MuiTooltip: { defaultProps: { arrow: true } },
  },
});
