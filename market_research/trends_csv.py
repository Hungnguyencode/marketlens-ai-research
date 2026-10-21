from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import Evidence


class GoogleTrendsCsvCollector:
    """Import a real, user-exported Google Trends chart CSV; this is not autonomous collection."""

    name = "google_trends_csv_import"
    source_name = "Google Trends (manual CSV export)"
    date_headers = {"week", "tuần", "day", "ngày", "month", "tháng", "date", "time"}

    def __init__(self, csv_path: str | Path, source_url: str):
        self.csv_path = Path(csv_path)
        self.source_url = source_url.strip()

    def _validate_source_url(self) -> None:
        parsed = urlparse(self.source_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (hostname == "google.com" or hostname.endswith(".google.com")):
            raise ValueError("Source URL must be the actual HTTPS Google Trends page used for the export")
        if "/trends/" not in parsed.path:
            raise ValueError("Source URL must point to a Google Trends page")

    @staticmethod
    def _normalized_header(value: str) -> str:
        # Canonical normalization makes precomposed and decomposed Vietnamese equivalent.
        return unicodedata.normalize("NFC", value).strip().casefold()

    @staticmethod
    def _keyword_from_header(value: str) -> str:
        return re.sub(r"\s*:\s*\([^)]*\)\s*$", "", value.strip())

    @staticmethod
    def _geography_from_header(value: str) -> str | None:
        match = re.search(r"\s*:\s*\(([^)]*)\)\s*$", value.strip())
        return match.group(1) if match else None

    @staticmethod
    def _numeric_value(value: str) -> float | None:
        value = value.strip().replace(",", "")
        if not value or value.lower() in {"<1", "< 1", "n/a", "na"}:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def collect(self, *, run_id: str, niche: str, keyword: str, target_region: str) -> list[Evidence]:
        self._validate_source_url()
        # Google Trends chart exports commonly include a short metadata preamble and UTF-8 BOM.
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as source_file:
            rows = list(csv.reader(source_file))
        header_index = next((i for i, row in enumerate(rows)
            if row and self._normalized_header(row[0]) in self.date_headers and len(row) > 1), None)
        if header_index is None:
            raise ValueError("CSV has no recognizable Google Trends time-series header (Week/Day/Month/Date)")

        header = rows[header_index]
        period_column = header[0].strip()
        series = [(i, self._keyword_from_header(name)) for i, name in enumerate(header[1:], start=1) if name.strip()]
        if not series:
            raise ValueError("Google Trends CSV has no keyword series columns")

        collected_at = datetime.now(timezone.utc)
        evidence: list[Evidence] = []
        for row in rows[header_index + 1:]:
            if not row or not row[0].strip():
                continue
            period = row[0].strip()
            for column_index, series_keyword in series:
                raw_value = row[column_index].strip() if column_index < len(row) else ""
                if not raw_value:
                    continue
                metric_value = self._numeric_value(raw_value)
                digest = hashlib.sha256(f"{run_id}|{self.name}|{series_keyword}|{period}".encode("utf-8")).hexdigest()[:16]
                evidence.append(Evidence(
                    evidence_id=f"{run_id}-{digest}", run_id=run_id, niche=niche,
                    keyword=series_keyword or keyword, source_name=self.source_name, source_url=self.source_url,
                    collected_at=collected_at, target_region=target_region, observation_period=period,
                    metric_name="search_interest_index", metric_value=metric_value,
                    metric_unit="Google Trends relative index (0-100; not absolute search volume)",
                    source_excerpt=f"{period_column} {period}: {raw_value}",
                    raw_observation={"period_column": period_column, "period": period,
                        "series_column": header[column_index], "raw_value": raw_value,
                        "geographic_metadata": self._geography_from_header(header[column_index]),
                        "csv_filename": self.csv_path.name},
                    extraction_method="User-provided Google Trends chart CSV import; manual export, not autonomous collection",
                    collection_status="success" if metric_value is not None else "partial"))
        if not evidence:
            raise ValueError("Google Trends CSV contained no time-series observations")
        return evidence
