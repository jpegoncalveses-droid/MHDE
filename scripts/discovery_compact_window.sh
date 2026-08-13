#!/bin/bash
# Discovery-pass protected window (PR #89: fix/discovery-scheduled-run-protection).
#
# WHY: the full discovery pass holds ~11.5G for ~4h on a 22G zero-swap host whose hourly
# compactors (firehose ~7min, okx ~2min, brain ~14min — ~23min/h of 1-2G bursts) share the
# box. Every uncontained pass failure was a HOST-level OOM (unit under its own cap); the
# first scheduled run died 4 minutes after brain-compact fired
# (data/processed/stage1_breadth_cap_measurement.md §9-13). Pausing the three timers for
# the pass removes the known hourly spikes from the exposure window. HONESTY: this lowers
# collision probability; it does not remove the unidentified ambient burst class (gate-6
# died under a partial pause).
#
# pause : stop the three compactor TIMERS (never an in-flight compact SERVICE — a running
#         merge finishes; replace-then-delete is reader-safe) and arm a transient failsafe.
# resume: restart the timers; cancel the failsafe ONLY once the restart succeeded.
#
# THE FAILSAFE covers exactly one case: ExecStopPost failed or was skipped while the
# discovery service is no longer running. Its command re-checks — if the pass is somehow
# STILL active at fire time it deliberately no-ops (restarting compactors mid-pass would
# recreate the collision this script exists to prevent; Persistent=true makes every
# paused timer fire its catch-up IMMEDIATELY on start). It does NOT survive a user-manager
# death — a transient unit dies with its manager — but that case self-heals differently:
# the three timers are `enabled`, so a fresh manager pulls them in via timers.target.
#
# CATCH-UP CONCURRENCY: on resume all three timers fire their Persistent catch-up at
# once, defeating the :06/:26/:36 stagger for that one cycle. Memory-safe by construction
# (resume runs after the pass EXITS, its ~11.5G already released; compactor caps total
# ~5G beside ~7G co-tenants); the shared-disk IO overlap for that single catch-up cycle
# is accepted and noted here — compact services are IO-idle-scheduled.
#
# SYSTEMCTL / SYSTEMD_RUN are injectable for tests.
set -u
ACTION="${1:?usage: discovery_compact_window.sh pause|resume}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SYSTEMD_RUN="${SYSTEMD_RUN:-systemd-run}"
FAILSAFE="mhde-discover-compact-failsafe"
TIMERS=(mhde-capture-firehose-compact.timer
        mhde-capture-okx-firehose-compact.timer
        mhde-brain-compact.timer)

clear_failsafe() {
    # A stale transient unit (left loaded, possibly failed) makes the next systemd-run
    # with the same --unit fail "unit already exists" — clear deterministically.
    "$SYSTEMCTL" --user stop "$FAILSAFE.timer" "$FAILSAFE.service" 2>/dev/null || true
    "$SYSTEMCTL" --user reset-failed "$FAILSAFE.service" 2>/dev/null || true
}

case "$ACTION" in
  pause)
    clear_failsafe
    "$SYSTEMCTL" --user stop "${TIMERS[@]}" \
      || echo "discovery_compact_window: PAUSE FAILED — compactor timers still live" >&2
    "$SYSTEMD_RUN" --user --on-active=5h30m --unit="$FAILSAFE" --collect \
      /bin/bash -c "systemctl --user is-active mhde-brain-discover.service >/dev/null 2>&1 || systemctl --user start ${TIMERS[*]}" \
      || echo "discovery_compact_window: FAILSAFE ARM FAILED — restore relies on ExecStopPost alone" >&2
    ;;
  resume)
    if "$SYSTEMCTL" --user start "${TIMERS[@]}"; then
      clear_failsafe
    else
      echo "discovery_compact_window: RESUME FAILED — failsafe left armed" >&2
    fi
    ;;
  *)
    echo "usage: discovery_compact_window.sh pause|resume" >&2
    exit 2
    ;;
esac
