"""Adapt the production single-niche pipeline; never duplicate analysis or validation rules."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from .models import Evidence, ResearchConfig
from .orchestrator import ResearchExecutionContext, run_research
from .research_contract import (SECRET_PATTERN, PreparedResearchResult, ResearchFailed,
                                ResearchOutcome, ResearchRequest, resolve_input, verify_bundle)


def _local_ollama_endpoint() -> str:
    endpoint = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ResearchFailed("RESEARCH_CONFIGURATION_FAILED")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid port")
    except ValueError:
        raise ResearchFailed("RESEARCH_CONFIGURATION_FAILED") from None
    return endpoint.rstrip("/")


def research_handler(request: ResearchRequest, context) -> PreparedResearchResult:
    context.progress("INPUT_VALIDATION", 0)
    stage = context.output_dir / "_partial"
    stage.mkdir()
    source = None
    if request.mode != "live":
        try:
            original = resolve_input(context.store.input_root, request.input_path, request.mode)
            raw = original.read_bytes()
            if hashlib.sha256(raw).hexdigest() != request.input_sha256:
                raise ValueError("Source changed since submission")
            text = raw.decode("utf-8-sig")
            key = os.getenv("GOOGLE_BOOKS_API_KEY", "")
            if SECRET_PATTERN.search(text) or (len(key) >= 8 and key in text):
                raise ValueError("Credentials must not be included in research inputs")
            if request.mode == "replay":
                for row in json.loads(text):
                    evidence = Evidence.model_validate(row)
                    parsed = urlparse(evidence.source_url)
                    if parsed.username or parsed.password or any(
                            name.lower() in {"key", "api_key", "token", "access_token", "password"}
                            for name, _ in parse_qsl(parsed.query)):
                        raise ValueError("Replay source URLs must not include credentials")
            inputs = stage / "inputs"
            inputs.mkdir()
            source = inputs / original.name
            with source.open("xb") as handle:
                handle.write(raw)
        except (OSError, ValueError, TypeError):
            raise ResearchFailed("RESEARCH_INPUT_FAILED") from None
    context.checkpoint()
    config = ResearchConfig(model=request.model, target_region=request.target_region,
                            request_timeout_seconds=request.request_timeout_seconds,
                            ollama_timeout_seconds=request.inference_timeout_seconds,
                            ollama_base_url=_local_ollama_endpoint() if request.allow_inference else "http://localhost:11434")
    result = run_research(
        config, request.topic, mode=request.mode,
        replay_path=str(source) if request.mode == "replay" else None,
        trends_csv_path=str(source) if request.mode == "csv_import" else None,
        source_url=request.source_url,
        execution=ResearchExecutionContext(
            root=stage, allow_network=request.allow_network, allow_inference=request.allow_inference,
            checkpoint=context.checkpoint, progress=context.progress,
            metadata={"job_id": context.job_id, "research_request": request.model_dump(mode="json"),
                      "requested_model": request.model, "input_sha256": request.input_sha256}))
    context.checkpoint()
    outcome = ResearchOutcome.from_result(result, request.model)
    if result.metadata["collection_errors"]:
        raise ResearchFailed("RESEARCH_COLLECTION_FAILED", outcome)
    if request.allow_inference and result.metadata["analysis_execution_status"] == "failed":
        raise ResearchFailed("RESEARCH_INFERENCE_FAILED", outcome)
    paths = {**result.metadata["report_paths"], "snapshot": result.metadata["snapshot_path"],
             "log": f"logs/{result.run_id}.jsonl"}
    if source is not None:
        paths["input"] = source.relative_to(stage).as_posix()
    manifest = {"artifact_kind": "RESEARCH_WORKFLOW_OUTPUT", "job_id": context.job_id,
                "request": request.model_dump(mode="json"), "outcome": outcome.model_dump(mode="json"),
                "artifacts": {key: {"path": name, "sha256": hashlib.sha256((stage / name).read_bytes()).hexdigest()}
                              for key, name in paths.items()}}
    temporary = stage / "manifest.json.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    temporary.rename(stage / "manifest.json")
    verify_bundle(context.output_dir, context.job_id, request, outcome)
    context.progress("READY_TO_PUBLISH", 8)
    return PreparedResearchResult(outcome=outcome)
