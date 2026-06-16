"""ADR-039 gap 3 — raw sd_notify (NOTIFY_SOCKET datagram) helper.

No sdnotify pip dependency: capture emits READY=1 / WATCHDOG=1 straight to
``$NOTIFY_SOCKET`` so the sharded units can be ``Type=notify`` with ``WatchdogSec``. It
MUST be a no-op when ``NOTIFY_SOCKET`` is unset (manual CLI / tests) and never raise.
"""
from __future__ import annotations

import socket

from crypto.research.capture_core import sd_notify


def test_notifier_from_env_unset_is_disabled():
    n = sd_notify.notifier_from_env(env={})            # NOTIFY_SOCKET absent
    assert n.enabled is False
    n.ready()
    n.watchdog()                                       # no-op, must NOT raise


def test_notifier_sends_ready_and_watchdog_to_real_socket(tmp_path):
    sock_path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2.0)
    try:
        n = sd_notify.notifier_from_env(env={"NOTIFY_SOCKET": sock_path})
        assert n.enabled is True
        n.ready()
        assert srv.recv(64) == b"READY=1\n"
        n.watchdog()
        assert srv.recv(64) == b"WATCHDOG=1\n"
    finally:
        srv.close()


def test_abstract_namespace_at_prefix_maps_to_nul():
    # systemd encodes an abstract-namespace socket with a leading '@' -> NUL byte.
    n = sd_notify.SystemdNotifier("@/org/test/x")
    assert n._addr_for_sendto() == "\0/org/test/x"


def test_send_swallows_oserror(tmp_path):
    # No listener at the path -> sendto raises OSError -> must be swallowed.
    n = sd_notify.notifier_from_env(env={"NOTIFY_SOCKET": str(tmp_path / "nobody.sock")})
    n.ready()                                          # MUST NOT raise


def test_watchdog_interval_s_is_half_the_usec_deadline():
    assert sd_notify.watchdog_interval_s(env={"WATCHDOG_USEC": "30000000"}) == 15.0
    assert sd_notify.watchdog_interval_s(env={}) is None
    assert sd_notify.watchdog_interval_s(env={"WATCHDOG_USEC": "x"}) is None
