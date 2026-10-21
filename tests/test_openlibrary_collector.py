from datetime import datetime, timezone

import httpx

from market_research.openlibrary_collector import (OpenLibraryCollector, _replay_snapshot,
    collect_openlibrary, openlibrary_tool_registry)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_valid_records_are_normalized_with_traceable_provenance():
    payload = {"numFound": 17, "docs": [
        {"key": "/works/OL123W", "title": "Cozy Coloring Book for Adults", "author_name": ["A. Author"],
         "first_publish_year": 2021, "edition_count": 2},
        {"key": "/works/OL456W", "title": "Coloring for Adults", "author_name": ["B. Writer"]},
    ]}
    def handler(request):
        assert request.url.host == "openlibrary.org"
        assert request.url.path == "/search.json"
        assert request.url.params["q"] == "cozy coloring book for adults"
        assert request.url.params["fields"] == OpenLibraryCollector.fields
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json=payload)

    result = OpenLibraryCollector(client=client_for(handler), min_interval_seconds=0).collect("cozy coloring book for adults")
    assert result.status == "success"
    assert result.raw_items_returned == 2 and result.source_total_matches == 17
    assert len(result.relevant_items) == 2
    item = result.items[0]
    assert item.original_query == "cozy coloring book for adults"
    assert item.work_id == "OL123W" and item.work_url == "https://openlibrary.org/works/OL123W"
    assert item.authors == ["A. Author"] and item.first_publish_year == 2021 and item.edition_count == 2
    assert item.source_name == "Open Library Search API" and item.retrieved_at.tzinfo == timezone.utc
    assert item.raw_record == payload["docs"][0] and item.request_url.startswith("https://openlibrary.org/search.json?")
    assert "sales" in " ".join(result.warnings).lower()
    assert not any("price" in field.lower() or "sales" in field.lower() for field in item.model_dump())


def test_empty_response_is_explicit():
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"num_found": 0, "docs": []})),
                                     min_interval_seconds=0)
    result = collector.collect("obscure coloring niche")
    assert result.status == "empty" and result.raw_items_returned == 0
    assert result.items == [] and result.relevant_items == []


def test_missing_optional_metadata_is_not_fabricated():
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={
        "docs": [{"key": "/works/OL13W", "title": "Coloring Book"}]})), min_interval_seconds=0)
    item = collector.collect("coloring books").items[0]
    assert item.authors == [] and item.first_publish_year is None and item.edition_count is None


def test_malformed_records_are_skipped_and_marked_partial():
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"docs": [
        None, {"key": "/works/OL1W"}, {"key": "/works/OL2W", "title": "Coloring Book"}]})), min_interval_seconds=0)
    result = collector.collect("coloring book")
    assert result.status == "partial" and len(result.items) == 1
    assert result.malformed_records_skipped == 2


def test_duplicate_work_ids_are_deduplicated():
    docs = [{"key": "/works/OL1W", "title": "Coloring Book"}, {"key": "/works/OL1W", "title": "Coloring Book Duplicate"}]
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"docs": docs})), min_interval_seconds=0)
    result = collector.collect("coloring book")
    assert len(result.items) == 1 and result.duplicate_records_skipped == 1
    assert result.status == "partial"


def test_irrelevant_titles_are_preserved_but_not_accepted_as_candidates():
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"docs": [
        {"key": "/works/OL1W", "title": "A History of Railways"}]})), min_interval_seconds=0)
    result = collector.collect("cozy coloring books for adults")
    assert result.status == "success" and len(result.items) == 1
    assert result.items[0].relevance_status == "low_relevance" and result.relevant_items == []


def test_429_retries_once_respecting_retry_after():
    calls, waits = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(calls) == 1 else httpx.Response(200, json={"docs": []})
    result = OpenLibraryCollector(client=client_for(handler), min_interval_seconds=0,
        max_retry_wait_seconds=2, sleep_fn=waits.append).collect("coloring book")
    assert result.status == "empty" and len(calls) == 2 and waits == [1.0]


def test_429_with_long_retry_after_stops_without_retrying_early():
    calls, waits = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})
    result = OpenLibraryCollector(client=client_for(handler), min_interval_seconds=0,
        max_retry_wait_seconds=2, sleep_fn=waits.append).collect("coloring book")
    assert result.status == "failed" and result.errors[0]["status_code"] == 429
    assert len(calls) == 1 and waits == []


def test_500_retries_once_then_returns_failure():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500)
    result = OpenLibraryCollector(client=client_for(handler), min_interval_seconds=0,
        max_retry_wait_seconds=2, sleep_fn=lambda _: None, random_fn=lambda: 0).collect("coloring book")
    assert result.status == "failed" and result.errors[0]["status_code"] == 500 and len(calls) == 2


def test_timeout_is_reported_without_leaking_request_details():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)
    result = OpenLibraryCollector(client=client_for(handler), min_interval_seconds=0).collect("cozy")
    assert result.status == "failed" and result.errors[0]["kind"] == "timeout"


def test_invalid_json_and_unexpected_shape_are_failed():
    invalid_json = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, content=b"{")), min_interval_seconds=0)
    assert invalid_json.collect("cozy").errors[0]["kind"] == "invalid_json"
    wrong_shape = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"items": []})), min_interval_seconds=0)
    assert wrong_shape.collect("cozy").errors[0]["kind"] == "unexpected_response_shape"


def test_cache_replay_preserves_retrieval_timestamp_and_marks_provenance(tmp_path):
    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"docs": [
        {"key": "/works/OL1W", "title": "Cozy Coloring Book"}]})), min_interval_seconds=0)
    result = collector.collect("cozy coloring book")
    result.retrieved_at = stamp
    for item in result.items + result.relevant_items:
        item.retrieved_at = stamp
    path = tmp_path / "snapshot.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    replay = _replay_snapshot(path)
    assert replay.provenance == "cache_replay"
    assert replay.retrieved_at == stamp and all(x.retrieved_at == stamp for x in replay.items)
    assert all(x.provenance == "cache_replay" for x in replay.items + replay.relevant_items)


def test_limit_is_clamped_and_reusable_function_is_ollama_independent():
    def handler(request):
        assert request.url.params["limit"] == "20"
        return httpx.Response(200, json={"docs": []})
    result = collect_openlibrary("coloring", 99, client=client_for(handler), min_interval_seconds=0)
    assert result.status == "empty"


def test_collector_is_available_through_bounded_project_tool_registry():
    collector = OpenLibraryCollector(client=client_for(lambda request: httpx.Response(200, json={"docs": []})), min_interval_seconds=0)
    registry = openlibrary_tool_registry(collector)
    result = registry.call("openlibrary_search", budget=1, query="coloring")
    assert result.status == "empty" and registry.calls == 1
