import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { LogOut, Menu, X } from "lucide-react";
import { useAuth } from "../lib/auth";
import { navForRole, pageTitle } from "../routes";
import { useHealth } from "../lib/health";

const ROLE_LABEL = { user: "Requester", admin: "Admin", approver: "Approver" };

/** Global shell: narrow sidebar (collapsible below 900px), top bar with page title + user menu. */
export default function AppLayout() {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();
  const health = useHealth();
  const [open, setOpen] = useState(false);
  useEffect(() => { setOpen(false); }, [pathname]);

  const sections = navForRole(user?.role, { paymentsEnabled: Boolean(health?.payments_simulation_enabled) });

  return (
    <div className="shell">
      <a href="#main" className="sr-only">Skip to content</a>
      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />}
      <aside className="sidebar" data-open={open} aria-label="Main navigation">
        <div className="row-between">
          <div className="brand"><span className="brand-mark" aria-hidden="true">W</span>WorkplaceOps AI</div>
          <button type="button" className="btn btn-ghost icon-btn menu-button" onClick={() => setOpen(false)} aria-label="Close menu">
            <X size={18} />
          </button>
        </div>
        <nav className="nav">
          {sections.map((s) => (
            <div key={s.title} className="stack-sm" style={{ gap: 2 }}>
              <div className="nav-section">{s.title}</div>
              {s.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end}>
                  <item.icon size={16} aria-hidden="true" />{item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div>
            <div style={{ fontWeight: 500 }}>{user?.name || user?.username}</div>
            <div className="muted small">{ROLE_LABEL[user?.role] || user?.role} · {user?.username}</div>
          </div>
          <button type="button" className="btn btn-sm" onClick={logout}><LogOut size={14} aria-hidden="true" />Sign out</button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="row">
            <button type="button" className="btn btn-ghost icon-btn menu-button" onClick={() => setOpen(true)} aria-label="Open menu">
              <Menu size={18} />
            </button>
            <span className="muted small">{pageTitle(pathname)}</span>
          </div>
          <span className="muted small">{ROLE_LABEL[user?.role] || ""}</span>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
