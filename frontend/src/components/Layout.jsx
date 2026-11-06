import { useEffect, useRef, useState } from "react";
import { Link as RouterLink, NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Avatar, Badge, Box, ButtonBase, Collapse, Divider, Drawer, IconButton, List, ListItemButton, ListItemIcon,
  ListItemText, ListSubheader, Menu, MenuItem, Stack, Toolbar, Tooltip, Typography, useMediaQuery,
} from "@mui/material";
import AccountBalanceRounded from "@mui/icons-material/AccountBalanceRounded";
import AssessmentRounded from "@mui/icons-material/AssessmentRounded";
import AutoAwesomeRounded from "@mui/icons-material/AutoAwesomeRounded";
import ContactsRounded from "@mui/icons-material/ContactsRounded";
import DashboardRounded from "@mui/icons-material/DashboardRounded";
import DescriptionRounded from "@mui/icons-material/DescriptionRounded";
import FactCheckRounded from "@mui/icons-material/FactCheckRounded";
import ExpandMoreRounded from "@mui/icons-material/ExpandMoreRounded";
import GroupRounded from "@mui/icons-material/GroupRounded";
import LockRounded from "@mui/icons-material/LockRounded";
import LogoutRounded from "@mui/icons-material/LogoutRounded";
import ManageAccountsRounded from "@mui/icons-material/ManageAccountsRounded";
import MenuRounded from "@mui/icons-material/MenuRounded";
import PeopleRounded from "@mui/icons-material/PeopleRounded";
import ReceiptLongRounded from "@mui/icons-material/ReceiptLongRounded";
import WalletRounded from "@mui/icons-material/WalletRounded";
import PieChartRounded from "@mui/icons-material/PieChartRounded";
import SettingsRounded from "@mui/icons-material/SettingsRounded";
import SystemUpdateAltRounded from "@mui/icons-material/SystemUpdateAltRounded";
import { useTheme } from "@mui/material/styles";
import { api } from "../api";
import { useApp } from "../context";
import { drawerWidth } from "../theme";

const muted = "rgba(255,255,255,.55)";
const itemColor = "rgba(255,255,255,.8)";
const hover = "rgba(255,255,255,.08)";
const labelProps = { primary: { sx: { fontSize: 14, fontWeight: 600, lineHeight: 1.3 } } };

// Every link, grouped the way people look for them. `show` hides links the user's role cannot open.
function navGroups(session) {
  const admin = session.permissions.administer;
  return [
    { title: null, items: [{ label: "Overview", to: "/", Icon: DashboardRounded }] },
    { title: "Books", items: [
      { label: "Transactions", to: "/transactions/", Icon: ReceiptLongRounded },
      { label: "Accounts", to: "/accounts/", Icon: AccountBalanceRounded },
      { label: "Invoices & bills", to: "/documents/", Icon: DescriptionRounded },
      { label: "Contacts", to: "/contacts/", Icon: ContactsRounded },
      { label: "Owners", to: "/owners/", Icon: PeopleRounded },
    ] },
    { title: "Reporting", items: [
      { label: "Reports", to: "/reports/", Icon: AssessmentRounded },
      { label: "Audit history", to: "/audit/", Icon: FactCheckRounded },
    ] },
    // Kept apart from the books on purpose: these are owners' own expenses, never company ones.
    { title: "Outside the books", items: [{ label: "Founder expense log", to: "/founder-expenses/", Icon: WalletRounded }] },
    { title: "Settings", collapsible: true, items: [
      { label: "Partnership", to: "/settings/partnership/", Icon: PieChartRounded, show: admin },
      { label: "Period locks", to: "/settings/periods/", Icon: LockRounded, show: admin },
      { label: "Users", to: "/settings/users/", Icon: GroupRounded, show: admin },
      { label: "AI access", to: "/settings/ai/", Icon: AutoAwesomeRounded },
      { label: "Software update", to: "/settings/updates/", Icon: SystemUpdateAltRounded, show: admin, badge: Boolean(session.update_available) },
    ] },
  ].map((group) => ({ ...group, items: group.items.filter((item) => item.show !== false) })).filter((group) => group.items.length);
}

