#!/usr/bin/env python3
"""Read-only system-state snapshot for STATE.md.

Collects machine facts about the repo, systemd timers, the DuckDB
production database, and the file-based engine-export contract, then
prints them to stdout as one delimited key:value block grouped by
section.

This script is strictly read-only: it issues no writes to the DB
(``read_only=True``), the repo, or systemd. It reports facts only — it
does not synthesise blockers, next actions, or divergence text. Those
judgement fields in STATE.md are filled by the operator / session.

Run:
    venv/bin/python scripts/snapshot_state.py
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "data" / "mhde.duckdb"

# Schema modules whose CREATE TABLE statements define the *documented*
# table set. Tables in the live DB but absent here are orphans.
SCHEMA_FILES = (
    REPO / "crypto" / "schema.py",
    REPO / "ml" / "schema.py",
    REPO / "fx" / "schema.py",
)


# --- output helpers ----------------------------------------------------------

def section(name: str) -> None:
    print(f"\n[{name}]")


def kv(key: str, value: object) -> None:
    print(f"{key}: {value}")


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a command, returning (returncode, combined stdout+stderr text)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=REPO, timeout=30
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 1, f"<command failed: {exc}>"
    out = proc.stdout.rstrip("\n")
    if proc.returncode != 0 and proc.stderr.strip():
        out = (out + "\n" + proc.stderr.rstrip("\n")).strip()
    return proc.returncode, out


# --- git ---------------------------------------------------------------------

def snapshot_git() -> None:
    section("git")

    _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    kv("current_branch", branch or "<unknown>")

    _, sha = _run(["git", "rev-parse", "--short", "HEAD"])
    _, porcelain = _run(["git", "status", "--porcelain"])
    dirty = bool(porcelain.strip())
    kv("head_short_sha", sha or "<unknown>")
    kv("dirty", "yes" if dirty else "no")
    kv("dirty_file_count", len(porcelain.splitlines()) if dirty else 0)

    rc, ab = _run(["git", "rev-list", "--left-right", "--count", "master...HEAD"])
    if rc == 0 and ab.strip():
        parts = ab.split()
        if len(parts) == 2:
            kv("behind_master", parts[0])
            kv("ahead_of_master", parts[1])
        else:
            kv("ahead_behind_master", ab.strip())
    else:
        kv("ahead_behind_master", f"<unavailable: {ab}>")

    _, stashes = _run(["git", "stash", "list"])
    stash_lines = [s for s in stashes.splitlines() if s.strip()]
    kv("stash_count", len(stash_lines))
    for i, line in enumerate(stash_lines):
        kv(f"stash[{i}]", line)

    _, branches = _run([
        "git", "branch", "-a", "--sort=-committerdate",
        "--format=%(refname:short)",
    ])
    recent = [b for b in branches.splitlines() if b.strip()][:8]
    kv("recent_branches", ", ".join(recent) if recent else "<none>")


# --- systemd -----------------------------------------------------------------

def _list_timers(user: bool) -> None:
    scope = "user" if user else "system"
    base = ["systemctl"] + (["--user"] if user else [])
    rc, out = _run(base + ["list-timers", "mhde-*", "--all", "--no-pager"])
    if rc != 0:
        kv(f"{scope}_timers", f"<unavailable: {out.splitlines()[0] if out else 'error'}>")
        return
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Drop the trailing summary line ("N timers listed.") for cleanliness.
    data_lines = [ln for ln in lines if "timers listed" not in ln]
    if len(data_lines) <= 1:
        kv(f"{scope}_timers", "<none>")
        return
    kv(f"{scope}_timers", f"{len(data_lines) - 1} timer(s)")
    for ln in data_lines:
        print(f"  {ln}")


def snapshot_systemd() -> None:
    section("systemd")
    _list_timers(user=False)
    _list_timers(user=True)


# --- duckdb ------------------------------------------------------------------

def _declared_tables() -> set[str]:
    declared: set[str] = set()
    for path in SCHEMA_FILES:
        try:
            text = path.read_text()
        except OSError:
            continue
        declared.update(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text))
    return declared


def snapshot_duckdb() -> None:
    section("duckdb")
    kv("db_path", str(DB_PATH))

    if not DB_PATH.exists():
        kv("db_status", f"<missing: {DB_PATH}>")
        return

    try:
        import duckdb
    except ImportError:
        kv("db_status", "<duckdb module not importable>")
        return

    # DuckDB permits one read-write OR many read-only connections. If a
    # pipeline holds the write lock, the read-only connect raises. Catch
    # it, report, and continue — never crash.
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
    except Exception as exc:  # noqa: BLE001 - DuckDB lock/IO errors vary
        kv("db_status", "DB locked, universe/model/table fields unavailable")
        kv("db_error", str(exc).splitlines()[0])
        return

    kv("db_status", "open (read_only)")
    try:
        # crypto_universe: row count + most recent add date (added_date).
        try:
            n = conn.execute("SELECT COUNT(*) FROM crypto_universe").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM crypto_universe WHERE is_active = TRUE"
            ).fetchone()[0]
            last_add = conn.execute(
                "SELECT MAX(added_date) FROM crypto_universe"
            ).fetchone()[0]
            last_remove = conn.execute(
                "SELECT MAX(removed_date) FROM crypto_universe"
            ).fetchone()[0]
            kv("crypto_universe_rows", n)
            kv("crypto_universe_active", active)
            kv("crypto_universe_last_added_date", last_add)
            kv("crypto_universe_last_removed_date", last_remove)
        except Exception as exc:  # noqa: BLE001
            kv("crypto_universe", f"<query failed: {str(exc).splitlines()[0]}>")

        # Active crypto model run.
        try:
            rows = conn.execute(
                "SELECT model_id FROM crypto_ml_model_runs WHERE is_active = true"
            ).fetchall()
            ids = [r[0] for r in rows]
            kv("active_crypto_model_runs", ", ".join(ids) if ids else "<none>")
        except Exception as exc:  # noqa: BLE001
            kv("active_crypto_model_runs", f"<query failed: {str(exc).splitlines()[0]}>")

        # Orphan tables: live tables not declared in the schema modules.
        try:
            live = {
                r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'"
                ).fetchall()
            }
            declared = _declared_tables()
            orphans = sorted(live - declared)
            missing = sorted(declared - live)
            kv("live_table_count", len(live))
            kv("declared_table_count", len(declared))
            kv("orphan_tables", ", ".join(orphans) if orphans else "<none>")
            kv("declared_but_absent", ", ".join(missing) if missing else "<none>")
        except Exception as exc:  # noqa: BLE001
            kv("orphan_tables", f"<query failed: {str(exc).splitlines()[0]}>")
    finally:
        conn.close()


# --- files -------------------------------------------------------------------

def snapshot_files() -> None:
    section("files")

    spec_path = REPO / "data" / "exports" / "active_spec.json"
    kv("active_spec_path", str(spec_path))
    if not spec_path.exists():
        kv("active_spec", "<missing>")
    else:
        try:
            spec = json.loads(spec_path.read_text())
            kv("universe.source", spec.get("universe", {}).get("source", "<absent>"))
            kv("universe.excluded", spec.get("universe", {}).get("excluded", "<absent>"))
            kv("spec_hash", spec.get("spec_hash", "<absent>"))
            kv("spec_version", spec.get("spec_version", "<absent>"))
            kv("generated_by_mhde_commit", spec.get("generated_by_mhde_commit", "<absent>"))
            # Strategy parameter block: the selected strategy plus the
            # sizing / risk / runtime parameters that define how it trades.
            for block in ("phase_1b_winner", "sizing", "risk", "runtime"):
                params = spec.get(block)
                if isinstance(params, dict):
                    for k in sorted(params):
                        kv(f"{block}.{k}", params[k])
                else:
                    kv(block, "<absent>")
        except (json.JSONDecodeError, OSError) as exc:
            kv("active_spec", f"<unreadable: {exc}>")

    # Latest predictions export by filename date (predictions_YYYY-MM-DD.json).
    exports = REPO / "data" / "exports"
    dated = sorted(
        p for p in exports.glob("predictions_*.json")
        if re.fullmatch(r"predictions_\d{4}-\d{2}-\d{2}\.json", p.name)
    )
    if dated:
        latest = dated[-1]
        kv("latest_predictions_file", latest.name)
        kv("latest_predictions_dated_count", len(dated))
    else:
        kv("latest_predictions_file", "<none>")


# --- main --------------------------------------------------------------------

def main() -> int:
    print("=== SNAPSHOT_STATE ===")
    snapshot_git()
    snapshot_systemd()
    snapshot_duckdb()
    snapshot_files()
    print("\n=== END SNAPSHOT_STATE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
