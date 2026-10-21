"""Closed single-niche job requests and artifact contracts; no execution on import."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import ResearchResult
from .trends_csv import GoogleTrendsCsvCollector


WORKSPACE = Path(__file__).resolve().parents[1]
RESEARCH_STEPS = 8
RESEARCH_PHASES = {"INPUT_VALIDATION", "PLANNING", "COLLECTION", "EVIDENCE_PREPARATION",
                   "MODEL_ANALYSIS", "DETERMINISTIC_ANALYSIS", "CLAIM_VALIDATION",
                   "REPORT_GENERATION", "ARTIFACT_VERIFICATION", "READY_TO_PUBLISH", "PUBLICATION"}
SECRET_PATTERN = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|password|secret|access[_-]?token)[\"']?\s*[:=]|"
    r"\bAIza[\w-]{20,}|\bsk-[\w-]{16,}|\bgh[pousr]_[\w]{20,})"
)


class ResearchSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    topic: str = Field(min_length=2, max_length=200)
    mode: Literal["csv_import", "replay", "live"]
    input_path: str | None = None
    source_url: str | None = None
    allow_network: bool = False
    allow_inference: bool = False
    model: str = "qwen3:4b-instruct"
    target_region: Literal["United States"] = "United States"
    request_timeout_seconds: int = Field(default=15, ge=1, le=120)
    inference_timeout_seconds: int = Field(default=120, ge=1, le=600)

    @field_validator("topic")
    @classmethod
    def safe_topic(cls, value):
        if value != value.strip() or re.search(r"[/\\:<>|?*\x00-\x1f]", value) or SECRET_PATTERN.search(value):
            raise ValueError("Topic must be plain text without paths, control characters or credentials.")
        return value

    @field_validator("model")
    @classmethod
    def safe_model(cls, value):
        if not re.fullmatch(r"[\w][\w.:/-]{0,99}", value, flags=re.ASCII) or SECRET_PATTERN.search(value):
            raise ValueError("Use an Ollama model identifier, never credentials or executable options.")
        return value

    @model_validator(mode="after")
    def explicit_inputs(self):
        if self.mode == "live":
            if not self.allow_network or self.input_path is not None or self.source_url is not None:
                raise ValueError("live requires allow_network=true and no local input or CSV source URL.")
        elif self.allow_network or not self.input_path:
            raise ValueError("Local modes require an input file and allow_network=false.")
        if self.mode == "csv_import":
            if not self.source_url:
                raise ValueError("csv_import requires the actual Google Trends export page URL.")
            try:
                GoogleTrendsCsvCollector("unused.csv", self.source_url)._validate_source_url()
                url = urlparse(self.source_url)
                query = parse_qs(url.query)
                if (url.username or url.password or url.fragment or url.port not in (None, 443)
                        or query.get("geo") != ["US"] or len(query.get("q", [])) != 1
                        or not query["q"][0].strip() or "," in query["q"][0]
                        or any(k.lower() not in {"q", "geo", "date", "cat", "gprop", "hl", "tz"}
                               for k, _ in parse_qsl(url.query)) or SECRET_PATTERN.search(self.source_url)
                        or any(SECRET_PATTERN.search(value) for _, value in parse_qsl(url.query))):
                    raise ValueError("Unsafe URL")
            except ValueError:
                raise ValueError("Use the actual credential-free HTTPS Google Trends URL with geo=US and known export options.") from None
        elif self.source_url is not None:
            raise ValueError("Only csv_import accepts source_url.")
        return self


class ResearchRequest(ResearchSubmission):
    input_sha256: str | None

    @model_validator(mode="after")
    def pinned_input(self):
        if self.mode == "live":
            if self.input_sha256 is not None:
                raise ValueError("Live requests cannot declare a local input checksum.")
        elif not self.input_sha256 or not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256):
            raise ValueError("Local research inputs require a pinned SHA-256 checksum.")
        return self


def resolve_input(root: Path, name: str, mode: str) -> Path:
    """Operator-selected root is trusted; job input cannot escape it or use aliases."""
    path = Path(name)
    win = PureWindowsPath(name)
    if (not name or name != name.strip() or "\x00" in name or name.startswith(("\\\\", "//"))
            or ".." in path.parts or ".." in win.parts or (win.drive and not win.is_absolute())
            or ":" in name.removeprefix(win.drive) or SECRET_PATTERN.search(name)):
        raise ValueError("Unsafe or ambiguous input path; use a local file within the configured input root.")
    candidate = path if path.is_absolute() else root / path
    if not candidate.is_relative_to(root):
        raise ValueError("Input must be within the configured input root.")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."} or part != part.rstrip(" ."):
            raise ValueError("Unsafe input path component.")
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Input symlinks and junctions are not accepted.")
    resolved = candidate.resolve(strict=True)
    suffix = ".csv" if mode == "csv_import" else ".json"
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix.lower() != suffix:
        raise ValueError("Input must be an existing local CSV or replay JSON of the expected type.")
    if resolved.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Local input exceeds the 32 MiB job input limit.")
    return resolved


def prepare_request(payload: dict, root: Path) -> ResearchRequest:
    submission = ResearchSubmission.model_validate(payload)
    data = submission.model_dump()
    checksum = None
    if submission.mode != "live":
        path = resolve_input(root, submission.input_path, submission.mode)
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        data["input_path"] = path.relative_to(root).as_posix()
    return ResearchRequest(**data, input_sha256=checksum)


class ResearchOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    validation_status: Literal["VALID", "DEGRADED", "INSUFFICIENT_EVIDENCE", "BLOCKED", "INVALID"]
    valid: bool
    schema_valid: bool
    claims_valid: bool
    evidence_sufficient: bool
    evidence_count: int
    analysis_provenance: str
    model_execution_status: str
    requested_model: str

    @classmethod
    def from_result(cls, result: ResearchResult, model: str):
        return cls(run_id=result.run_id, validation_status=result.validation.status,
                   valid=result.validation.valid, schema_valid=result.validation.schema_valid,
                   claims_valid=result.validation.claims_valid, evidence_sufficient=result.validation.evidence_sufficient,
                   evidence_count=len(result.evidence), analysis_provenance=result.metadata["analysis_provenance"],
                   model_execution_status=result.metadata["analysis_execution_status"], requested_model=model)


class PreparedResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_path: Literal["_partial/manifest.json"] = "_partial/manifest.json"
    outcome: ResearchOutcome


class ResearchFailed(Exception):
    def __init__(self, code: str, outcome: ResearchOutcome | None = None):
        super().__init__("Research operation failed; sensitive details are withheld.")
        self.code, self.outcome = code, outcome


def verify_bundle(job_directory: Path, job_id: str, request: ResearchRequest, outcome: ResearchOutcome,
                  *, directory_name: Literal["_partial", "result"] = "_partial") -> None:
    """Verify every staged reference before the worker publishes it under its transaction."""
    if directory_name not in {"_partial", "result"}:
        raise ValueError("Only staged or published research bundles can be verified.")
    stage = job_directory / directory_name
    if stage.is_symlink() or not stage.resolve().is_relative_to(job_directory):
        raise ValueError("Unsafe staging directory.")
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["job_id"] != job_id or manifest["request"] != request.model_dump(mode="json")
            or manifest["outcome"] != outcome.model_dump(mode="json")):
        raise ValueError("Research manifest does not match the submitted request and outcome.")
    artifacts = manifest["artifacts"]
    required = {"json", "markdown", "html", "snapshot", "log"}
    if request.mode != "live":
        required.add("input")
    if set(artifacts) != required:
        raise ValueError("Research artifact bundle is incomplete.")
    for entry in artifacts.values():
        name = entry["path"]
        relative = Path(name)
        path = stage / relative
        if (relative.is_absolute() or relative.drive or ":" in name or "\\" in name or ".." in relative.parts
                or path.is_symlink() or not path.resolve().is_relative_to(stage) or not path.is_file()
                or path.stat().st_size == 0 or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]):
            raise ValueError("Research artifact checksum or path is invalid.")
    result = ResearchResult.model_validate_json((stage / artifacts["json"]["path"]).read_text(encoding="utf-8"))
    if (ResearchOutcome.from_result(result, request.model) != outcome or result.metadata.get("job_id") != job_id
            or result.metadata.get("research_request") != request.model_dump(mode="json")):
        raise ValueError("Research result does not match its job manifest.")
    if request.mode != "live" and artifacts["input"]["sha256"] != request.input_sha256:
        raise ValueError("Research source input checksum changed.")
