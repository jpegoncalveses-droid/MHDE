from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

import duckdb

logger = logging.getLogger("mhde.ingestion")


class BaseIngestor(ABC):
    source_name: ClassVar[str]
    source_status: ClassVar[str] = "active"  # active|experimental|stub|disabled

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.logger = logging.getLogger(f"mhde.ingestion.{self.source_name}")

    @abstractmethod
    def ingest(
        self,
        conn: duckdb.DuckDBPyConnection,
        run_id: str,
        tickers: list[str],
    ) -> dict:
        """Ingest data into DuckDB. Returns summary dict."""

    def log_run(
        self,
        conn: duckdb.DuckDBPyConnection,
        run_id: str,
        use_case: str,
        status: str,
        records_attempted: int = 0,
        records_inserted: int = 0,
        records_failed: int = 0,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        now = datetime.utcnow()
        try:
            conn.execute(
                """
                INSERT INTO source_runs
                    (id, run_id, source_name, use_case, status,
                     started_at, finished_at,
                     records_attempted, records_inserted, records_failed,
                     error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    uuid.uuid4().hex[:16],
                    run_id,
                    self.source_name,
                    use_case,
                    status,
                    started_at or now,
                    finished_at or now,
                    records_attempted,
                    records_inserted,
                    records_failed,
                    error_message,
                    now,
                ],
            )
        except Exception as exc:
            self.logger.debug("Could not log source run: %s", exc)


class StubIngestor(BaseIngestor):
    source_status: ClassVar[str] = "stub"

    def ingest(self, conn, run_id, tickers):
        self.logger.info("[STUB] %s ingestion not yet implemented", self.source_name)
        self.log_run(conn, run_id, "stub", "stub", 0, 0, 0)
        return {"source": self.source_name, "status": "stub", "records": 0}
