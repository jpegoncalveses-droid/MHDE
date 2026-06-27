"""run_pass-level guard for the scoped-construction discipline (component 2).

Component 1 made the reader construct ``ds.dataset()`` over the batch's partition files
instead of the whole ``symbol=*/date=*`` tree. This pins the consequence at the runner
level — the property the operator asked to guarantee ("build it once per batch per pass,
not 22x"): a full multi-batch ``run_pass`` constructs the dataset exactly ONCE PER BATCH,
each over a SCOPED file list, NEVER the whole-tree base string. The old pattern — one
whole-tree construction per 25-symbol batch (22x/pass on the live universe) — is gone.

This is RED on the pre-fix reader (which passed ``str(base)`` per batch) and GREEN on the
scoped reader.
"""
from __future__ import annotations

from crypto.research.capture_core import store as capture_store
from crypto.research.brain import config as cfg
from crypto.research.brain import pipeline, sources, reader

_T0_MS = 1_781_640_000_000               # 2026-06-16 20:00:00 UTC, a 60s boundary
SYMS = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT"]


def _agg_row(symbol, *, recv_ns, T_ms):
    return {"recv_ts_ns": recv_ns, "e": "aggTrade", "E": T_ms, "a": 1, "s": symbol,
            "p": "100", "q": "2", "f": 1, "l": 1, "T": T_ms, "m": False}


def _write_capture(root, rows):
    w = capture_store.aggtrade_writer(str(root))
    for r in rows:
        w.append(r)
    w.flush_all()


def test_run_pass_constructs_once_per_batch_each_scoped(tmp_path, monkeypatch):
    r0 = _T0_MS * 1_000_000
    rows = [_agg_row(s, recv_ns=r0 + 10 + i, T_ms=_T0_MS + 1_000 + i) for i, s in enumerate(SYMS)]
    _write_capture(tmp_path / "capture", rows)
    now = (_T0_MS + 60_000) * 1_000_000 + cfg.BRAIN_WATERMARK_NS   # window-0 settled

    calls = []
    orig = reader.ds.dataset

    def spy(source, *a, **k):
        calls.append(source)
        return orig(source, *a, **k)

    monkeypatch.setattr(reader.ds, "dataset", spy)

    pipeline.run_pass(
        sources.TRADES, capture_root=str(tmp_path / "capture"),
        store_root=str(tmp_path / "brain"),
        registry_path=str(tmp_path / "brain" / "registry.sqlite"),
        now_ns=now, symbols=SYMS, batch_size=2)

    # 6 symbols / batch_size 2 = 3 batches -> exactly 3 constructions, one per batch.
    assert len(calls) == 3, f"construct once per batch (3), got {len(calls)}"
    # every construction is a SCOPED file list, never the whole-tree base string.
    assert all(isinstance(s, list) for s in calls), \
        f"every construction must be a scoped file list, not whole-tree str: " \
        f"{[type(s).__name__ for s in calls]}"
    # and each scoped list spans only its own batch's symbols (<=2), never the whole universe.
    for src in calls:
        syms_in = {p.split("symbol=")[1].split("/")[0] for p in src}
        assert 1 <= len(syms_in) <= 2, f"a construction spanned more than one batch: {syms_in}"
    # the three batches together cover all six symbols, disjointly.
    covered = sorted(p.split("symbol=")[1].split("/")[0] for src in calls for p in src)
    assert covered == sorted(SYMS)
