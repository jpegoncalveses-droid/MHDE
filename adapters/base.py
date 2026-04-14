from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, ClassVar, Optional


@dataclass
class Scores:
    access: int           # 1-5: reliable reachability
    completeness: int     # 1-5: required fields present
    freshness: int        # 1-5: data currency (5=realtime)
    reliability: int      # 1-5: structural consistency
    parsing_ease: int     # 1-5: ease of parsing (5=clean JSON)
    cost_efficiency: int  # 1-5: cost relative to value
    strategic_value: int  # 1-5: importance to MHDE

    def __post_init__(self):
        for name, val in (
            ("access", self.access), ("completeness", self.completeness),
            ("freshness", self.freshness), ("reliability", self.reliability),
            ("parsing_ease", self.parsing_ease), ("cost_efficiency", self.cost_efficiency),
            ("strategic_value", self.strategic_value),
        ):
            if not 1 <= val <= 5:
                raise ValueError(f"Scores.{name}={val} is outside the 1-5 range")

    def total(self) -> int:
        return (self.access + self.completeness + self.freshness +
                self.reliability + self.parsing_ease +
                self.cost_efficiency + self.strategic_value)

    def to_dict(self) -> dict:
        return {
            "access": self.access,
            "completeness": self.completeness,
            "freshness": self.freshness,
            "reliability": self.reliability,
            "parsing_ease": self.parsing_ease,
            "cost_efficiency": self.cost_efficiency,
            "strategic_value": self.strategic_value,
            "total": self.total(),
        }


@dataclass
class ValidationResult:
    source: str
    use_case: str
    tickers_tested: list[str]
    access_result: str          # ok | auth_fail | rate_limited | error
    access_error: Optional[str]
    required_fields_present: bool
    missing_fields: list[str]
    historical_depth: str       # e.g. "5y", "90d", "N/A"
    freshness: str              # e.g. "same-day", "1d", "stale", "N/A"
    parsing_difficulty: str     # easy | moderate | hard
    rate_limit_notes: str
    fallback_suggestion: str
    final_status: str           # Core | Useful but optional | Fallback only | Reject for v1
    notes: str
    scores: Scores
    raw_sample_path: Optional[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scores"] = self.scores.to_dict()
        return d


class BaseAdapter(ABC):
    source_name: ClassVar[str]
    use_cases: ClassVar[list[str]]

    def __init__(self, settings: dict, tickers_config: list[dict]):
        self.settings = settings
        self.tickers_config = tickers_config
        self.logger = logging.getLogger(f"mhde.adapter.{self.source_name}")
        samples_dir = settings.get("outputs", {}).get("samples_dir", "samples")
        self._samples_dir = Path(samples_dir)
        self._samples_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def test_access(self) -> tuple[str, Optional[str]]:
        """Returns (access_result, error_message)."""

    @abstractmethod
    def fetch_sample_data(self, tickers: list[dict], use_case: str) -> Optional[Any]:
        """Fetch data for use_case. Returns None on unrecoverable failure."""

    @abstractmethod
    def validate_schema(self, data: Any, use_case: str) -> tuple[bool, list[str]]:
        """Returns (all_required_present, missing_field_names)."""

    @abstractmethod
    def evaluate_freshness(self, data: Any, use_case: str) -> str:
        """Returns freshness string: 'same-day'|'1d'|'1w'|'>1mo'|'N/A'."""

    @abstractmethod
    def evaluate_history(self, data: Any, use_case: str) -> str:
        """Returns history depth string: '5y'|'2y'|'90d'|'N/A'."""

    @abstractmethod
    def summarize_result(
        self,
        data: Optional[Any],
        use_case: str,
        access_result: str,
    ) -> ValidationResult:
        """Build the full ValidationResult for this use_case."""

    def run(self, tickers: list[dict]) -> list[ValidationResult]:
        access_result, access_error = self.test_access()
        self.logger.info("%s access: %s", self.source_name, access_result)
        results = []
        for use_case in self.use_cases:
            self.logger.info("Running %s / %s", self.source_name, use_case)
            data = None
            sample_path = None
            if access_result == "ok":
                try:
                    data = self.fetch_sample_data(tickers, use_case)
                    if data is not None:
                        sample_path = self._save_sample(data, use_case)
                except Exception as exc:
                    self.logger.error("%s/%s fetch error: %s", self.source_name, use_case, exc)
            result = self.summarize_result(data, use_case, access_result)
            result.access_error = access_error
            if sample_path is not None:
                result.raw_sample_path = str(sample_path)
            results.append(result)
            self.logger.info("%s/%s -> %s", self.source_name, use_case, result.final_status)
        return results

    def _save_sample(self, data: Any, use_case: str) -> Path:
        path = self._samples_dir / f"{self.source_name}_{use_case}.json"
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
        self.logger.debug("Sample saved: %s", path)
        return path