function isActive(pathname, to) {
  return to === "/" ? pathname === "/" : pathname.startsWith(to);
}

function NavItem({ label, to, Icon, onClick, badge = false, active }) {
  const ref = useRef(null);
  // Keep the current page's link visible when the nav is taller than the window.
  useEffect(() => { if (active) ref.current?.scrollIntoView?.({ block: "nearest" }); }, [active]);
  return (
    <ListItemButton ref={ref} component={NavLink} to={to} onClick={onClick} selected={active} sx={{ mx: 1, my: 0.25, minHeight: 38, py: 0.5, borderRadius: 1, color: itemColor, "& .MuiListItemIcon-root": { color: "inherit" }, "&.Mui-selected": { color: "#17372F", bgcolor: "#E5F0EC", "&:hover": { bgcolor: "#E5F0EC" } }, "&:hover": { bgcolor: hover } }}>
      <ListItemIcon sx={{ minWidth: 36 }}><Badge color="secondary" variant="dot" invisible={!badge}><Icon fontSize="small" /></Badge></ListItemIcon>
      <ListItemText primary={label} slotProps={labelProps} />
    </ListItemButton>
  );
}

function GroupTitle({ children }) {
  return <ListSubheader disableSticky sx={{ bgcolor: "transparent", color: muted, fontSize: 11, fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase", lineHeight: 1, px: 2.5, pt: 2, pb: 0.75 }}>{children}</ListSubheader>;
}

function NavGroup({ group, pathname, onNavigate }) {
  const containsActive = group.items.some((item) => isActive(pathname, item.to));
  const [expanded, setExpanded] = useState(containsActive);
  const items = group.items.map((item) => <NavItem key={item.to} {...item} active={isActive(pathname, item.to)} onClick={onNavigate} />);
  if (!group.title) return <List disablePadding sx={{ pt: 1 }}>{items}</List>;
  if (!group.collapsible) return <List disablePadding subheader={<GroupTitle>{group.title}</GroupTitle>}>{items}</List>;
  const open = expanded || containsActive;
  const attention = !open && group.items.some((item) => item.badge);
  return (
    <List disablePadding sx={{ pt: 1 }}>
      <ListItemButton onClick={() => setExpanded(!open)} aria-expanded={open} sx={{ mx: 1, minHeight: 38, py: 0.5, borderRadius: 1, color: itemColor, "&:hover": { bgcolor: hover } }}>
        <ListItemIcon sx={{ minWidth: 36, color: "inherit" }}><Badge color="secondary" variant="dot" invisible={!attention}><SettingsRounded fontSize="small" /></Badge></ListItemIcon>
        <ListItemText primary={group.title} slotProps={labelProps} />
        <ExpandMoreRounded fontSize="small" sx={{ color: muted, transition: "transform .15s", transform: open ? "rotate(180deg)" : "none" }} />
      </ListItemButton>
      <Collapse in={open} timeout="auto" unmountOnExit><Box sx={{ pl: 1.5 }}>{items}</Box></Collapse>
    </List>
  );
}

export default function Layout({ children }) {
  const theme = useTheme();
  const mobile = useMediaQuery(theme.breakpoints.down("md"));
  const [open, setOpen] = useState(false);
  const [userMenuAnchor, setUserMenuAnchor] = useState(null);
  const { session, refreshSession } = useApp();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const close = () => setOpen(false);
  const closeUserMenu = () => setUserMenuAnchor(null);
  const openProfile = () => { closeUserMenu(); navigate("/settings/profile/"); };
  const signOut = async () => { closeUserMenu(); await api("/api/auth/logout/", { method: "POST" }); await refreshSession(); navigate("/login/"); };
  const drawer = (
    <Stack sx={{ height: "100%", bgcolor: "#18332D", color: "white" }}>
      <Toolbar sx={{ px: 2.25, minHeight: "64px !important", flexShrink: 0 }}>
        <Stack component={RouterLink} to="/" direction="row" spacing={1.25} sx={{ alignItems: "center", color: "#FFFFFF", textDecoration: "none", "&:visited": { color: "#FFFFFF" } }} onClick={close}>
          <Avatar variant="rounded" sx={{ width: 34, height: 34, bgcolor: "secondary.main", fontSize: 12, fontWeight: 900 }}>LB</Avatar>
          <Typography variant="h3" color="inherit">LiteBooks</Typography>
        </Stack>
      </Toolbar>
      <Divider sx={{ borderColor: "rgba(255,255,255,.1)" }} />
      <Box component="div" sx={{ flex: 1, minHeight: 0, overflowY: "auto", pb: 2, scrollbarWidth: "thin", scrollbarColor: "rgba(255,255,255,.2) transparent" }}>
        {navGroups(session).map((group) => <NavGroup key={group.title || "home"} group={group} pathname={pathname} onNavigate={close} />)}
      </Box>
    </Stack>
  );
  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <AppBar position="fixed" color="inherit" elevation={0} sx={{ ml: { md: `${drawerWidth}px` }, width: { md: `calc(100% - ${drawerWidth}px)` }, borderBottom: 1, borderColor: "divider", bgcolor: "rgba(255,255,255,.94)", backdropFilter: "blur(8px)" }}>
        <Toolbar sx={{ minHeight: "64px !important", px: { xs: 1.5, sm: 3 } }}>
          {mobile && <Tooltip title="Open navigation"><IconButton onClick={() => setOpen(true)} edge="start" aria-label="Open navigation"><MenuRounded /></IconButton></Tooltip>}
          <Box sx={{ flex: 1 }} />
          <Tooltip title="User menu">
            <ButtonBase
              aria-label="Open user menu"
              aria-controls={userMenuAnchor ? "user-menu" : undefined}
              aria-haspopup="menu"
              aria-expanded={userMenuAnchor ? "true" : undefined}
              onClick={(event) => setUserMenuAnchor(event.currentTarget)}
              sx={{ borderRadius: 1, p: 0.5, pl: { sm: 1 }, gap: 1, "&:hover": { bgcolor: "action.hover" } }}
            >
              <Typography variant="body2" sx={{ fontWeight: 700, display: { xs: "none", sm: "block" } }}>{session.user.name}</Typography>
              <Avatar sx={{ width: 34, height: 34, bgcolor: "info.main", fontSize: 13 }}>{session.user.name.slice(0, 2).toUpperCase()}</Avatar>
              <ExpandMoreRounded fontSize="small" sx={{ display: { xs: "none", sm: "block" }, color: "text.secondary" }} />
            </ButtonBase>
          </Tooltip>
          <Menu id="user-menu" anchorEl={userMenuAnchor} open={Boolean(userMenuAnchor)} onClose={closeUserMenu} anchorOrigin={{ vertical: "bottom", horizontal: "right" }} transformOrigin={{ vertical: "top", horizontal: "right" }}>
            <MenuItem onClick={openProfile}><ListItemIcon><ManageAccountsRounded fontSize="small" /></ListItemIcon>Profile settings</MenuItem>
            <Divider />
            <MenuItem onClick={signOut}><ListItemIcon><LogoutRounded fontSize="small" /></ListItemIcon>Sign out</MenuItem>
          </Menu>
        </Toolbar>
      </AppBar>
      <Box component="nav" aria-label="Primary navigation" sx={{ width: { md: drawerWidth }, flexShrink: { md: 0 } }}>
        <Drawer variant={mobile ? "temporary" : "permanent"} open={mobile ? open : true} onClose={close} ModalProps={{ keepMounted: true }} sx={{ "& .MuiDrawer-paper": { width: drawerWidth, border: 0 } }}>{drawer}</Drawer>
      </Box>
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, pt: "64px" }}>
        <Box sx={{ width: "100%", maxWidth: 1480, mx: "auto", p: { xs: 2, sm: 3, lg: 4 }, pb: 8 }}>{children}</Box>
      </Box>
    </Box>
  );
}
