from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import warnings

import streamlit as st

# ──────────────────────────────────────────────────────────────────────
# Auth survives refresh (auth-survives-refresh)
#
# The password gate lives in st.session_state, which a hard browser reload
# or a mobile-PWA force-close/reopen discards — forcing a re-login. To keep
# the session across reloads we additionally set a signed, expiring cookie on
# successful login and, on each load, skip the prompt when a valid unexpired
# cookie is present.
#
#   * The cookie carries ONLY ``subject|expiry`` plus an HMAC-SHA256 signature
#     over that payload, keyed by the server secret MHDE_DASHBOARD_COOKIE_SECRET.
#     The password / password-hash is NEVER placed in the cookie.
#   * Every load re-validates server-side; expired or tampered tokens are
#     rejected.
#   * FAIL CLOSED: if MHDE_DASHBOARD_COOKIE_SECRET is unset/empty the cookie
#     path is entirely inert and the dashboard behaves exactly as before
#     (prompt on every refresh). The feature never fails open and never crashes.
#
# Cookie attribute note: the cookie is written from JS (document.cookie) via a
# Streamlit component, so it cannot be HttpOnly. The token is HMAC-signed and
# contains no credential, but a successful XSS could replay it until expiry —
# hence the bounded TTL. See the PR description for the operator step.
# ──────────────────────────────────────────────────────────────────────

_COOKIE_NAME = "mhde_auth"
_COOKIE_SECRET_ENV = "MHDE_DASHBOARD_COOKIE_SECRET"
_COOKIE_TTL_ENV = "MHDE_DASHBOARD_COOKIE_TTL_HOURS"
_DEFAULT_TTL_HOURS = 168  # 7 days


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _check_password(password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_password(password), stored_hash)


def _cookie_secret() -> str | None:
    """Server secret for signing the auth cookie, or ``None`` when unset/empty.

    ``None`` is the fail-closed signal: callers must treat it as "cookie auth
    disabled" and fall back to prompting every load.
    """
    value = os.environ.get(_COOKIE_SECRET_ENV, "").strip()
    return value or None


def _cookie_ttl_seconds() -> int:
    """Cookie lifetime in seconds, from ``MHDE_DASHBOARD_COOKIE_TTL_HOURS``
    (default 7 days). A non-numeric value falls back to the default rather
    than crashing the page."""
    raw = os.environ.get(_COOKIE_TTL_ENV)
    if raw is None:
        return _DEFAULT_TTL_HOURS * 3600
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_HOURS * 3600
    if hours <= 0:
        return _DEFAULT_TTL_HOURS * 3600
    return int(hours * 3600)


def mint_auth_token(
    subject: str, secret: str, *, now: int | None = None, ttl_seconds: int = 3600
) -> str:
    """Signed cookie token encoding ``subject|expiry`` only.

    Format: ``base64url(subject|expiry) . hmac_sha256_hex(secret, subject|expiry)``.
    Never includes the password or its hash.
    """
    now = int(time.time()) if now is None else int(now)
    expiry = now + int(ttl_seconds)
    raw = f"{subject}|{expiry}"
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def verify_auth_token(token: str, secret: str, *, now: int | None = None) -> bool:
    """True iff ``token`` is well-formed, HMAC-valid under ``secret``, and not
    yet expired. Never raises on malformed input — returns ``False``."""
    if not token or not secret:
        return False
    now = int(time.time()) if now is None else int(now)
    try:
        b64, sig = token.split(".", 1)
        padding = "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64 + padding).decode()
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        _subject, exp = raw.rsplit("|", 1)
        return now < int(exp)
    except Exception:
        return False


def _should_skip_prompt(
    *,
    session_authenticated: bool,
    secret: str | None,
    cookie_token: str | None,
    now: int | None = None,
) -> bool:
    """Core auth decision, side-effect free and unit-tested.

    Skip the login prompt when either the in-memory session is already
    authenticated, or (only when a server secret is configured) a valid
    unexpired cookie is present. With ``secret is None`` the cookie branch is
    never taken — the fail-closed path.
    """
    if session_authenticated:
        return True
    if secret and cookie_token and verify_auth_token(cookie_token, secret, now=now):
        return True
    return False


def _read_auth_cookie() -> str | None:
    """Read the auth cookie the browser sent on this request, or ``None``.

    Uses ``st.context.cookies`` (Streamlit ≥ 1.42), guarded so a missing
    attribute or any access error degrades to "no cookie" rather than crashing.
    """
    try:
        cookies = st.context.cookies
        return cookies.get(_COOKIE_NAME) if cookies else None
    except Exception:
        return None


def _write_auth_cookie(token: str, ttl_seconds: int) -> None:
    """Set the auth cookie in the browser via a tiny JS component.

    ``st.components.v1.html`` renders a ``srcdoc`` iframe that inherits the
    parent document's origin, so ``document.cookie`` writes land on the
    dashboard's own domain. ``Secure`` + ``SameSite=Strict`` are set; HttpOnly
    is unavailable to JS (documented tradeoff above)."""
    try:
        import streamlit.components.v1 as components

        # JSON-encode to safely embed the token in the script literal.
        import json as _json

        token_js = _json.dumps(token)
        components.html(
            f"""
            <script>
            (function() {{
              var t = {token_js};
              var maxAge = {int(ttl_seconds)};
              document.cookie = "{_COOKIE_NAME}=" + t +
                "; path=/; max-age=" + maxAge + "; SameSite=Strict; Secure";
            }})();
            </script>
            """,
            height=0,
        )
    except Exception:
        # Never let a cookie-write failure break login; session-state auth
        # still holds for this session.
        pass


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

    secret = _cookie_secret()

    # Already authenticated this session, or a valid signed cookie is present
    # (cookie path inert when no secret is configured — fail closed).
    if _should_skip_prompt(
        session_authenticated=bool(st.session_state["authenticated"]),
        secret=secret,
        cookie_token=_read_auth_cookie(),
    ):
        st.session_state["authenticated"] = True
        return

    st.title("MHDE — Login")
    with st.form("login"):
        user_input = st.text_input("Username")
        pass_input = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        if user_input == username and _check_password(pass_input, password_hash):
            st.session_state["authenticated"] = True
            # Persist across hard refresh / PWA reopen only when a secret is
            # configured; otherwise behaviour is unchanged (prompt each load).
            if secret:
                ttl = _cookie_ttl_seconds()
                _write_auth_cookie(mint_auth_token(username, secret, ttl_seconds=ttl), ttl)
            st.rerun()
        else:
            st.error("Invalid credentials.")

    st.stop()


def generate_password_hash(password: str) -> str:
    """Utility: generate a password hash for .env."""
    return _hash_password(password)
