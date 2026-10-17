"""Login / signup page."""
from __future__ import annotations

import re

import streamlit as st

from ui.lib.api import api_post, friendly_error

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_signup(username: str, name: str, email: str, password: str, confirm: str) -> list[str]:
    """Client-side checks mirroring SignupIn (the backend validates again)."""
    errors = []
    if not USERNAME_RE.match(username.strip()):
        errors.append("Username must be 3–32 characters: letters, digits, `_`, `.` or `-`.")
    if not name.strip() or len(name.strip()) > 80:
        errors.append("Name is required (max 80 characters).")
    if not EMAIL_RE.match(email.strip()) or len(email.strip()) > 254:
        errors.append("Please enter a valid email address.")
    if len(password) < 8 or len(password) > 128:
        errors.append("Password must be at least 8 characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors


def _store_auth(data: object) -> bool:
    if not isinstance(data, dict) or not data.get("token") or not isinstance(data.get("user"), dict):
        st.error("Unexpected response from the backend.")
        return False
    st.session_state["auth"] = {
        "token": data["token"],
        "expires_at": data.get("expires_at"),
        "user": data["user"],
    }
    st.session_state.pop("auth_notice", None)
    return True


def render() -> None:
    st.title("🧾 WorkplaceOps AI — Request-to-Payment Copilot")
    notice = st.session_state.get("auth_notice")
    if notice:
        st.info(notice)

    tab_login, tab_signup = st.tabs(["Log in", "Create account"])
    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Username", key="login_username")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Log in", type="primary")
        if submitted:
            if not username.strip() or not password:
                st.error("Please enter your username and password.")
            else:
                try:
                    data = api_post("/api/auth/login", {"username": username.strip(), "password": password},
                                    timeout=15)
                except Exception as exc:
                    st.error(friendly_error(exc))
                else:
                    if _store_auth(data):
                        st.rerun()

    with tab_signup:
        with st.form("signup_form"):
            s_username = st.text_input("Username", key="signup_username",
                                       help="3–32 characters: letters, digits, _ . -")
            s_name = st.text_input("Full name", key="signup_name")
            s_email = st.text_input("Email", key="signup_email")
            s_password = st.text_input("Password", type="password", key="signup_password",
                                       help="At least 8 characters")
            s_confirm = st.text_input("Confirm password", type="password", key="signup_confirm")
            s_submitted = st.form_submit_button("Create account", type="primary")
        if s_submitted:
            errors = validate_signup(s_username, s_name, s_email, s_password, s_confirm)
            if errors:
                for e in errors:
                    st.error(e)
            else:
                try:
                    data = api_post("/api/auth/signup", {
                        "username": s_username.strip(), "name": s_name.strip(),
                        "email": s_email.strip(), "password": s_password,
                    }, timeout=15)
                except Exception as exc:
                    st.error(friendly_error(exc))
                else:
                    if _store_auth(data):
                        st.rerun()
    st.caption("New accounts are requesters. Admin accounts are configured by operations.")
