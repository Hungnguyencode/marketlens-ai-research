from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

from .models import Evidence


class GoogleBooksCollectionError(RuntimeError):
    """Safe-to-log collector error; never includes the request URL or API key."""

    def __init__(self, message: str, *, status_code: int | None = None, reason: str | None = None,
                 attempts: int = 1, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.attempts = attempts
        self.retry_after_seconds = retry_after_seconds

    def diagnostic(self) -> dict:
        return {"source": "Google Books API", "status_code": self.status_code,
                "reason": self.reason, "attempts": self.attempts,
                "retry_after_seconds": self.retry_after_seconds, "message": str(self)}


class GoogleBooksCollector:
    """Collect bibliographic metadata, never interpret catalog records as demand."""

    name = "google_books_catalog"
    endpoint = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, timeout: float = 15, api_key: str | None = None, client=None,
                 max_retries: int = 2, base_backoff_seconds: float = 0.6,
                 max_backoff_seconds: float = 8.0, sleep_fn=time.sleep, random_fn=random.random):
        self.timeout, self.api_key, self.client = timeout, api_key, client
        self.max_retries = max(0, min(int(max_retries), 2))
        self.base_backoff_seconds = max(0.0, base_backoff_seconds)
        self.max_backoff_seconds = max(0.0, max_backoff_seconds)
        self.sleep_fn, self.random_fn = sleep_fn, random_fn

    @staticmethod
    def _error_fields(response: httpx.Response) -> tuple[str, str]:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        message = str(error.get("message") or "No API error message provided")
        reasons = []
        error_items = error.get("errors", []) if isinstance(error, dict) else []
        for item in error_items if isinstance(error_items, list) else []:
            if item.get("reason"):
                reasons.append(str(item["reason"]))
        reason = ",".join(reasons) or ("quota_or_rate_limit" if response.status_code == 429 else "http_error")
        return reason, message[:300]

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text

    def _request(self, params: dict) -> dict:
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout, headers={"User-Agent": "EvidenceFirstResearch/0.2"})
        try:
            return self._request_with_client(client, params)
        finally:
            if owns_client:
                client.close()

    def _request_with_client(self, client, params: dict) -> dict:
        for attempt in range(1, self.max_retries + 2):
            try:
                response = client.get(self.endpoint, params=params, timeout=self.timeout)
            except httpx.TimeoutException as exc:
                raise GoogleBooksCollectionError("Google Books request timed out", reason="timeout", attempts=attempt) from exc
            except httpx.RequestError as exc:
                # Do not forward exception text: it can contain the full URL including API key.
                raise GoogleBooksCollectionError(f"Google Books network request failed ({type(exc).__name__})",
                    reason="network_error", attempts=attempt) from exc

            if response.status_code == 429:
                reason, message = self._error_fields(response)
                retry_after = self._retry_after(response)
                if attempt > self.max_retries:
                    raise GoogleBooksCollectionError(f"Google Books rate limit/quota response (HTTP 429): {self._redact(message)}",
                        status_code=429, reason=reason, attempts=attempt, retry_after_seconds=retry_after)
                delay = retry_after
                if delay is None:
                    delay = min(self.max_backoff_seconds,
                                self.base_backoff_seconds * (2 ** (attempt - 1)) + self.random_fn() * 0.25)
                if delay > self.max_backoff_seconds:
                    raise GoogleBooksCollectionError(
                        f"Google Books requested a {delay:.1f}s retry delay, beyond the configured {self.max_backoff_seconds:.1f}s wait cap; stopped without retry",
                        status_code=429, reason=reason, attempts=attempt, retry_after_seconds=delay)
                self.sleep_fn(delay)
                continue

            if response.status_code >= 400:
                reason, message = self._error_fields(response)
                if response.status_code == 403 and any(x in reason.lower() for x in ("quota", "limit", "rate")):
                    reason = reason or "quota_or_permission_denied"
                raise GoogleBooksCollectionError(
                    f"Google Books API returned HTTP {response.status_code}: {self._redact(message)}",
                    status_code=response.status_code, reason=reason, attempts=attempt)
            try:
                return response.json()
            except ValueError as exc:
                raise GoogleBooksCollectionError("Google Books returned invalid JSON", status_code=response.status_code,
                    reason="invalid_json", attempts=attempt) from exc
        raise AssertionError("unreachable")

    def collect(self, *, run_id: str, niche: str, keyword: str, target_region: str) -> list[Evidence]:
        params = {"q": keyword, "maxResults": 40, "printType": "books", "orderBy": "relevance"}
        if self.api_key:
            params["key"] = self.api_key
        payload = self._request(params)
        observed = datetime.now(timezone.utc)
        total = payload.get("totalItems")
        items = payload.get("items", [])
        # This source URL is reproducible and intentionally excludes the optional API key.
        query_url = f"{self.endpoint}?q={quote_plus(keyword)}&maxResults=40&printType=books"
        key = hashlib.sha256(f"{run_id}|{self.name}|{keyword}".encode()).hexdigest()[:16]
        evidences = [Evidence(evidence_id=f"{run_id}-{key}-count", run_id=run_id, niche=niche, keyword=keyword,
            source_name="Google Books API", source_url=query_url, collected_at=observed, target_region=target_region,
            observation_period="snapshot at collection time", metric_name="catalog_results_reported",
            metric_value=total, metric_unit="bibliographic records (API-reported; not sales or demand)",
            source_excerpt=f"Google Books API reported {total} matching catalog records for this query.",
            raw_observation={"totalItems": total, "returnedItems": len(items)}, extraction_method="Google Books Volumes API JSON",
            collection_status="success")]
        for i, item in enumerate(items[:40]):
            info = item.get("volumeInfo", {})
            sale = item.get("saleInfo", {}).get("listPrice", {})
            title = info.get("title", "Untitled")
            authors = info.get("authors", [])
            price = sale.get("amount")
            evidences.append(Evidence(evidence_id=f"{run_id}-{key}-item-{i+1}", run_id=run_id, niche=niche,
                keyword=keyword, source_name="Google Books API", source_url=info.get("infoLink", query_url),
                collected_at=observed, target_region=target_region, observation_period="catalog snapshot",
                metric_name="published_list_price" if price is not None else "catalog_record",
                metric_value=price if price is not None else None, metric_unit=sale.get("currencyCode"),
                source_excerpt=f"{title} — {', '.join(authors)}"[:500],
                raw_observation={"id": item.get("id"), "title": title, "authors": authors,
                    "publishedDate": info.get("publishedDate"), "categories": info.get("categories", []),
                    "infoLink": info.get("infoLink"), "listPrice": sale or None},
                extraction_method="Google Books Volumes API JSON", collection_status="success"))
        return evidences
