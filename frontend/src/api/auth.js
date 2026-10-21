import { api, setToken } from "./client";

export async function login(username, password) {
  const out = await api.post("/api/auth/login", { username, password }, { auth: false });
  setToken(out.token);
  return out.user;
}

export async function signup({ username, name, email, password }) {
  const out = await api.post("/api/auth/signup", { username, name, email, password }, { auth: false });
  setToken(out.token);
  return out.user;
}

export async function me() {
  return api.get("/api/auth/me");
}

export async function logout() {
  try { await api.post("/api/auth/logout"); } catch { /* token may already be invalid */ }
  setToken(null);
}
