"""Unit tests for monitoring.pipeline_monitor.core."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from monitoring.pipeline_monitor.core import (
    PipelineResult,
    Status,
    StepResult,
    STATUS_EMOJI,
    evaluate_steps,
    render_telegram_message,
)


NOW = datetime(2026, 5, 12, 6, 40, 0, tzinfo=timezone.utc)


# ── emoji mapping ─────────────────────────────────────────────────────
def test_emoji_mapping():
    assert STATUS_EMOJI[Status.GREEN] == "🟢"
    assert STATUS_EMOJI[Status.RED] == "🔴"
    assert STATUS_EMOJI[Status.SKIPPED] == "⚪"


def test_step_result_emoji_property():
    assert StepResult("x", Status.GREEN).emoji == "🟢"
    assert StepResult("x", Status.RED, "boom").emoji == "🔴"
    assert StepResult("x", Status.SKIPPED).emoji == "⚪"


# ── overall status ────────────────────────────────────────────────────
def test_overall_green_when_all_green():
    pr = PipelineResult("Crypto", NOW, [
        StepResult("a", Status.GREEN), StepResult("b", Status.GREEN),
    ])
    assert pr.overall is Status.GREEN
    assert pr.has_red is False


def test_overall_red_when_any_red():
    pr = PipelineResult("Crypto", NOW, [
        StepResult("a", Status.GREEN), StepResult("b", Status.RED, "x"),
        StepResult("c", Status.SKIPPED),
    ])
    assert pr.overall is Status.RED
    assert pr.has_red is True


def test_overall_green_when_only_skipped_and_green():
    # SKIPPED alone never makes the pipeline red.
    pr = PipelineResult("FX", NOW, [
        StepResult("a", Status.GREEN), StepResult("b", Status.SKIPPED),
    ])
    assert pr.overall is Status.GREEN
    assert pr.has_red is False


# ── render ────────────────────────────────────────────────────────────
def test_render_header_green():
    pr = PipelineResult("Crypto", NOW, [StepResult("OHLCV ingestion", Status.GREEN)])
    msg = render_telegram_message(pr)
    lines = msg.splitlines()
    assert lines[0] == "🟢 Crypto Pipeline 2026-05-12 06:40 UTC"
    assert lines[1] == "🟢 OHLCV ingestion"


def test_render_header_red_when_any_step_red():
    pr = PipelineResult("Crypto", NOW, [
        StepResult("OHLCV ingestion", Status.GREEN),
        StepResult("Feature pipeline", Status.RED, "no rows for 2026-05-11"),
        StepResult("Model predictions", Status.SKIPPED, "skipped — earlier step failed"),
    ])
    msg = render_telegram_message(pr)
    lines = msg.splitlines()
    assert lines[0].startswith("🔴 Crypto Pipeline 2026-05-12 06:40 UTC")
    assert lines[1] == "🟢 OHLCV ingestion"
    assert lines[2] == "🔴 Feature pipeline — no rows for 2026-05-11"
    assert lines[3] == "⚪ Model predictions — skipped — earlier step failed"


def test_render_includes_detail_only_when_present():
    pr = PipelineResult("FX", NOW, [
        StepResult("Bar ingestion", Status.GREEN),
        StepResult("Signal generation", Status.GREEN, "BUY_GBP @ 2026-05-12 06:00"),
    ])
    lines = render_telegram_message(pr).splitlines()
    assert lines[1] == "🟢 Bar ingestion"
    assert lines[2] == "🟢 Signal generation — BUY_GBP @ 2026-05-12 06:00"


# ── evaluate_steps: cascade ───────────────────────────────────────────
def test_evaluate_steps_all_green():
    steps = [
        ("step a", lambda: StepResult("step a", Status.GREEN, "ok")),
        ("step b", lambda: StepResult("step b", Status.GREEN, "ok")),
    ]
    res = evaluate_steps(steps)
    assert [s.status for s in res] == [Status.GREEN, Status.GREEN]


def test_evaluate_steps_cascades_skip_after_red():
    steps = [
        ("step a", lambda: StepResult("step a", Status.GREEN)),
        ("step b", lambda: StepResult("step b", Status.RED, "broken")),
        ("step c", lambda: StepResult("step c", Status.GREEN)),  # never called
        ("step d", lambda: StepResult("step d", Status.GREEN)),
    ]
    res = evaluate_steps(steps)
    assert [s.status for s in res] == [Status.GREEN, Status.RED, Status.SKIPPED, Status.SKIPPED]
    assert res[1].detail == "broken"
    assert "skip" in res[2].detail.lower()


def test_evaluate_steps_no_cascade_when_disabled():
    steps = [
        ("step a", lambda: StepResult("step a", Status.RED, "x")),
        ("step b", lambda: StepResult("step b", Status.GREEN)),
        ("step c", lambda: StepResult("step c", Status.RED, "y")),
    ]
    res = evaluate_steps(steps, stop_on_red=False)
    assert [s.status for s in res] == [Status.RED, Status.GREEN, Status.RED]


def test_evaluate_steps_exception_becomes_red():
    def boom() -> StepResult:
        raise RuntimeError("kaboom")

    steps = [("step a", boom), ("step b", lambda: StepResult("step b", Status.GREEN))]
    res = evaluate_steps(steps)
    assert res[0].status is Status.RED
    assert "kaboom" in res[0].detail
    # step b cascades to skipped
    assert res[1].status is Status.SKIPPED


def test_evaluate_steps_uses_registered_name_authoritatively():
    # If a check returns a StepResult with a mismatched name, the runner's
    # registered name wins (keeps the rendered list stable).
    steps = [("Canonical name", lambda: StepResult("wrong name", Status.GREEN, "hi"))]
    res = evaluate_steps(steps)
    assert res[0].name == "Canonical name"
    assert res[0].detail == "hi"
