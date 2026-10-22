import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .analysis import (attach_python_direction_reference, build_verified_facts,
    compute_trend_metrics, fallback_analysis, normalize_analysis_keywords)
from .collectors import GoogleBooksCollector
from .logging import ExecutionLogger
from .models import AnalysisDraft, MarketAnalysis, ResearchConfig, ResearchResult
from .ollama import OllamaClient
from .planner import AgentPlanner
from .reports import write_reports
from .store import EvidenceStore
from .tools import ToolRegistry
from .validator import validate_analysis
from .trends_csv import (GoogleTrendsCsvCollector, SYNTHETIC_SOURCE, SYNTHETIC_NOTICE,
                         is_synthetic_evidence)


@dataclass(frozen=True)
class ResearchExecutionContext:
    """Optional host adapter; legacy callers retain their original paths and behavior."""
    root: Path
    allow_network: bool
    allow_inference: bool
    checkpoint: Callable[[], None]
    progress: Callable[[str, int], None]
    metadata: dict = field(default_factory=dict)


def run_research(config: ResearchConfig, niche: str, mode="live", replay_path: str | None = None,
                 output_dir="outputs", trends_csv_path: str | None = None, source_url: str | None = None,
                 *, execution: ResearchExecutionContext | None = None):
    check = execution.checkpoint if execution else lambda: None
    def progress(phase, completed):
        check()
        if execution:
            execution.progress(phase, completed)

    def diagnostic(exc):
        details = exc.diagnostic() if hasattr(exc, "diagnostic") else {"message": str(exc)}
        if not execution:
            return details
        return {"source": getattr(collector, "source_name", "Google Books API"), "status_code": details.get("status_code")
                if type(details.get("status_code")) is int else None,
                "reason": "quota_or_permission_denied" if details.get("status_code") == 403 else
                          "rate_limited" if details.get("status_code") == 429 else "source_operation_failed",
                "attempts": details.get("attempts") if type(details.get("attempts")) is int else None,
                "retry_after_seconds": details.get("retry_after_seconds")
                if type(details.get("retry_after_seconds")) in (int, float) else None,
                "message": "Source operation failed; sensitive details withheld."}

    check()
    if execution:
        output_dir = execution.root / "reports"
    if execution and mode == "live" and not execution.allow_network:
        raise ValueError("Live collection was not authorized.")
    if mode == "csv_import" and (not trends_csv_path or not source_url):
        raise ValueError("csv_import mode requires both trends_csv_path and the actual Google Trends source_url")
    run_id = uuid4().hex[:12]
    started = datetime.now(timezone.utc); tick = time.perf_counter()
    snap_path = (execution.root if execution else Path(".")) / "snapshots" / f"{run_id}.json"
    store = EvidenceStore(replay_path or snap_path)
    logger = ExecutionLogger((execution.root if execution else Path(".")) / "logs" / f"{run_id}.jsonl", run_id)
    progress("PLANNING", 1)
    keywords = [niche]
    if mode == "csv_import":
        collector = GoogleTrendsCsvCollector(trends_csv_path, source_url)
    else:
        collector = GoogleBooksCollector(config.request_timeout_seconds, os.getenv("GOOGLE_BOOKS_API_KEY"),
                                         **({"checkpoint": check} if execution else {}))
    registry = ToolRegistry({collector.name: collector.collect})
    inference_allowed = (execution is None or execution.allow_inference) and source_url != SYNTHETIC_SOURCE
    ollama = (OllamaClient(config.ollama_base_url, config.model, config.ollama_timeout_seconds,
                          **({"checkpoint": check} if execution else {})) if inference_allowed else None)
    planner = AgentPlanner(registry.available_tools, llm=ollama.plan if mode == "live" and ollama else None)
    plan_started = time.perf_counter()
    plan, decision_source = planner.plan(niche, keywords)
    logger.step("plan_research", "AgentPlanner", "fallback" if decision_source.startswith("deterministic_fallback") else "success",
        decision_source, duration=time.perf_counter()-plan_started)
    progress("COLLECTION", 2)
    if mode == "replay":
        evidence = store.load()
        check()
        if execution:
            store = EvidenceStore(snap_path)
            store.save(evidence)
        collection_errors = []
        logger.step("load_replay", "EvidenceStore", "success", f"loaded {len(evidence)} saved records")
    else:
        evidence = []
        collection_errors = []
        collected_keywords = set()
        for decision in plan.decisions[:config.max_rounds]:
            check()
            if decision.keyword.casefold() in collected_keywords:
                logger.step("collect_evidence", decision.tool_name, "skipped", "duplicate keyword request suppressed")
                continue
            collected_keywords.add(decision.keyword.casefold())
            step_started = time.perf_counter()
            try:
                rows = registry.call(decision.tool_name, budget=config.tool_call_budget, run_id=run_id, niche=niche,
                    keyword=decision.keyword, target_region=config.target_region)
                check()
                evidence.extend(rows)
                logger.step("collect_evidence", decision.tool_name, "success", f"collected {len(rows)} records", duration=time.perf_counter()-step_started)
            except Exception as exc:
                check()
                details = diagnostic(exc)
                collection_errors.append(details)
                logger.step("collect_evidence", decision.tool_name, "error", "collector failed",
                    str(details), time.perf_counter()-step_started)
        check()
        store = EvidenceStore(snap_path); store.save(evidence)
    synthetic_flags = [is_synthetic_evidence(item) for item in evidence]
    synthetic = source_url == SYNTHETIC_SOURCE or any(synthetic_flags)
    if synthetic:
        if not all(synthetic_flags) or any(item.source_url != SYNTHETIC_SOURCE
                or item.metric_name != "synthetic_demo_index" or not item.source_name.startswith("SYNTHETIC")
                for item in evidence):
            raise ValueError("SYNTHETIC demo provenance is inconsistent or mixed with genuine research evidence.")
        inference_allowed = False
        ollama = None
        logger.step("data_provenance", "EvidenceStore", "success", SYNTHETIC_NOTICE)
    quota_403 = collection_errors and all(e.get("status_code") == 403 and
        any(term in (str(e.get("reason", "")) + " " + str(e.get("message", ""))).lower()
            for term in ("quota", "limit", "rate")) for e in collection_errors)
    if mode == "live":
        collection_status = ("completed" if evidence else "rate_limited" if collection_errors and
            all(e.get("status_code") == 429 for e in collection_errors) else
            "quota_or_permission_denied" if quota_403 else "blocked")
    elif mode == "csv_import":
        collection_status = "completed" if evidence else "blocked"
    else:
        collection_status = "replay"

    if execution and mode != "live":
        if len({item.keyword.casefold().strip() for item in evidence}) > 1 or len({item.niche for item in evidence}) > 1:
            raise ValueError("A single-niche research job requires one local keyword series and niche.")
        if mode == "csv_import" and evidence and source_url != SYNTHETIC_SOURCE:
            declared = parse_qs(urlparse(source_url).query).get("q", [])
            normalize = GoogleTrendsCsvCollector._normalized_header
            if len(declared) != 1 or normalize(declared[0]) != normalize(evidence[0].keyword):
                raise ValueError("CSV keyword does not match its declared Google Trends source URL.")
        aliases = {"us", "united states", "hoa kỳ"}
        if any(item.target_region.casefold() not in aliases or
               (item.raw_observation.get("geographic_metadata") and
                GoogleTrendsCsvCollector._normalized_header(item.raw_observation["geographic_metadata"]) not in aliases)
               for item in evidence):
            raise ValueError("Local evidence must match the requested United States scope.")
    progress("EVIDENCE_PREPARATION", 3)
    analysis_started = time.perf_counter()
    ollama_received_evidence = False
    analysis_provenance = "deterministic_fallback"
    analysis_execution_status = "skipped"
    analysis_error = None
    computed_metrics = compute_trend_metrics([e.model_dump(mode="json") for e in evidence])
    verified_facts = build_verified_facts([e.model_dump(mode="json") for e in evidence], computed_metrics)
    progress("MODEL_ANALYSIS" if evidence and inference_allowed else "DETERMINISTIC_ANALYSIS", 4)
    if not evidence:
        analysis = fallback_analysis(niche, [e.model_dump(mode="json") for e in evidence], verified_facts)
        logger.step("analyze_evidence", "OllamaClient", "skipped", "no evidence; no analysis request sent to Ollama",
            duration=time.perf_counter()-analysis_started)
    elif not inference_allowed:
        analysis = fallback_analysis(niche, [e.model_dump(mode="json") for e in evidence], verified_facts)
        analysis_execution_status = "not_requested"
        logger.step("analyze_evidence", "OllamaClient", "skipped", "inference not authorized; deterministic fallback only")
    else:
        try:
            draft = ollama.analyze(niche, [e.model_dump(mode="json") for e in evidence],
                config.report_language, computed_metrics=computed_metrics, verified_facts=verified_facts)
            check()
            if isinstance(draft, MarketAnalysis):
                draft_data = draft.model_dump(exclude={"factual_observations"})
            elif isinstance(draft, AnalysisDraft):
                draft_data = draft.model_dump()
            else:
                draft_data = AnalysisDraft.model_validate(draft).model_dump()
            analysis = MarketAnalysis(**draft_data, factual_observations=[fact.description for fact in verified_facts])
            ollama_received_evidence = True
            analysis_provenance = "ollama"
            analysis_execution_status = "success"
            logger.step("analyze_evidence", "OllamaClient", "success", f"model={config.model}; evidence_count={len(evidence)}",
                duration=time.perf_counter()-analysis_started)
        except Exception as exc:
            check()
            ollama_received_evidence = ollama.last_generation_received
            analysis = fallback_analysis(niche, [e.model_dump(mode="json") for e in evidence], verified_facts)
            analysis_provenance = "deterministic_fallback"
            analysis_execution_status = "failed"
            analysis_error = "Ollama operation failed; sensitive details withheld." if execution else f"{type(exc).__name__}: {exc}"
            logger.step("analyze_evidence", "OllamaClient", "unavailable", "deterministic low-confidence fallback",
                analysis_error, time.perf_counter()-analysis_started)
    if synthetic:
        analysis.factual_observations = [fact.description for fact in verified_facts]
    progress("CLAIM_VALIDATION", 5)
    evidence_dicts = [e.model_dump(mode="json") for e in evidence]
    normalize_analysis_keywords(analysis, evidence_dicts)
    attach_python_direction_reference(analysis, verified_facts)
    validation_started = time.perf_counter()
    validation = validate_analysis(analysis, [e.model_dump(mode="json") for e in evidence], collection_status,
        analysis_provenance=analysis_provenance, verified_facts=verified_facts)
    analysis_recovery = {"attempted": False}
    if analysis_provenance == "ollama" and not validation.claims_valid \
            and getattr(ollama, "last_analysis_request_count", 1) < 2:
        original_draft = analysis.model_dump(exclude={"factual_observations"})
        initial_diagnostics = list(validation.unsupported_assertions)
        analysis_recovery = {"attempted": True, "correction_provenance": "single validator-guided Ollama pass",
            "original_draft": original_draft, "initial_diagnostics": initial_diagnostics}
        try:
            corrected_draft = ollama.analyze(niche, [e.model_dump(mode="json") for e in evidence],
                config.report_language, computed_metrics=computed_metrics, verified_facts=verified_facts,
                correction_diagnostics=initial_diagnostics)
            check()
            corrected_data = corrected_draft.model_dump()
            corrected_analysis = MarketAnalysis(**corrected_data,
                factual_observations=[fact.description for fact in verified_facts])
            normalize_analysis_keywords(corrected_analysis, evidence_dicts)
            attach_python_direction_reference(corrected_analysis, verified_facts)
            corrected_validation = validate_analysis(corrected_analysis, evidence_dicts, collection_status,
                analysis_provenance="ollama", verified_facts=verified_facts)
            analysis = corrected_analysis
            validation = corrected_validation
            analysis_recovery.update({"result": "accepted" if validation.claims_valid else "still_invalid",
                "final_diagnostics": list(validation.unsupported_assertions),
                "corrected_draft": analysis.model_dump(exclude={"factual_observations"})})
            logger.step("correct_analysis", "OllamaClient", "success" if validation.claims_valid else "invalid",
                f"one correction pass; final_status={validation.status}; diagnostics={len(validation.unsupported_assertions)}",
                duration=time.perf_counter()-validation_started)
        except Exception as exc:
            check()
            correction_error = "Model correction failed; sensitive details withheld." if execution else f"{type(exc).__name__}: {exc}"
            analysis_recovery.update({"result": "correction_failed", "error": correction_error,
                "final_diagnostics": initial_diagnostics})
            logger.step("correct_analysis", "OllamaClient", "error", "one correction pass failed; original invalid draft retained",
                correction_error, duration=time.perf_counter()-validation_started)
    elif analysis_provenance == "ollama" and not validation.claims_valid:
        analysis_recovery = {"attempted": False, "result": "bounded_generation_repair_budget_used",
            "initial_diagnostics": list(validation.unsupported_assertions),
            "final_diagnostics": list(validation.unsupported_assertions)}
    logger.step("validate_analysis", "EvidenceValidator", validation.status.lower(),
        f"status={validation.status}; schema_valid={validation.schema_valid}; evidence_sufficient={validation.evidence_sufficient}",
        duration=time.perf_counter()-validation_started)
    if synthetic:
        validation.warnings.append(SYNTHETIC_NOTICE)
    finished = datetime.now(timezone.utc)
    result = ResearchResult(run_id=run_id, niche=niche, category=config.category, target_region=config.target_region,
        report_language=config.report_language, mode=mode, started_at=started, finished_at=finished, evidence=evidence,
        analysis=analysis, validation=validation, verified_facts=verified_facts,
        metadata={"planner_decision_source": decision_source,
        "tool_calls": registry.calls, "tool_call_budget": config.tool_call_budget,
        "execution_seconds": round(time.perf_counter()-tick, 3), "estimated_api_cost_usd": 0,
        "snapshot_path": str(store.path), "collection_errors": collection_errors,
        "live_collection_status": collection_status,
        "analysis_provenance": analysis_provenance,
        "analysis_execution_status": analysis_execution_status,
        "llm_schema_valid": analysis_provenance == "ollama",
        "analysis_error": analysis_error,
        "analysis_recovery": analysis_recovery,
        "measured_keywords": list(dict.fromkeys(e.keyword for e in evidence)),
        "computed_metrics": computed_metrics,
        "verified_fact_count": len(verified_facts),
        "evidence_records_received_by_ollama": len(evidence) if ollama_received_evidence else 0,
        "collection_method": "manual_csv_import" if mode == "csv_import" else mode})
    if execution:
        result.metadata.update(execution.metadata)
        result.metadata["snapshot_path"] = snap_path.relative_to(execution.root).as_posix()
    if synthetic:
        result.metadata.update(data_provenance="SYNTHETIC", collection_method="synthetic_demo_csv")
    progress("REPORT_GENERATION", 6)
    report_started = time.perf_counter()
    paths = write_reports(result, output_dir)
    published_paths = ({key: Path(value).relative_to(execution.root).as_posix() for key, value in paths.items()}
                       if execution else paths)
    logger.step("generate_report", "ReportGenerator", "success", ", ".join(published_paths.values()), duration=time.perf_counter()-report_started)
    result.metadata["report_paths"] = published_paths
    # Persist final metadata/result with report paths.
    Path(paths["json"]).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    progress("ARTIFACT_VERIFICATION", 7)
    return result
