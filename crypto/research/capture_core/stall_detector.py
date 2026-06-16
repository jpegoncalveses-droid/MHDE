"""ADR-039 §D layer-2 — peer-asymmetry dead-shard detector.

A HUNG shard (event loop wedged / sockets silent but the process still alive) is invisible to
systemd ``Restart=`` and surfaces only as missing partitions — with symbol sharding that is a
permanent gap for ~1/N of the symbols. This detects it three ways: a systemd ``failed`` unit, a
stale/absent heartbeat, or the asymmetry tell — a shard whose traffic stops advancing while its
peers keep flowing (the dead-shard signal a single-process design never had).

``evaluate`` is PURE — heartbeats, unit states, and the previous baseline are all passed in — so
every branch is unit-tested. The CLI (``capture-stall-check``) wires the real I/O around it.
"""
from __future__ import annotations

import json
import logging
import pathlib
import subprocess

logger = logging.getLogger("mhde.crypto.capture_core.stall_detector")

NS_PER_S = 1_000_000_000


def evaluate(*, heartbeats, failed_units, prev, now_ns, expected_shards,
            interval_s, stale_factor=3):
    """Decide stall alerts. Returns ``(alerts: list[str], new_state: dict)``.

    ``heartbeats``: ``{shard_label: {ts_ns, dispatched, bytes_in, rows}}`` (current).
    ``failed_units``: systemd unit names currently ``failed``.
    ``prev``: ``{shard_label: {dispatched, rows}}`` from the previous check (asymmetry baseline).
    ``expected_shards``: shard labels that SHOULD be reporting.
    ``new_state`` is the baseline to persist for the next run.
    """
    alerts: list = []

    # 1. systemd already knows a unit exited / crash-looped to `failed`.
    for u in sorted(set(failed_units)):
        alerts.append(f"unit {u} is failed")

    # 2. an expected shard never wrote a heartbeat (never started / died before first write).
    for s in expected_shards:
        if s not in heartbeats:
            alerts.append(f"shard {s}: no heartbeat file")

    # 3. heartbeat too old -> the process is hung or gone.
    stale_ns = stale_factor * interval_s * NS_PER_S
    stale = set()
    for s, hb in heartbeats.items():
        age_ns = now_ns - int(hb["ts_ns"])
        if age_ns > stale_ns:
            stale.add(s)
            alerts.append(f"shard {s}: heartbeat stale ({age_ns / NS_PER_S:.0f}s)")

    # 4. peer-asymmetry: among FRESH shards that have a prior baseline, did ANY peer advance?
    #    If so, a fresh shard that did NOT advance is stalled. If NONE advanced it is a global
    #    lull (e.g. a market-wide reconnect), NOT a per-shard stall — do not false-alert.
    #    "Advance" = rows written (ADR-039 §D: "partitions stop advancing while others advance").
    #    Rows come from the persistent parquet writers, so the signal is MONOTONIC across a
    #    connection-manager rebuild (a universe re-resolve), unlike the mgr-scoped dispatched count.
    def _advanced(s):
        p = prev.get(s)
        return p is not None and int(heartbeats[s]["rows"]) > int(p["rows"])

    fresh = [s for s in heartbeats if s not in stale]
    if any(_advanced(s) for s in fresh):
        for s in fresh:
            if prev.get(s) is not None and not _advanced(s):
                alerts.append(f"shard {s}: stalled (no new traffic while peers flow)")

    new_state = {s: {"dispatched": int(hb["dispatched"]), "rows": int(hb.get("rows", 0))}
                 for s, hb in heartbeats.items()}
    return alerts, new_state


# -- I/O around the pure evaluator (all injectable for tests) ------------------

def read_heartbeats(heartbeat_dir) -> dict:
    """Read all ``shard-*.json`` heartbeats in the dir -> ``{shard_label: payload}``. Junk /
    half-written / missing files are skipped, never fatal (the detector runs while shards write)."""
    out: dict = {}
    d = pathlib.Path(heartbeat_dir)
    if not d.is_dir():
        return out
    for f in d.glob("shard-*.json"):
        label = f.name[len("shard-"):-len(".json")]
        try:
            out[label] = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    return out


def _systemctl_failed(unit: str) -> bool:
    try:
        rc = subprocess.run(["systemctl", "--user", "is-failed", unit],
                            capture_output=True, text=True, timeout=10)
        return rc.stdout.strip() == "failed"
    except (OSError, subprocess.SubprocessError):
        return False                              # can't tell -> don't false-alert


def _send_alert_text(text: str) -> None:
    try:
        from monitoring.alert import send_text   # DB-free Telegram path (same as the guards)
        send_text(text)
    except Exception:                             # noqa: BLE001 — alerting must never crash us
        logger.warning("stall-detector alert send failed", exc_info=True)


def _load_state(path) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path, state) -> None:
    try:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(p)
    except OSError:
        logger.warning("stall-detector state save failed", exc_info=True)


def run_check(*, heartbeat_dir, expected_shards, unit_names, interval_s, stale_factor,
             state_path, now_ns,
             read_heartbeats_fn=None, unit_failed_fn=None, send_fn=None,
             load_state_fn=None, save_state_fn=None) -> list:
    """Read heartbeats + systemd unit states, evaluate, alert (ONE combined Telegram), and
    persist the new baseline for the next run. All I/O is injectable for tests."""
    read_heartbeats_fn = read_heartbeats_fn or read_heartbeats
    unit_failed_fn = unit_failed_fn or _systemctl_failed
    send_fn = send_fn or _send_alert_text
    load_state_fn = load_state_fn or _load_state
    save_state_fn = save_state_fn or _save_state

    heartbeats = read_heartbeats_fn(heartbeat_dir)
    failed = [u for u in unit_names if unit_failed_fn(u)]
    prev = load_state_fn(state_path)
    alerts, new_state = evaluate(
        heartbeats=heartbeats, failed_units=failed, prev=prev, now_ns=now_ns,
        expected_shards=expected_shards, interval_s=interval_s, stale_factor=stale_factor)
    if alerts:
        send_fn("capture-core stall detector:\n" + "\n".join(f"- {a}" for a in alerts))
    save_state_fn(state_path, new_state)
    return alerts
