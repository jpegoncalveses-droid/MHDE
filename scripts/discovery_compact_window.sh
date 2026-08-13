#!/bin/bash
# Discovery-pass protected window (PR: fix/discovery-scheduled-run-protection).
#
# WHY: the full discovery pass holds ~11.5G for ~4h on a 22G zero-swap host whose hourly
# compactors (firehose ~7min, okx ~2min, brain ~14min — ~23min/h of 1-2G bursts) share the
# box. Every uncontained pass failure was a HOST-level OOM (unit under its own cap); the
# first scheduled run died 4 minutes after brain-compact fired
# (data/processed/stage1_breadth_cap_measurement.md §9-13). Pausing the three timers for
# the pass removes the known hourly spikes from the exposure window. HONESTY: this lowers
# collision probability; it does not remove the unidentified ambient burst class (gate-6
# died under a partial pause). Compaction backlog is catch-up-able by design: each
# compactor processes ALL pending closed hours on its next fire (measured single-hour
# runs: 7/2/14 min; a ~4h backlog fits the ~1.8h inter-pass window unless per-hour cost
# is fully linear — measure catch-up runtimes after deploy and revisit the pause set if
# the backlog grows cycle-over-cycle).
#
# pause : stop the three compactor TIMERS (never an in-flight compact SERVICE — a running
#         merge finishes; replace-then-delete is reader-safe) and arm a transient failsafe
#         that restarts them in 5h30m even if the user manager or this service dies.
# resume: restart the timers, cancel the failsafe. Wired as ExecStartPre/ExecStopPost of
#         mhde-brain-discover.service — ExecStopPost runs on EVERY exit, oom-kill included.
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

case "$ACTION" in
  pause)
    "$SYSTEMCTL" --user stop "$FAILSAFE.timer" 2>/dev/null || true    # clear a stale failsafe
    "$SYSTEMCTL" --user stop "${TIMERS[@]}" || true
    "$SYSTEMD_RUN" --user --on-active=5h30m --unit="$FAILSAFE" --collect \
      systemctl --user start "${TIMERS[@]}" || true
    ;;
  resume)
    "$SYSTEMCTL" --user start "${TIMERS[@]}" || true
    "$SYSTEMCTL" --user stop "$FAILSAFE.timer" 2>/dev/null || true
    ;;
  *)
    echo "usage: discovery_compact_window.sh pause|resume" >&2
    exit 2
    ;;
esac
