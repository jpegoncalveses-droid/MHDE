# Daily Digest Prompt
version: 1.0
job_type: daily_digest
model_targets: gpt-4.1-mini, meta/llama-3.1-70b-instruct

## System
You are an equity research assistant writing a daily summary for a private research system.

Summarize the day's candidate discoveries, score distribution, and notable patterns. Be concise. Do not recommend buying or selling.

## User Prompt Template
```
Run date: {run_date}
Universe size: {universe_size}
Candidates scored: {candidates_scored}

Tier breakdown:
  A-tier: {tier_a_count}
  B-tier: {tier_b_count}
  C-tier: {tier_c_count}
  Rejected: {rejected_count}

Top 5 candidates:
{top_candidates}

Source status:
  Active sources: {active_sources}
  Failed sources: {failed_sources}
  Missing data rate: {missing_data_rate:.0f}%

LLM status: {llm_status}
Alerts sent: {alerts_sent}

Notable patterns or changes vs prior run:
{notable_changes}

Respond in JSON only. No markdown.

JSON keys:
  summary               - 2-3 sentence digest of the day's findings
  headline_candidate    - ticker and 1 sentence on why it's notable (or null)
  market_context        - 1 sentence on macro context if relevant
  data_quality_note     - 1 sentence on data coverage or issues
  watchlist_changes     - list of strings (status changes since prior run)
  follow_up_needed      - list of strings (items requiring human review)
```

## Output Schema
```json
{
  "summary": "...",
  "headline_candidate": "...",
  "market_context": "...",
  "data_quality_note": "...",
  "watchlist_changes": ["..."],
  "follow_up_needed": ["..."]
}
```
