from __future__ import annotations

import logging
import uuid
from datetime import datetime

import requests

from ingestion.base_ingestor import BaseIngestor

logger = logging.getLogger("mhde.ingestion.cftc")

_TFF_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_DISAG_URL = "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json"

_INDEX_SERIES = {
    "E-MINI S&P 500": "cftc_es_net_lev",
    "NASDAQ MINI": "cftc_nq_net_lev",
    "RUSSELL E-MINI": "cftc_rty_net_lev",
}
_COMMODITY_SERIES = {
    "CRUDE OIL, LIGHT SWEET": "cftc_wti_net_mm",
    "GOLD - COMMODITY EXCHANGE": "cftc_gold_net_mm",
}


def _safe_int(row: dict, *fields: str) -> int:
    for f in fields:
        v = row.get(f)
        if v is not None:
            try:
                return int(v)
            except (ValueError, TypeError):
                continue
    return 0


class CFTCIngestor(BaseIngestor):
    source_name = "cftc"

    def _fetch(self, url: str, limit: int = 400) -> list[dict]:
        try:
            r = requests.get(
                url,
                params={"$limit": str(limit), "$order": "report_date_as_yyyy_mm_dd DESC"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            logger.warning("CFTC fetch failed: %s", exc)
        return []

    def ingest(self, conn, run_id, tickers):
        started = datetime.utcnow()
        inserted = 0

        for rows, targets, net_field in [
            (self._fetch(_TFF_URL), _INDEX_SERIES, "lev_money_positions_long_all"),
            (self._fetch(_DISAG_URL), _COMMODITY_SERIES, "m_money_positions_long_all"),
        ]:
            for row in rows:
                market = row.get("market_and_exchange_names", "").upper()
                rd_raw = row.get("report_date_as_yyyy_mm_dd", "")
                as_of_str = rd_raw[:10] if rd_raw else None
                if not as_of_str:
                    continue
                for keyword, series_id in targets.items():
                    if keyword.upper() in market:
                        long_ = _safe_int(row, net_field)
                        short_field = net_field.replace("long", "short")
                        short_ = _safe_int(row, short_field)
                        net = long_ - short_
                        try:
                            as_of = datetime.strptime(as_of_str, "%Y-%m-%d").date()
                            conn.execute(
                                """
                                INSERT INTO macro_series
                                    (id, series_id, series_name, value, as_of_date,
                                     source, run_id, created_at)
                                VALUES (?, ?, ?, ?, ?, 'cftc', ?, ?)
                                ON CONFLICT (series_id, as_of_date) DO NOTHING
                                """,
                                [
                                    uuid.uuid4().hex[:16], series_id,
                                    f"CFTC CoT: {keyword}", float(net),
                                    as_of, run_id, datetime.utcnow(),
                                ],
                            )
                            inserted += 1
                        except Exception:
                            pass

        self.log_run(conn, run_id, "cot_positioning", "ok",
                     inserted, inserted, 0, started_at=started)
        self.logger.info("CFTC: %d CoT observations inserted", inserted)
        return {"source": self.source_name, "status": "ok", "records": inserted}
