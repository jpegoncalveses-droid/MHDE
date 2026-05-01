# MHDE Prompt Registry

All LLM prompts are stored as Markdown files in `llm/prompts/`. Each file includes version,
job_type, and model targets in its header.

## Prompts

| Name | Job Type | Version | Models |
|------|----------|---------|--------|
| hypothesis_generation | hypothesis_generation | 1.0 | gpt-4.1-mini, meta/llama-3.1-70b-instruct |
| catalyst_extraction | catalyst_extraction | 1.0 | gpt-4.1-mini, meta/llama-3.1-70b-instruct |
| thesis_critique | thesis_critique | 1.0 | gpt-4.1-mini, meta/llama-3.1-70b-instruct |
| rejection_reason | rejection_reason | 1.0 | gpt-4.1-mini, meta/llama-3.1-70b-instruct |
| daily_digest | daily_digest | 1.0 | gpt-4.1-mini, meta/llama-3.1-70b-instruct |

## Versioning Policy

- Bump version when the prompt structure or required JSON keys change.
- All LLM calls log `prompt_version` to `llm_runs`.
- Do not delete prompt files — archive them with a version suffix if replaced.
