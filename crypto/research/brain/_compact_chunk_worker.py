"""Subprocess worker for chunked brain-store compaction.

Reads partition paths from stdin (one per line), compacts them until the merge budget via
:func:`compaction._compact_chunk`, and writes the chunk summary as a single JSON line to
stdout. Run as::

    python -m crypto.research.brain._compact_chunk_worker <root> <budget> <registry_path>

(an empty ``<registry_path>`` means no registry parity oracle for this run.)

It exists so each compaction chunk runs in its OWN process: process exit returns the pyarrow
memory pool to the OS, bounding peak RSS by run size (in-process release does not reliably do
this — the PR #60 finding). The chunk summary — counts AND every mismatch/skip — is marshalled
back as JSON; a finding not written here is dropped by the process exit (the PR #60 lesson).
Filesystem + read-only registry only; never opens the production DB.
"""
from __future__ import annotations

import json
import sys

from crypto.research.brain import compaction


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    root, budget = argv[0], int(argv[1])
    registry_path = argv[2] if len(argv) > 2 and argv[2] else None
    paths = [ln for ln in sys.stdin.read().splitlines() if ln]
    res = compaction._compact_chunk(root, paths, budget, registry_path)
    sys.stdout.write(json.dumps(res))


if __name__ == "__main__":
    main()
