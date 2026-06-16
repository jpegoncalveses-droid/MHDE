"""Raw systemd sd_notify (``$NOTIFY_SOCKET``) — no ``sdnotify`` pip dependency.

Capture-core emits ``READY=1`` (process is up) and ``WATCHDOG=1`` (liveness heartbeat) to
``$NOTIFY_SOCKET`` so the ADR-039 sharded units can be ``Type=notify`` with ``WatchdogSec``.
Every entry point is a NO-OP when ``NOTIFY_SOCKET`` is unset (manual CLI runs / tests) and
never raises — a notify failure must never take down capture.
"""
from __future__ import annotations

import os
import socket
from typing import Optional


class SystemdNotifier:
    """Send systemd service-notification datagrams to ``$NOTIFY_SOCKET``.

    Disabled (every call a no-op) when ``addr`` is falsy. The datagram socket is created
    lazily and reused; ``OSError`` is swallowed so a missing / re-bound notify socket can
    never crash the capture loop.
    """

    def __init__(self, addr: Optional[str]) -> None:
        self._addr = addr
        self._sock: Optional[socket.socket] = None

    @property
    def enabled(self) -> bool:
        return bool(self._addr)

    def _addr_for_sendto(self) -> Optional[str]:
        if not self._addr:
            return None
        # Abstract namespace: systemd encodes it with a leading '@' that maps to a NUL.
        return ("\0" + self._addr[1:]) if self._addr[0] == "@" else self._addr

    def _send(self, state: bytes) -> None:
        addr = self._addr_for_sendto()
        if addr is None:
            return
        try:
            if self._sock is None:
                self._sock = socket.socket(
                    socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
            self._sock.sendto(state, addr)
        except OSError:
            pass                              # never let a notify failure kill capture

    def ready(self) -> None:
        self._send(b"READY=1\n")

    def watchdog(self) -> None:
        self._send(b"WATCHDOG=1\n")


def notifier_from_env(env: Optional[dict] = None) -> SystemdNotifier:
    """Build a notifier from ``NOTIFY_SOCKET`` (disabled/no-op when unset)."""
    env = os.environ if env is None else env
    return SystemdNotifier(env.get("NOTIFY_SOCKET"))


def watchdog_interval_s(env: Optional[dict] = None) -> Optional[float]:
    """Ping cadence = half the systemd ``WATCHDOG_USEC`` deadline; None if unset/invalid."""
    env = os.environ if env is None else env
    usec = env.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return (int(usec) / 1_000_000.0) / 2.0
    except ValueError:
        return None
