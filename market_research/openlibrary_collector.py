"""Open Library catalog metadata collection, separate from market-demand evidence."""
from __future__ import annotations

import argparse
import json
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

import httpx
from pydantic import BaseModel, Field

from .logging import ExecutionLogger
from .tools import ToolRegistry


class CatalogItem(BaseModel):
    original_query: str
    title: str
    work_id: str
    authors: list[str] = Field(default_factory=list)
    first_publish_year: int | None = None
    edition_count: int | None = None
    work_url: str
    source_name: str = "Open Library Search API"
    retrieved_at: datetime
    request_url: str
    raw_record: dict[str, Any]
    relevance_score: float
    relevance_status: Literal["candidate", "low_relevance"]
    provenance: Literal["live", "cache_replay"] = "live"


class CollectionResult(BaseModel):
    provider: str = "Open Library Search API"
    query: str
    status: Literal["success", "empty", "partial", "failed"]
    retrieved_at: datetime
    request_url: str
    source_total_matches: int | None = None
    raw_items_returned: int = 0
    items: list[CatalogItem] = Field(default_factory=list)
    relevant_items: list[CatalogItem] = Field(default_factory=list)
    malformed_records_skipped: int = 0
    duplicate_records_skipped: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_response: dict[str, Any] | None = None
    provenance: Literal["live", "cache_replay"] = "live"


