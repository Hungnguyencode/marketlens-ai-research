import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
from .trends_csv import GoogleTrendsCsvCollector


def run_research(config: ResearchConfig, niche: str, mode="live", replay_path: str | None = None,
                 output_dir="outputs", trends_csv_path: str | None = None, source_url: str | None = None):
    if mode == "csv_import" and (not trends_csv_path or not source_url):
        raise ValueError("csv_import mode requires both trends_csv_path and the actual Google Trends source_url")
    run_id = uuid4().hex[:12]
    started = datetime.now(timezone.utc); tick = time.perf_counter()
    snap_path = Path("snapshots") / f"{run_id}.json"
    store = EvidenceStore(replay_path or snap_path)
    logger = ExecutionLogger(Path("logs") / f"{run_id}.jsonl", run_id)
    keywords = [niche]
    if mode == "csv_import":
        collector = GoogleTrendsCsvCollector(trends_csv_path, source_url)
    else:
        collector = GoogleBooksCollector(config.request_timeout_seconds, os.getenv("GOOGLE_BOOKS_API_KEY"))
    registry = ToolRegistry({collector.name: collector.collect})
    ollama = OllamaClient(config.ollama_base_url, config.model, config.ollama_timeout_seconds)
    planner = AgentPlanner(registry.available_tools, llm=ollama.plan if mode == "live" else None)
    plan_started = time.perf_counter()
    plan, decision_source = planner.plan(niche, keywords)
    logger.step("plan_research", "AgentPlanner", "fallback" if decision_source.startswith("deterministic_fallback") else "success",
        decision_source, duration=time.perf_counter()-plan_started)
    if mode == "replay":
        evidence = store.load()
        collection_errors = []
        logger.step("load_replay", "EvidenceStore", "success", f"loaded {len(evidence)} saved records")
    else:
        evidence = []
        collection_errors = []
        collected_keywords = set()
        for decision in plan.decisions[:config.max_rounds]:
            if decision.keyword.casefold() in collected_keywords:
                logger.step("collect_evidence", decision.tool_name, "skipped", "duplicate keyword request suppressed")
                continue
            collected_keywords.add(decision.keyword.casefold())
            step_started = time.perf_counter()
            try:
                rows = registry.call(decision.tool_name, budget=config.tool_call_budget, run_id=run_id, niche=niche,
                    keyword=decision.keyword, target_region=config.target_region)
                evidence.extend(rows)
                logger.step("collect_evidence", decision.tool_name, "success", f"collected {len(rows)} records", duration=time.perf_counter()-step_started)
            except Exception as exc:
                diagnostic = exc.diagnostic() if hasattr(exc, "diagnostic") else {"message": str(exc)}
                collection_errors.append(diagnostic)
                logger.step("collect_evidence", decision.tool_name, "error", "collector failed",
                    str(diagnostic), time.perf_counter()-step_started)
        store = EvidenceStore(snap_path); store.save(evidence)
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

    analysis_started = time.perf_counter()
    ollama_received_evidence = False
    analysis_provenance = "deterministic_fallback"
    analysis_execution_status = "skipped"
    analysis_error = None
    computed_metrics = compute_trend_metrics([e.model_dump(mode="json") for e in evidence])
    verified_facts = build_verified_facts([e.model_dump(mode="json") for e in evidence], computed_metrics)
    if not evidence:
        analysis = fallback_analysis(niche, [e.model_dump(mode="json") for e in evidence], verified_facts)
        logger.step("analyze_evidence", "OllamaClient", "skipped", "no evidence; no analysis request sent to Ollama",
            duration=time.perf_counter()-analysis_started)
    else:
        try:
            draft = ollama.analyze(niche, [e.model_dump(mode="json") for e in evidence],
                config.report_language, computed_metrics=computed_metrics, verified_facts=verified_facts)
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
            ollama_received_evidence = ollama.last_generation_received
            analysis = fallback_analysis(niche, [e.model_dump(mode="json") for e in evidence], verified_facts)
            analysis_provenance = "deterministic_fallback"
            analysis_execution_status = "failed"
            analysis_error = f"{type(exc).__name__}: {exc}"
            logger.step("analyze_evidence", "OllamaClient", "unavailable", "deterministic low-confidence fallback",
                str(exc), time.perf_counter()-analysis_started)
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
            analysis_recovery.update({"result": "correction_failed", "error": f"{type(exc).__name__}: {exc}",
                "final_diagnostics": initial_diagnostics})
            logger.step("correct_analysis", "OllamaClient", "error", "one correction pass failed; original invalid draft retained",
                f"{type(exc).__name__}: {exc}", duration=time.perf_counter()-validation_started)
    elif analysis_provenance == "ollama" and not validation.claims_valid:
        analysis_recovery = {"attempted": False, "result": "bounded_generation_repair_budget_used",
            "initial_diagnostics": list(validation.unsupported_assertions),
            "final_diagnostics": list(validation.unsupported_assertions)}
    logger.step("validate_analysis", "EvidenceValidator", validation.status.lower(),
        f"status={validation.status}; schema_valid={validation.schema_valid}; evidence_sufficient={validation.evidence_sufficient}",
        duration=time.perf_counter()-validation_started)
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
    report_started = time.perf_counter()
    paths = write_reports(result, output_dir)
    logger.step("generate_report", "ReportGenerator", "success", ", ".join(paths.values()), duration=time.perf_counter()-report_started)
    result.metadata["report_paths"] = paths
    # Persist final metadata/result with report paths.
    Path(paths["json"]).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
