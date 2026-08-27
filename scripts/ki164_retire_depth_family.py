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
Filesystem-only; never opens a DB, never touches a live writer, never touches ``_gaps`` or
any surviving dataset.

    venv/bin/python scripts/ki164_retire_depth_family.py            # report only
    venv/bin/python scripts/ki164_retire_depth_family.py --apply    # delete
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto.research.capture_core import config as cc_cfg          # noqa: E402
from crypto.research.capture_core_okx import config as okx_cfg     # noqa: E402

RETIRED = cc_cfg.CAPTURE_RETIRED_DATASETS
DEFAULT_ROOTS = (cc_cfg.RAW_DIR, okx_cfg.RAW_DIR)


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
    report: dict = {ds: {"files": 0, "bytes": 0, "paths": []} for ds in RETIRED}
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
        print(f"\n  Deleted {tf:,} files ({_human(tb)}) across {len(roots)} root(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
