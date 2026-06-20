"""Subprocess worker for chunked WHOLE-PARTITION compaction (the as-of seal-yesterday pass).

Reads partition paths from stdin (one per line), whole-partition compacts them until the
merge budget via :func:`maintenance._migrate_chunk`, and writes the chunk summary as a
single JSON line to stdout. Run as::

    python -m crypto.research.capture_core._migrate_chunk_worker <root> <budget> <now_ms>

It exists so each whole-partition compaction chunk runs in its OWN process: process exit
returns the pyarrow memory pool to the OS, bounding peak RSS by run size (in-process
release does not reliably do this). Filesystem-only; never opens the production DB.
"""
from __future__ import annotations

import json
import sys

from crypto.research.capture_core import maintenance


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    root, budget, now_ms = argv[0], int(argv[1]), int(argv[2])
    paths = [ln for ln in sys.stdin.read().splitlines() if ln]
    res = maintenance._migrate_chunk(root, paths, budget, now_ms)
    sys.stdout.write(json.dumps(res))


if __name__ == "__main__":
    main()
