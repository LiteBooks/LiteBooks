import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Alert, Snackbar } from "@mui/material";
import { api } from "./api";

const AppContext = createContext(null);

export function AppProvider({ children }) {
  const [session, setSession] = useState(null);
  const [options, setOptions] = useState(null);
  const [notice, setNotice] = useState(null);

  const refreshSession = async () => {
    const data = await api("/api/auth/session/");
    setSession(data);
    return data;
  };

  const refreshOptions = async () => {
    const data = await api("/api/options/");
    setOptions(data);
    return data;
  };

  useEffect(() => { refreshSession().catch((error) => setSession({ authenticated: false, error: error.message })); }, []);
  useEffect(() => {
    if (session?.authenticated) refreshOptions().catch((error) => setNotice({ severity: "error", message: error.message }));
    else setOptions(null);
  }, [session?.authenticated]);

  const value = useMemo(() => ({ session, options, refreshSession, refreshOptions, notify: (message, severity = "success") => setNotice({ message, severity }) }), [session, options]);
  return (
    <AppContext.Provider value={value}>
      {children}
      <Snackbar open={Boolean(notice)} autoHideDuration={4500} onClose={() => setNotice(null)} anchorOrigin={{ vertical: "bottom", horizontal: "right" }}>
        {notice ? <Alert severity={notice.severity} variant="filled" onClose={() => setNotice(null)}>{notice.message}</Alert> : <span />}
      </Snackbar>
    </AppContext.Provider>
  );
}

export function useApp() {
  return useContext(AppContext);
}

