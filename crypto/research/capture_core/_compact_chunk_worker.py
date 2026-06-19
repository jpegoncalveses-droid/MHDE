"""Subprocess worker for chunked firehose compaction.

Reads partition paths from stdin (one per line), compacts their closed hours until the
merge budget via :func:`maintenance._compact_chunk`, and writes the chunk summary as a
single JSON line to stdout. Run as::

    python -m crypto.research.capture_core._compact_chunk_worker <root> <budget> <now_ts> <grace_s>

It exists so each compaction chunk runs in its OWN process: process exit returns the
pyarrow memory pool to the OS, bounding peak RSS by run size (in-process release does not
reliably do this). Filesystem-only; never opens the production DB.
"""
from __future__ import annotations

import json
import sys

from crypto.research.capture_core import maintenance


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    root, budget, now_ts, grace_s = argv[0], int(argv[1]), float(argv[2]), float(argv[3])
    paths = [ln for ln in sys.stdin.read().splitlines() if ln]
    res = maintenance._compact_chunk(root, paths, budget, now_ts, grace_s)
    sys.stdout.write(json.dumps(res))


if __name__ == "__main__":
    main()
