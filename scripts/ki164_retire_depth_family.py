#!/usr/bin/env python3
"""KI-164 one-off: sweep the RETIRED depth family off disk, on every capture root.

The writers are retired by config kill-switch (``DEPTH_ENABLED`` /
``DEPTH_SNAPSHOT_ENABLED`` / ``DEPTH_STATE_ENABLED`` all False), which stops NEW files.
This removes what is already there:

  * ``depth``           — the raw 100 ms diff tape
  * ``depth_snapshot``  — the REST seeding/resync snapshots
  * ``depth_state``     — whatever regrew since the 2026-08-26 21:20 emergency deletion
                          (~870k files/day while its writer was still live)

DRY-RUN BY DEFAULT: prints per-source file count and bytes and touches nothing. Pass
``--apply`` to delete. Idempotent — a second ``--apply`` finds nothing and reports zeros.
Filesystem-only; never opens a DB, never touches ``_gaps`` or any surviving dataset.

ORDER MATTERS — run this LAST:
  1. update the working tree (the kill-switches live in config)
  2. ``systemctl --user restart mhde-capture.target``  <- the RUNNING shards hold the OLD
     code until this; deleting first just lets a live writer recreate the tree
  3. ``--apply`` this script
Until step 2 lands, the retired datasets still carry the tightest nightly ceiling
(``CAPTURE_RETIRED_RETENTION_DAYS``), so they cannot grow unbounded in the meantime.

    venv/bin/python scripts/ki164_retire_depth_family.py            # report only
    venv/bin/python scripts/ki164_retire_depth_family.py --apply    # delete
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from crypto.research.capture_core import config as cc_cfg          # noqa: E402
from crypto.research.capture_core_okx import config as okx_cfg     # noqa: E402

RETIRED = cc_cfg.CAPTURE_RETIRED_DATASETS
#: Anchored to the REPO ROOT: the config values are relative, so a run from any other cwd
#: would silently report "absent / TOTAL 0" and read as "nothing to sweep".
DEFAULT_ROOTS = (os.path.join(_REPO_ROOT, cc_cfg.RAW_DIR),
                 os.path.join(_REPO_ROOT, okx_cfg.RAW_DIR))


def _measure(path: str) -> tuple[int, int]:
    """(files, bytes) under ``path`` — one walk, no stat storm on missing dirs."""
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
                files += 1
            except OSError:                      # vanished under a live writer: ignore
                continue
    return files, total


def sweep(roots, *, apply: bool = False) -> dict:
    """Measure (and with ``apply``, delete) the retired datasets across ``roots``.

    Returns ``{dataset: {"files": n, "bytes": n, "paths": [...]}}`` aggregated over roots.
    """
    report: dict = {ds: {"files": 0, "bytes": 0, "paths": [], "remaining": 0}
                   for ds in RETIRED}
    for root in roots:
        for ds in RETIRED:
            path = os.path.join(root, ds)
            if not os.path.isdir(path):
                continue
            files, nbytes = _measure(path)
            report[ds]["files"] += files
            report[ds]["bytes"] += nbytes
            report[ds]["paths"].append(path)
            if apply:
                shutil.rmtree(path, ignore_errors=True)
                # Re-measure: rmtree(ignore_errors=True) swallows failures (e.g. a symlinked
                # dataset dir), so reporting the PRE-count as "deleted" could be a lie.
                left_files, left_bytes = _measure(path) if os.path.isdir(path) else (0, 0)
                # max(0, ...) — if the ORDER was violated and a live writer recreated files
                # mid-sweep, `left` can exceed the pre-count; never print a negative "deleted".
                report[ds]["files"] = max(0, report[ds]["files"] - left_files)
                report[ds]["bytes"] = max(0, report[ds]["bytes"] - left_bytes)
                report[ds]["remaining"] = report[ds].get("remaining", 0) + left_files
    return report


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if x < 1024 or unit == "T":
            return f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}T"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Default is a dry run that touches nothing.")
    ap.add_argument("--root", action="append", default=None,
                    help="Capture root (repeatable). Default: both Binance and OKX roots.")
    args = ap.parse_args(argv)

    roots = args.root or list(DEFAULT_ROOTS)
    mode = "APPLY (deleting)" if args.apply else "DRY RUN (nothing will be deleted)"
    print(f"KI-164 retire depth family — {mode}")
    for r in roots:
        print(f"  root: {r}{'' if os.path.isdir(r) else '   [absent]'}")

    report = sweep(roots, apply=args.apply)
    tf = tb = 0
    print(f"\n  {'dataset':<16}{'files':>12}{'bytes':>12}")
    for ds in RETIRED:
        e = report[ds]
        tf += e["files"]
        tb += e["bytes"]
        print(f"  {ds:<16}{e['files']:>12,}{_human(e['bytes']):>12}")
    print(f"  {'TOTAL':<16}{tf:>12,}{_human(tb):>12}")

    if not args.apply:
        print("\n  Dry run — nothing deleted. Re-run with --apply to reclaim.")
    else:
        left = sum(report[ds].get("remaining", 0) for ds in RETIRED)
        print(f"\n  Deleted {tf:,} files ({_human(tb)}) across {len(roots)} root(s).")
        if left:
            print(f"  WARNING: {left:,} file(s) could NOT be removed — re-run, or check for "
                  f"a symlinked dataset dir / permissions. If the writers are still live the "
                  f"ORDER was violated: restart mhde-capture.target first.")
            return 1                      # non-zero so a wrapper can detect a partial sweep
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
