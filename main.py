import argparse
import json
import os
from pathlib import Path

import httpx

from market_research.models import ResearchConfig
from market_research.ollama import OllamaClient
from market_research.orchestrator import run_research


def main():
    parser = argparse.ArgumentParser(description="Evidence-first market research agent")
    parser.add_argument("--niche", default="cozy coloring books for adults")
    parser.add_argument("--mode", choices=["live", "replay", "csv_import"], default="live")
    parser.add_argument("--replay", help="Path to a previously captured real evidence JSON snapshot")
    parser.add_argument("--trends-csv", help="Path to a real, manually exported Google Trends CSV (csv_import mode)")
    parser.add_argument("--source-url", help="Actual Google Trends page URL from which the CSV was exported")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--check-ollama", action="store_true", help="Send one real generation request to local Ollama and exit")
    args = parser.parse_args()
    raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    raw.update({"ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "model": os.getenv("OLLAMA_MODEL", raw.get("model", "qwen3:4b-instruct")),
        "request_timeout_seconds": float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15")),
        "ollama_timeout_seconds": float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
        "tool_call_budget": int(os.getenv("TOOL_CALL_BUDGET", "6"))})
    config = ResearchConfig(**raw)
    if args.check_ollama:
        try:
            result = OllamaClient(config.ollama_base_url, config.model, config.ollama_timeout_seconds).diagnose()
            print(json.dumps({"status": "success", **result}, ensure_ascii=False, indent=2))
        except (httpx.HTTPError, ValueError) as exc:
            print(json.dumps({"status": "unavailable", "endpoint": f"{config.ollama_base_url.rstrip('/')}/api/generate",
                "model": config.model, "error": type(exc).__name__}, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        return
    if args.mode == "csv_import" and (not args.trends_csv or not args.source_url):
        parser.error("--mode csv_import requires --trends-csv and --source-url")
    result = run_research(config, args.niche, args.mode, args.replay, args.output_dir,
        args.trends_csv, args.source_url)
    print(json.dumps({"run_id": result.run_id, "mode": result.mode, "evidence_count": len(result.evidence),
        "collection_status": result.metadata["live_collection_status"],
        "analysis_status": result.validation.status, "schema_valid": result.validation.schema_valid,
        "analysis_provenance": result.metadata.get("analysis_provenance"),
        "analysis_execution_status": result.metadata.get("analysis_execution_status"),
        "llm_schema_valid": result.metadata.get("llm_schema_valid"),
        "evidence_sufficient": result.validation.evidence_sufficient,
        "claims_valid": result.validation.claims_valid,
        "unsupported_assertions": result.validation.unsupported_assertions,
        "evidence_records_received_by_ollama": result.metadata["evidence_records_received_by_ollama"],
        "reports": result.metadata["report_paths"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