class OpenLibraryCollector:
    endpoint = "https://openlibrary.org/search.json"
    fields = "key,title,author_name,first_publish_year,edition_count"
    name = "openlibrary_search"
    source_name = "Open Library Search API"

    def __init__(self, *, timeout: float = 15, client=None, min_interval_seconds: float = 1.0,
                 max_retries: int = 1, max_retry_wait_seconds: float = 3,
                 sleep_fn=time.sleep, random_fn=random.random, clock_fn=time.monotonic):
        self.timeout = timeout
        self.client = client
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.max_retries = max(0, min(int(max_retries), 1))
        self.max_retry_wait_seconds = max(0.0, max_retry_wait_seconds)
        self.sleep_fn, self.random_fn, self.clock_fn = sleep_fn, random_fn, clock_fn
        self._last_request_at: float | None = None

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = unicodedata.normalize("NFC", text).casefold()
        return {x for x in re.findall(r"[\w]+", normalized)
                if x not in {"book", "books", "for", "and", "the", "of", "to", "a", "an"}}

    @classmethod
    def relevance(cls, query: str, title: str) -> tuple[float, Literal["candidate", "low_relevance"]]:
        terms = cls._tokens(query)
        if not terms:
            return 0.0, "low_relevance"
        overlap = terms & cls._tokens(title)
        score = len(overlap) / len(terms)
        return score, "candidate" if overlap and score >= 0.5 else "low_relevance"

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _paced_get(self, client: httpx.Client, params: dict[str, Any]) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            now = self.clock_fn()
            if self._last_request_at is not None:
                delay = self.min_interval_seconds - (now - self._last_request_at)
                if delay > 0:
                    self.sleep_fn(delay)
            self._last_request_at = self.clock_fn()
            response = client.get(self.endpoint, params=params, timeout=self.timeout)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt >= self.max_retries:
                return response
            retry_after = self._retry_after(response)
            delay = retry_after if retry_after is not None else min(2.0, 0.5 * (2**attempt) + self.random_fn() * 0.25)
            if delay > self.max_retry_wait_seconds:
                return response
            self.sleep_fn(delay)
        raise AssertionError("unreachable")

    def collect(self, query: str, limit: int = 10) -> CollectionResult:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 20))
        params = {"q": query, "fields": self.fields, "limit": limit}
        # Open Library does not require credentials. Keep the exact request URL for provenance.
        request_url = f"{self.endpoint}?q={quote_plus(query)}&fields={quote_plus(self.fields)}&limit={limit}"
        now = datetime.now(timezone.utc)
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout,
            headers={"User-Agent": "EvidenceFirstMarketResearch/0.1 (metadata research)"})
        try:
            response = self._paced_get(client, params)
            if response.status_code >= 400:
                return CollectionResult(query=query, status="failed", retrieved_at=now,
                    request_url=request_url, errors=[{"kind": "http_error", "status_code": response.status_code,
                        "retry_after_seconds": self._retry_after(response)}])
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                return CollectionResult(query=query, status="failed", retrieved_at=now,
                    request_url=request_url, errors=[{"kind": "invalid_json", "status_code": response.status_code}])
        except httpx.TimeoutException:
            return CollectionResult(query=query, status="failed", retrieved_at=now, request_url=request_url,
                errors=[{"kind": "timeout", "message": f"Request exceeded {self.timeout}s timeout"}])
        except httpx.RequestError as exc:
            return CollectionResult(query=query, status="failed", retrieved_at=now, request_url=request_url,
                errors=[{"kind": "network_error", "exception_type": type(exc).__name__}])
        finally:
            if owns_client:
                client.close()

        if not isinstance(payload, dict) or not isinstance(payload.get("docs"), list):
            return CollectionResult(query=query, status="failed", retrieved_at=now, request_url=request_url,
                errors=[{"kind": "unexpected_response_shape", "expected": "object with docs array"}],
                raw_response=payload if isinstance(payload, dict) else None)
        docs = payload["docs"]
        items: list[CatalogItem] = []
        seen: set[str] = set()
        malformed = duplicate = 0
        warnings: list[str] = []
        for doc in docs:
            if not isinstance(doc, dict):
                malformed += 1
                continue
            key, title = doc.get("key"), doc.get("title")
            if not isinstance(key, str) or not re.fullmatch(r"/?works/OL[0-9]+W", key.strip("/")):
                malformed += 1
                continue
            if not isinstance(title, str) or not title.strip():
                malformed += 1
                continue
            work_id = key.rstrip("/").split("/")[-1]
            if work_id in seen:
                duplicate += 1
                continue
            seen.add(work_id)
            authors_value = doc.get("author_name", [])
            authors = [x.strip() for x in authors_value if isinstance(x, str) and x.strip()] if isinstance(authors_value, list) else []
            year = doc.get("first_publish_year")
            if not isinstance(year, int) or isinstance(year, bool):
                year = None
            count = doc.get("edition_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                count = None
            score, relevance_status = self.relevance(query, title.strip())
            item = CatalogItem(original_query=query, title=title.strip(), work_id=work_id, authors=authors,
                first_publish_year=year, edition_count=count, work_url=f"https://openlibrary.org/works/{work_id}",
                retrieved_at=now, request_url=request_url, raw_record=doc, relevance_score=score,
                relevance_status=relevance_status)
            items.append(item)
        if malformed:
            warnings.append(f"Skipped {malformed} malformed record(s).")
        if duplicate:
            warnings.append(f"Skipped {duplicate} duplicate work identifier(s).")
        matches = payload.get("num_found", payload.get("numFound"))
        if not isinstance(matches, int) or isinstance(matches, bool):
            matches = None
            warnings.append("Response omitted a valid num_found/numFound field.")
        status: Literal["success", "empty", "partial", "failed"]
        if not docs:
            status = "empty"
        elif malformed or len(items) != len(docs):
            status = "partial" if items else "failed"
        else:
            status = "success"
        relevant = [item for item in items if item.relevance_status == "candidate"]
        if not relevant and items:
            warnings.append("No records met the transparent title/query relevance threshold; none accepted as candidates.")
        warnings.append("Catalog metadata and edition counts are not sales, demand, price, or complete product-competition measures.")
        return CollectionResult(query=query, status=status, retrieved_at=now, request_url=request_url,
            source_total_matches=matches, raw_items_returned=len(docs), items=items,
            relevant_items=relevant, malformed_records_skipped=malformed, duplicate_records_skipped=duplicate,
            warnings=warnings, raw_response=payload)


def collect_openlibrary(query: str, limit: int = 10, **collector_options: Any) -> CollectionResult:
    """Reusable Milestone 2.2 entry point; collection is independent of Ollama."""
    return OpenLibraryCollector(**collector_options).collect(query=query, limit=limit)


def openlibrary_tool_registry(collector: OpenLibraryCollector | None = None) -> ToolRegistry:
    """Expose this independent source through the project's bounded tool abstraction."""
    collector = collector or OpenLibraryCollector()
    return ToolRegistry({collector.name: lambda query, limit=10: collector.collect(query=query, limit=limit)})


def _replay_snapshot(path: Path) -> CollectionResult:
    result = CollectionResult.model_validate_json(path.read_text(encoding="utf-8"))
    result.provenance = "cache_replay"
    for item in result.items:
        item.provenance = "cache_replay"
    for item in result.relevant_items:
        item.provenance = "cache_replay"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Open Library catalog metadata without Ollama")
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", choices=("live", "replay"), default="live")
    parser.add_argument("--snapshot", help="Write a live JSON snapshot, or read one in replay mode")
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args()
    if args.mode == "replay":
        if not args.snapshot:
            parser.error("--mode replay requires --snapshot")
        result = _replay_snapshot(Path(args.snapshot))
    else:
        registry = openlibrary_tool_registry(OpenLibraryCollector(timeout=15))
        result = registry.call("openlibrary_search", budget=1, query=args.query, limit=args.limit)
        if args.snapshot:
            destination = Path(args.snapshot)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger = ExecutionLogger(Path(args.log_dir) / "openlibrary-collector.jsonl", f"openlibrary-{int(time.time())}")
    logger.step("collect_openlibrary", "OpenLibraryCollector", result.status,
        f"query={result.query}; raw={result.raw_items_returned}; parsed={len(result.items)}; relevant={len(result.relevant_items)}",
        json.dumps(result.errors, ensure_ascii=False) if result.errors else None)
    output = {"provider": result.provider, "query": result.query, "status": result.status,
        "provenance": result.provenance, "retrieved_at": result.retrieved_at.isoformat(),
        "request_url": result.request_url, "raw_items_returned": result.raw_items_returned,
        "parsed_items": len(result.items), "relevant_items_accepted": len(result.relevant_items),
        "source_total_matches": result.source_total_matches,
        "sample_source_urls": [item.work_url for item in result.relevant_items[:5]],
        "errors": result.errors, "warnings": result.warnings,
        "snapshot": args.snapshot, "execution_log": str(Path(args.log_dir) / "openlibrary-collector.jsonl")}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if result.status == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
