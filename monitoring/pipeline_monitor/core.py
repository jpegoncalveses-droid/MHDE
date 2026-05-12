"""Core types for the pipeline monitor.

A *pipeline run report* is a :class:`PipelineResult` — a pipeline name, a
UTC timestamp, and an ordered list of :class:`StepResult` (one per pipeline
step). Each step is :data:`Status.GREEN`, :data:`Status.RED`, or
:data:`Status.SKIPPED` (rendered 🟢 / 🔴 / ⚪).

:func:`evaluate_steps` runs a list of ``(name, callable)`` checks in order.
With ``stop_on_red=True`` (the default, used by the daily per-pipeline
runner) the first RED short-circuits the rest: every later step is reported
SKIPPED without being run, because the production pipelines are strictly
sequential ("each step's output is the next step's input" — ARCHITECTURE.md).
With ``stop_on_red=False`` (used by the continuous monitor, whose checks are
independent) every check runs.

:func:`render_telegram_message` produces the plain-text Telegram body:

    🟢/🔴 <Pipeline> Pipeline <YYYY-MM-DD> <HH:MM UTC>
    🟢/🔴/⚪ <step name> [— <detail>]
    ...

The header is 🔴 iff any step is RED.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

logger = logging.getLogger("mhde.monitoring.pipeline_monitor")


class Status(enum.Enum):
    GREEN = "green"
    RED = "red"
    SKIPPED = "skipped"


STATUS_EMOJI: dict[Status, str] = {
    Status.GREEN: "🟢",
    Status.RED: "🔴",
    Status.SKIPPED: "⚪",
}


@dataclass
class StepResult:
    """Outcome of one pipeline step.

    ``detail`` is a short human note — a row count, a date, a reason — shown
    after an em-dash in the Telegram message and always logged.
    """

    name: str
    status: Status
    detail: str = ""

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI[self.status]


@dataclass
class PipelineResult:
    pipeline: str          # display name, e.g. "Crypto" / "Equity" / "FX" / "Continuous"
    as_of: datetime        # tz-aware UTC
    steps: list[StepResult] = field(default_factory=list)

    @property
    def overall(self) -> Status:
        """RED if any step is RED, else GREEN. SKIPPED steps never make it RED."""
        if any(s.status is Status.RED for s in self.steps):
            return Status.RED
        return Status.GREEN

    @property
    def has_red(self) -> bool:
        return self.overall is Status.RED


# ──────────────────────────────────────────────────────────────────────
# step evaluation
# ──────────────────────────────────────────────────────────────────────
def evaluate_steps(
    steps: Iterable[tuple[str, Callable[[], StepResult]]],
    *,
    stop_on_red: bool = True,
) -> list[StepResult]:
    """Run ``steps`` (``(display_name, no-arg callable -> StepResult)``) in order.

    A callable that raises is treated as a RED step (a check that can't even
    run is itself a failure). The *registered* display name is authoritative —
    if a callable returns a StepResult with a different name, it is rewritten.

    With ``stop_on_red`` the first RED step short-circuits: every subsequent
    step is reported SKIPPED (⚪) without being run.
    """
    results: list[StepResult] = []
    tripped = False
    for name, fn in steps:
        if tripped and stop_on_red:
            results.append(StepResult(name, Status.SKIPPED, "skipped — an earlier step failed"))
            continue
        try:
            sr = fn()
        except Exception as exc:  # noqa: BLE001 — any failure to evaluate == red
            logger.exception("pipeline-monitor check %r raised", name)
            sr = StepResult(name, Status.RED, f"check raised {type(exc).__name__}: {exc}")
        sr = StepResult(name, sr.status, sr.detail)
        results.append(sr)
        if sr.status is Status.RED:
            tripped = True
    return results


# ──────────────────────────────────────────────────────────────────────
# rendering
# ──────────────────────────────────────────────────────────────────────
def render_telegram_message(result: PipelineResult) -> str:
    header_emoji = STATUS_EMOJI[Status.RED if result.has_red else Status.GREEN]
    date_str = result.as_of.strftime("%Y-%m-%d")
    time_str = result.as_of.strftime("%H:%M UTC")
    lines = [f"{header_emoji} {result.pipeline} Pipeline {date_str} {time_str}"]
    for step in result.steps:
        line = f"{step.emoji} {step.name}"
        if step.detail:
            line += f" — {step.detail}"
        lines.append(line)
    return "\n".join(lines)
