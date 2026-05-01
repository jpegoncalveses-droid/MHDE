from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime

import duckdb

from llm.schemas import LLMOutput
from llm.local_provider import MockProvider

logger = logging.getLogger("mhde.llm")


def _get_provider(cfg: dict):
    llm_cfg = cfg.get("llm", {})
    provider_name = llm_cfg.get("default_provider", "mock")

    if provider_name == "openai":
        from llm.openai_provider import OpenAIProvider
        return OpenAIProvider(cfg)
    elif provider_name == "nvidia":
        from llm.nvidia_provider import NvidiaProvider
        return NvidiaProvider(cfg)
    else:
        if provider_name != "mock":
            logger.warning("Unknown LLM provider '%s' — using mock", provider_name)
        return MockProvider()


def _log_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    job_type: str,
    output: LLMOutput,
    input_data: dict,
    estimated_tokens: int = 0,
    estimated_cost: float = 0.0,
) -> None:
    input_json = json.dumps(input_data, default=str)
    output_json = json.dumps(output.to_dict(), default=str)
    try:
        conn.execute(
            """
            INSERT INTO llm_runs
                (llm_run_id, run_id, ticker, job_type, provider, model,
                 prompt_version, input_hash, output_hash, input_json, output_json,
                 estimated_tokens, estimated_cost, status, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex[:16], run_id, ticker, job_type,
                output.provider, output.model, output.prompt_version,
                hashlib.md5(input_json.encode()).hexdigest()[:8],
                hashlib.md5(output_json.encode()).hexdigest()[:8],
                input_json, output_json,
                estimated_tokens, estimated_cost,
                "error" if output.error else "ok",
                output.error,
                datetime.utcnow(),
            ],
        )
    except Exception as exc:
        logger.debug("Could not log LLM run: %s", exc)


def run_briefs(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    hypotheses: list[dict],
    cfg: dict,
) -> list[LLMOutput]:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("enabled", True):
        logger.info("LLM disabled in config — skipping briefs")
        return []

    max_candidates = llm_cfg.get("max_candidates", 10)
    provider = _get_provider(cfg)
    outputs = []

    is_mock = isinstance(provider, MockProvider)
    if is_mock:
        logger.warning(
            "LLM running in mock mode — configure OPENAI_API_KEY or NVIDIA_API_KEY "
            "for real analysis."
        )

    for hyp in hypotheses[:max_candidates]:
        ticker = hyp.get("ticker") or hyp[2] if isinstance(hyp, tuple) else hyp.get("ticker")
        context = hyp if isinstance(hyp, dict) else {}

        try:
            output = provider.generate(ticker, "hypothesis_generation", context)
            _log_run(conn, run_id, ticker, "hypothesis_generation", output, context)
            outputs.append(output)
            logger.info("Brief generated for %s (%s, confidence=%s)",
                        ticker, output.provider, output.confidence)
        except Exception as exc:
            logger.error("Brief failed for %s: %s", ticker, exc)

    return outputs
