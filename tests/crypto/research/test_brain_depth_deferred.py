"""DEPTH is deferred out of the runner SOURCES (component 3).

``depth_state`` is an expire-only 2-day rolling buffer, deliberately excluded from the
capture compactor (``FIREHOSE_PRUNABLE_DATASETS``). With ~3M uncompacted fragments its
read OOMs at ``ds.dataset()`` construction — a separate, harder wall than the un-date-pruned
klines footer scan the scoped-construction fix (components 1-2) addresses. Until depth_state
has its own fragmentation plan (a depth-aware compactor over sealed-only partitions, or a
latest-snapshot read), DEPTH stays OUT of the continuous runner's source set. The DEPTH
SourceSpec + primitive remain defined so it can rejoin once that lands. Tracked as KI-159.
"""
from __future__ import annotations

from crypto.research.brain import sources, runner


def test_depth_not_in_runner_sources():
    assert "depth" not in sources.SOURCES
    assert len(sources.SOURCES) == 12


def test_depth_spec_preserved_for_rejoin():
    # The spec + reader/primitive stay defined so depth can rejoin once depth_state has a
    # fragmentation plan; it is simply not wired into the runner's source set yet.
    assert sources.DEPTH.dataset == "depth"
    assert sources.DEPTH.capture_dataset == "depth_state"


def test_runner_default_sources_excludes_depth():
    specs = runner.sources_module_values()
    assert len(specs) == 12
    assert all(s.dataset != "depth" for s in specs)
