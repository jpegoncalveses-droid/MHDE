"""Unit tests for the signed, expiring auth cookie (auth-survives-refresh).

The cookie carries only ``subject|expiry`` plus an HMAC signature over that
payload, keyed by ``MHDE_DASHBOARD_COOKIE_SECRET``. The password and its hash
are NEVER placed in the cookie. Validation is server-side on every load and
the feature FAILS CLOSED: with no secret configured the cookie path is inert
and the dashboard prompts on every refresh exactly as before.

Only the pure token/decision helpers are unit-tested; the Streamlit wiring in
``require_auth`` (reading ``st.context.cookies`` / writing via a component) is
not, consistent with the rest of ``dashboard/``.
"""
from __future__ import annotations

import pytest

from dashboard import auth

SECRET = "test-server-secret-value"
SUBJECT = "admin"
NOW = 1_900_000_000  # fixed epoch seconds


# ── mint_auth_token / verify_auth_token roundtrip ────────────────────

def test_mint_then_verify_roundtrip_within_ttl():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    assert auth.verify_auth_token(token, SECRET, now=NOW + 60) is True


def test_verify_rejects_expired_token():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    # one second past expiry
    assert auth.verify_auth_token(token, SECRET, now=NOW + 3601) is False


def test_verify_rejects_tampered_payload():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    b64, sig = token.split(".", 1)
    # flip a character in the signature → HMAC mismatch
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert auth.verify_auth_token(f"{b64}.{bad_sig}", SECRET, now=NOW) is False


def test_verify_rejects_wrong_secret():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    assert auth.verify_auth_token(token, "a-different-secret", now=NOW) is False


def test_verify_rejects_malformed_token_without_raising():
    for bad in ("", "no-dot-here", "not.base64.$$$", "....", "a.b"):
        assert auth.verify_auth_token(bad, SECRET, now=NOW) is False


def test_token_never_contains_password_or_hash():
    # The token must encode only subject + expiry, never a credential.
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    assert SECRET not in token
    # A password the user might have typed must not appear either.
    assert "hunter2" not in token


# ── _cookie_secret (fail-closed wiring) ──────────────────────────────

def test_cookie_secret_none_when_unset(monkeypatch):
    monkeypatch.delenv("MHDE_DASHBOARD_COOKIE_SECRET", raising=False)
    assert auth._cookie_secret() is None


def test_cookie_secret_none_when_empty(monkeypatch):
    monkeypatch.setenv("MHDE_DASHBOARD_COOKIE_SECRET", "")
    assert auth._cookie_secret() is None


def test_cookie_secret_value_when_set(monkeypatch):
    monkeypatch.setenv("MHDE_DASHBOARD_COOKIE_SECRET", SECRET)
    assert auth._cookie_secret() == SECRET


# ── _should_skip_prompt (the core auth decision) ─────────────────────

def test_skip_prompt_when_session_already_authenticated():
    # An authenticated session short-circuits regardless of cookie/secret.
    assert auth._should_skip_prompt(
        session_authenticated=True, secret=None, cookie_token=None, now=NOW
    ) is True


def test_skip_prompt_with_valid_cookie_and_secret():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    assert auth._should_skip_prompt(
        session_authenticated=False, secret=SECRET, cookie_token=token, now=NOW
    ) is True


def test_fail_closed_when_secret_unset_even_with_valid_looking_cookie():
    # Mint a token with SOME secret, then evaluate with no server secret.
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=3600)
    assert auth._should_skip_prompt(
        session_authenticated=False, secret=None, cookie_token=token, now=NOW
    ) is False


def test_no_skip_when_cookie_absent():
    assert auth._should_skip_prompt(
        session_authenticated=False, secret=SECRET, cookie_token=None, now=NOW
    ) is False


def test_no_skip_when_cookie_expired():
    token = auth.mint_auth_token(SUBJECT, SECRET, now=NOW, ttl_seconds=10)
    assert auth._should_skip_prompt(
        session_authenticated=False, secret=SECRET, cookie_token=token, now=NOW + 11
    ) is False


# ── _cookie_ttl_seconds ──────────────────────────────────────────────

def test_cookie_ttl_default(monkeypatch):
    monkeypatch.delenv("MHDE_DASHBOARD_COOKIE_TTL_HOURS", raising=False)
    assert auth._cookie_ttl_seconds() == auth._DEFAULT_TTL_HOURS * 3600


def test_cookie_ttl_override(monkeypatch):
    monkeypatch.setenv("MHDE_DASHBOARD_COOKIE_TTL_HOURS", "24")
    assert auth._cookie_ttl_seconds() == 24 * 3600


def test_cookie_ttl_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MHDE_DASHBOARD_COOKIE_TTL_HOURS", "not-a-number")
    assert auth._cookie_ttl_seconds() == auth._DEFAULT_TTL_HOURS * 3600
