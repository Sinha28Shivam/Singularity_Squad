"""WorkplaceOps AI — role-based Streamlit portal.

Prototype on synthetic data. Draft-only: nothing is purchased or paid.
Run the backend first:  python -m uvicorn app.main:app --reload
Then:                   python -m streamlit run ui/app.py   (ui/streamlit_app.py also works)
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from ui.lib.api import API_BASE, api_post, clear_session, current_user, fetch_health, fetch_invoices  # noqa: E402
from ui.pages_portal import admin_console, approvals, login, user_portal  # noqa: E402

DISCLAIMER = (
    "**Prototype on synthetic data · Draft-only.** Purchase orders stay drafts, nothing is purchased "
    "and no payment is executed. Approvals record internal authorization by an eligible human approver only."
)


def sidebar() -> None:
    with st.sidebar:
        st.header("🧾 WorkplaceOps AI")
        health = fetch_health()
        if not isinstance(health, dict):
            st.error("Backend offline")
            st.caption(f"`{API_BASE}` — start with `python -m uvicorn app.main:app --reload`")
            if st.button("Retry connection"):
                fetch_health.clear()
                fetch_invoices.clear()
                st.rerun()
        else:
            st.success(f"API: {health.get('status', 'ok')}")
            if health.get("llm_enabled"):
                st.caption(f"🤖 LLM enabled · model `{health.get('model', '?')}`")
            else:
                st.caption("🧮 LLM: deterministic fallback")
            db = health.get("database")
            db_icon = {"connected": "🟢", "unavailable": "🔴", "disabled": "⚪"}.get(db, "❔")
            persistence = health.get("persistence")
            st.caption(f"{db_icon} DB: {db or 'unknown'}" + (f" · persistence {persistence}" if persistence else ""))
            if db == "unavailable" and persistence == "required":
                st.warning("Database unavailable — requests cannot be saved until it is back.")
            if st.button("Refresh status"):
                fetch_health.clear()
                st.rerun()

        user = current_user()
        if user:
            st.divider()
            st.markdown(f"Signed in as **{user.get('name') or user.get('username')}** ({user.get('role')})")
            if st.button("🚪 Logout", key="logout_btn"):
                try:
                    api_post("/api/auth/logout", None, timeout=10)
                except Exception:
                    pass  # the local session is cleared regardless
                clear_session("You have been logged out.")
                st.rerun()
        st.divider()
        st.caption("All data is synthetic. Outputs are drafts; humans make every decision.")


def main() -> None:
    st.set_page_config(page_title="WorkplaceOps AI — Request-to-Payment Copilot", page_icon="🧾", layout="wide")
    sidebar()
    st.warning(DISCLAIMER)

    user = current_user()
    if not user:
        pages = [st.Page(login.render, title="Log in", icon="🔐", url_path="login", default=True)]
    elif user.get("role") == "admin":
        pages = [
            st.Page(admin_console.render, title="Admin console", icon="🛠️", url_path="admin", default=True),
            st.Page(approvals.render, title="Approvals (read-only)", icon="🛡️", url_path="approvals"),
        ]
    elif user.get("role") == "approver":
        pages = [st.Page(approvals.render, title="Approvals", icon="🛡️", url_path="approvals", default=True)]
    else:
        pages = [st.Page(user_portal.render, title="My requests", icon="📋", url_path="my-requests", default=True)]
    st.navigation(pages).run()


main()
