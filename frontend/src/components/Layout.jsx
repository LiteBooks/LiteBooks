import { useState } from "react";
import { Link as RouterLink, NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Avatar, Box, ButtonBase, Divider, Drawer, IconButton, List, ListItemButton, ListItemIcon,
  ListItemText, Menu, MenuItem, Stack, Toolbar, Tooltip, Typography, useMediaQuery,
} from "@mui/material";
import AccountBalanceRounded from "@mui/icons-material/AccountBalanceRounded";
import AssessmentRounded from "@mui/icons-material/AssessmentRounded";
import ContactsRounded from "@mui/icons-material/ContactsRounded";
import DashboardRounded from "@mui/icons-material/DashboardRounded";
import DescriptionRounded from "@mui/icons-material/DescriptionRounded";
import FactCheckRounded from "@mui/icons-material/FactCheckRounded";
import ExpandMoreRounded from "@mui/icons-material/ExpandMoreRounded";
import LogoutRounded from "@mui/icons-material/LogoutRounded";
import ManageAccountsRounded from "@mui/icons-material/ManageAccountsRounded";
import MenuRounded from "@mui/icons-material/MenuRounded";
import PeopleRounded from "@mui/icons-material/PeopleRounded";
import ReceiptLongRounded from "@mui/icons-material/ReceiptLongRounded";
import SettingsRounded from "@mui/icons-material/SettingsRounded";
import { useTheme } from "@mui/material/styles";
import { api } from "../api";
import { useApp } from "../context";
import { drawerWidth } from "../theme";

const mainItems = [
  ["Overview", "/", DashboardRounded], ["Transactions", "/transactions/", ReceiptLongRounded],
  ["Accounts", "/accounts/", AccountBalanceRounded], ["Invoices & bills", "/documents/", DescriptionRounded],
  ["Contacts", "/contacts/", ContactsRounded], ["Owners", "/owners/", PeopleRounded],
  ["Reports", "/reports/", AssessmentRounded],
];

function NavItem({ label, to, Icon, onClick }) {
  const location = useLocation();
  const active = to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);
  return (
    <ListItemButton component={NavLink} to={to} onClick={onClick} selected={active} sx={{ mx: 1, my: 0.25, minHeight: 42, borderRadius: 1, color: "rgba(255,255,255,.78)", "& .MuiListItemIcon-root": { color: "inherit" }, "&.Mui-selected": { color: "#17372F", bgcolor: "#E5F0EC", "&:hover": { bgcolor: "#E5F0EC" } }, "&:hover": { bgcolor: "rgba(255,255,255,.08)" } }}>
      <ListItemIcon sx={{ minWidth: 38 }}><Icon fontSize="small" /></ListItemIcon><ListItemText primary={label} primaryTypographyProps={{ fontSize: 14, fontWeight: 600 }} />
    </ListItemButton>
  );
}

export default function Layout({ children }) {
  const theme = useTheme();
  const mobile = useMediaQuery(theme.breakpoints.down("md"));
  const [open, setOpen] = useState(false);
  const [userMenuAnchor, setUserMenuAnchor] = useState(null);
  const { session, refreshSession } = useApp();
  const navigate = useNavigate();
  const close = () => setOpen(false);
  const closeUserMenu = () => setUserMenuAnchor(null);
  const openProfile = () => { closeUserMenu(); navigate("/settings/profile/"); };
  const signOut = async () => { closeUserMenu(); await api("/api/auth/logout/", { method: "POST" }); await refreshSession(); navigate("/login/"); };
  const drawer = (
    <Stack sx={{ height: "100%", bgcolor: "#18332D", color: "white" }}>
      <Toolbar sx={{ px: 2.25, minHeight: "64px !important" }}>
        <Stack component={RouterLink} to="/" direction="row" alignItems="center" spacing={1.25} sx={{ color: "#FFFFFF", textDecoration: "none", "&:visited": { color: "#FFFFFF" } }} onClick={close}>
          <Avatar variant="rounded" sx={{ width: 34, height: 34, bgcolor: "secondary.main", fontSize: 12, fontWeight: 900 }}>LB</Avatar>
          <Typography variant="h3" color="inherit">LiteBooks</Typography>
        </Stack>
      </Toolbar>
      <Divider sx={{ borderColor: "rgba(255,255,255,.1)" }} />
      <List sx={{ pt: 1.5 }}>{mainItems.map(([label, to, Icon]) => <NavItem key={to} label={label} to={to} Icon={Icon} onClick={close} />)}</List>
      <Box sx={{ mt: "auto", pb: 1.5 }}>
        {session.permissions.administer && <><NavItem label="Period locks" to="/settings/periods/" Icon={SettingsRounded} onClick={close} /><NavItem label="Users" to="/settings/users/" Icon={PeopleRounded} onClick={close} /></>}
        <NavItem label="Audit history" to="/audit/" Icon={FactCheckRounded} onClick={close} />
        <ListItemButton onClick={signOut} sx={{ mx: 1, mt: 0.25, borderRadius: 1, color: "rgba(255,255,255,.78)", "&:hover": { bgcolor: "rgba(255,255,255,.08)" } }}>
          <ListItemIcon sx={{ minWidth: 38, color: "inherit" }}><LogoutRounded fontSize="small" /></ListItemIcon><ListItemText primary="Sign out" primaryTypographyProps={{ fontSize: 14, fontWeight: 600 }} />
        </ListItemButton>
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
              <Typography variant="body2" fontWeight={700} sx={{ display: { xs: "none", sm: "block" } }}>{session.user.name}</Typography>
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
