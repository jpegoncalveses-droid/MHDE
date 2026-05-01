from __future__ import annotations

from abc import ABC, abstractmethod

from llm.schemas import LLMOutput


class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(self, ticker: str, job_type: str, context: dict) -> LLMOutput:
        """Generate LLM output for the given ticker and job type."""
