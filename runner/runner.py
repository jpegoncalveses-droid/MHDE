from __future__ import annotations

import logging
from typing import Optional, Sequence

from adapters.base import BaseAdapter, ValidationResult

logger = logging.getLogger("mhde.runner")


class ValidationRunner:
    def __init__(
        self,
        settings: dict,
        tickers: list[dict],
        adapters: Optional[list] = None,
        source_filter: Optional[list[str]] = None,
    ):
        self.settings = settings
        self.tickers = tickers
        self.source_filter = source_filter
        self._adapters: list = adapters if adapters is not None else self._build_adapters()

    def _build_adapters(self) -> list:
        from adapters.sec_edgar import SECEdgarAdapter
        from adapters.polygon import PolygonAdapter
        from adapters.alpha_vantage import AlphaVantageAdapter
        from adapters.company_ir import CompanyIRAdapter
        from adapters.nasdaq_earnings import NasdaqEarningsAdapter
        from adapters.fred import FREDAdapter

        return [
            SECEdgarAdapter(settings=self.settings, tickers_config=self.tickers),
            PolygonAdapter(settings=self.settings, tickers_config=self.tickers),
            AlphaVantageAdapter(settings=self.settings, tickers_config=self.tickers),
            CompanyIRAdapter(settings=self.settings, tickers_config=self.tickers),
            NasdaqEarningsAdapter(settings=self.settings, tickers_config=self.tickers),
            FREDAdapter(settings=self.settings, tickers_config=self.tickers),
        ]

    def run(self) -> list[ValidationResult]:
        all_results: list[ValidationResult] = []
        for adapter in self._adapters:
            if self.source_filter and adapter.source_name not in self.source_filter:
                logger.info("Skipping %s (filtered out)", adapter.source_name)
                continue
            logger.info("=== Running adapter: %s ===", adapter.source_name)
            try:
                results = adapter.run(self.tickers)
                all_results.extend(results)
            except Exception as exc:
                logger.error("Adapter %s crashed: %s", adapter.source_name, exc)
        return all_results
