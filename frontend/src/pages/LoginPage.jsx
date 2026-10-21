import { useState } from "react";
import { useAuth } from "../lib/auth";
import { InlineError, Notice } from "../components/States";

/** Sign in / create account (spec §6.1). Account creation uses the existing /api/auth/signup (role: user). */
export default function LoginPage() {
  const { login, signup, notice } = useAuth();
  const [mode, setMode] = useState("signin");
  const [form, setForm] = useState({ username: "", password: "", name: "", email: "" });
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setPending(true); setError(null);
    try {
      if (mode === "signin") await login(form.username.trim(), form.password);
      else await signup({ username: form.username.trim(), name: form.name.trim(), email: form.email.trim(), password: form.password });
    } catch (err) {
      setError(err);
      setPending(false);
    }
  }

  const signin = mode === "signin";
  return (
    <div className="auth-page">
      <main className="auth-card">
        <div className="stack-sm">
          <div className="brand" style={{ padding: 0 }}><span className="brand-mark" aria-hidden="true">W</span>WorkplaceOps AI</div>
          <h1>{signin ? "Sign in" : "Create an account"}</h1>
          <p className="muted">Submit workplace requests and follow them through review and approval.</p>
        </div>
        {notice && <Notice tone="info">{notice}</Notice>}
        <form className="card stack" onSubmit={submit} noValidate>
          {!signin && (
            <>
              <div className="field">
                <label htmlFor="name" className="required">Full name</label>
                <input id="name" className="input" value={form.name} onChange={set("name")} autoComplete="name" required maxLength={80} />
              </div>
              <div className="field">
                <label htmlFor="email" className="required">Work email</label>
                <input id="email" type="email" className="input" value={form.email} onChange={set("email")} autoComplete="email" required maxLength={254} />
              </div>
            </>
          )}
          <div className="field">
            <label htmlFor="username" className="required">Username</label>
            <input id="username" className="input" value={form.username} onChange={set("username")} autoComplete="username" required maxLength={64} />
            {!signin && <span className="help">3–32 characters: letters, numbers, dot, dash or underscore.</span>}
          </div>
          <div className="field">
            <label htmlFor="password" className="required">Password</label>
            <input id="password" type="password" className="input" value={form.password} onChange={set("password")}
              autoComplete={signin ? "current-password" : "new-password"} required maxLength={128} />
            {!signin && <span className="help">At least 8 characters.</span>}
          </div>
          <InlineError error={error} />
          <button type="submit" className="btn btn-primary" disabled={pending || !form.username || !form.password}>
            {pending ? "Please wait…" : signin ? "Sign in" : "Create account"}
          </button>
        </form>
        <p className="small muted" style={{ textAlign: "center" }}>
          {signin ? "New here? " : "Already have an account? "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setMode(signin ? "signup" : "signin"); setError(null); }}>
            {signin ? "Create an account" : "Sign in instead"}
          </button>
        </p>
      </main>
    </div>
  );
}
