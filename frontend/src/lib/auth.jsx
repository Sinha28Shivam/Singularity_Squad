import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import * as authApi from "../api/auth";
import { getToken, onUnauthorized, setToken } from "../api/client";

const AuthContext = createContext(null);

/** Session state. The role shown here only shapes navigation; the backend enforces every permission. */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(Boolean(getToken()));
  const [notice, setNotice] = useState(null);

  useEffect(() => {
    onUnauthorized(() => { setUser(null); setNotice("Your session has ended. Please sign in again."); });
    if (!getToken()) return;
    authApi.me().then(setUser).catch(() => { setToken(null); setUser(null); }).finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (u, p) => { const usr = await authApi.login(u, p); setNotice(null); setUser(usr); return usr; }, []);
  const signup = useCallback(async (data) => { const usr = await authApi.signup(data); setNotice(null); setUser(usr); return usr; }, []);
  const logout = useCallback(async () => { await authApi.logout(); setUser(null); }, []);

  const value = useMemo(() => ({ user, loading, notice, login, signup, logout, setNotice }),
    [user, loading, notice, login, signup, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
