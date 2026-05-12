"""Pipeline monitoring — one Telegram message per pipeline, every step green/red/skipped.

See ``docs/PIPELINE_MONITORING.md`` and DECISIONS.md (the pipeline-monitor ADR).

Layout:
  core.py              — Status / StepResult / PipelineResult + render + step evaluator
  checks/crypto.py     — one outcome-based check per crypto pipeline step
  checks/equity.py     — same for the equity pipeline
  checks/fx.py         — same for the FX pipeline
  daily_runner.py      — runs all checks for one pipeline, sends one Telegram message
  continuous_runner.py — runs the continuous checks, sends Telegram only if any red
"""
