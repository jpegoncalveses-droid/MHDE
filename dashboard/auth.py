from __future__ import annotations

import hashlib
import hmac
import os
import warnings

import streamlit as st


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _check_password(password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_password(password), stored_hash)


def require_auth() -> None:
    """Block dashboard until authenticated. Call at the top of every page."""
    enabled = os.environ.get("MHDE_DASHBOARD_AUTH_ENABLED", "true").lower()

    if enabled in ("false", "0", "no"):
        warnings.warn(
            "Dashboard auth is disabled. Do not expose to the public internet.",
            stacklevel=2,
        )
        st.sidebar.warning("Auth disabled — local mode only.")
        return

    username = os.environ.get("MHDE_DASHBOARD_USERNAME", "admin")
    password_hash = os.environ.get("MHDE_DASHBOARD_PASSWORD_HASH", "")

    if not password_hash:
        st.error(
            "MHDE_DASHBOARD_PASSWORD_HASH is not set. "
            "Set it to sha256(password) to enable the dashboard."
        )
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if st.session_state["authenticated"]:
        return

    st.title("MHDE — Login")
    with st.form("login"):
        user_input = st.text_input("Username")
        pass_input = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        if user_input == username and _check_password(pass_input, password_hash):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid credentials.")

    st.stop()


def generate_password_hash(password: str) -> str:
    """Utility: generate a password hash for .env."""
    return _hash_password(password)
