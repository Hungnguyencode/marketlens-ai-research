from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, NotRequired, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class ResearchConfig(BaseModel):
    category: str = "Coloring & Activity Books"
    target_region: str = "United States"
    keyword_language: str = "English"
    report_language: str = "Vietnamese"
    time_range: str = "past 12 months"
    model: str = "qwen3:4b-instruct"
    ollama_base_url: str = "http://localhost:11434"
    request_timeout_seconds: float = 15
    ollama_timeout_seconds: float = 120
    tool_call_budget: int = 6
    max_rounds: int = 2


class Evidence(BaseModel):
    evidence_id: str
    run_id: str
    niche: str
    keyword: str
    source_name: str
    source_url: str
    collected_at: datetime
    target_region: str
    observation_period: str
    metric_name: str
    metric_value: float | int | str | None = None
    metric_unit: str | None = None
    source_excerpt: str | None = None
    raw_observation: dict[str, Any] = Field(default_factory=dict)
    extraction_method: str
    collection_status: Literal["success", "partial", "unavailable", "error"]

    @classmethod
    def now(cls) -> datetime:
        return datetime.now(timezone.utc)


class ToolDecision(BaseModel):
    tool_name: str
    keyword: str
    rationale: str


class Plan(BaseModel):
    keyword_candidates: list[str] = Field(min_length=1, max_length=12)
    decisions: list[ToolDecision] = Field(default_factory=list)
    rationale: str


class TrendWindowMetric(TypedDict):
    start_period: str
    end_period: str
    weeks: int
    average_relative_index: float
    evidence_ids: list[str]


class TrendPointMetric(TypedDict):
    week: str
    relative_index: float
    evidence_id: str
    source_url: str


class ComputedTrendMetric(TypedDict, total=False):
    metric_id: Required[str]
    keyword: Required[str]
    status: Required[Literal["computed", "insufficient_data"]]
    reason: NotRequired[str]
    completed_observation_count: NotRequired[int]
    latest_completed_week: NotRequired[str | None]
    recent_window: NotRequired[TrendWindowMetric | None]
    previous_window: NotRequired[TrendWindowMetric | None]
    change_in_index_points: NotRequired[float | None]
    direction: NotRequired[Literal["increasing", "stable", "decreasing", "insufficient_data"]]
    peak: NotRequired[TrendPointMetric | None]
    seasonality_note: NotRequired[str]
    evidence_ids: NotRequired[list[str]]
    comparison_evidence_ids: NotRequired[list[str]]
    recent_evidence: NotRequired[list[TrendPointMetric]]


class VerifiedFact(BaseModel):
    fact_id: str
    keyword: str
    metric_name: str
    value: float | int | str | None
    unit: str | None
    observation_period: str
    evidence_ids: list[str] = Field(default_factory=list)
    metric_ids: list[str] = Field(default_factory=list)
    coverage_count: int | None = None
    description: str


class GroundedClaim(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


FactAlias = Literal["previous_four_week_average", "recent_four_week_average", "four_week_change",
    "four_week_direction", "observed_peak", "weekly_observation"]


class FactSelection(BaseModel):
    """A semantic, run-independent selection; canonical fact IDs are resolved by Python."""
    alias: FactAlias
    keyword: str
    observation_period: str


class AliasGroundedClaim(BaseModel):
    text: str
    fact_selections: list[FactSelection] = Field(default_factory=list)


class ProductConceptDraft(BaseModel):
    what_to_sell: str = Field(min_length=8)
    intended_customer_hypothesis: str = Field(min_length=5)
    differentiation_rationale: str = Field(min_length=5)
    uncertainties: list[str] = Field(default_factory=list)


class AnalysisGenerationDraft(BaseModel):
    """Ollama-facing contract: semantic fact aliases only, with Python-owned market fields."""
    model_config = ConfigDict(extra="forbid")

    main_keywords: list[str]
    related_keywords: list[str] = Field(default_factory=list)
    keyword_research_candidates: list[str] = Field(default_factory=list)
    evidence_based_inferences: list[AliasGroundedClaim] = Field(default_factory=list)
    hypotheses: list[AliasGroundedClaim] = Field(default_factory=list)
    trend_direction: Literal["increasing", "stable", "decreasing", "insufficient evidence"]
    trend_reasons: list[AliasGroundedClaim] = Field(default_factory=list)
    product_ideas: list[ProductConceptDraft] = Field(min_length=2, max_length=3)


class ProductIdea(BaseModel):
    what_to_sell: str
    target_customer: str
    why_now: GroundedClaim
    supporting_fact_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_status: Literal["evidence_backed", "hypothesis"] = "hypothesis"
    differentiation: str = ""


class AnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    main_keywords: list[str]
    related_keywords: list[str]
    keyword_research_candidates: list[str] = Field(default_factory=list)
    evidence_based_inferences: list[GroundedClaim]
    # Structured hypotheses can cite observed facts while keeping causal explanations speculative.
    hypotheses: list[GroundedClaim | str]
    trend_direction: Literal["increasing", "stable", "decreasing", "insufficient evidence"]
    # Attached by Python from the computed metric; the LLM cannot mint fact IDs.
    trend_direction_fact_ids: list[str] = Field(default_factory=list)
    trend_reasons: list[GroundedClaim]
    target_customers: list[str]
    customer_needs_or_complaints: list[str]
    competition_assessment: str
    indicative_price_range: str | None
    product_ideas: list[ProductIdea] = Field(min_length=2, max_length=3)
    confidence: Literal["low", "medium", "high"]
    missing_or_unavailable_data: list[str]


class MarketAnalysis(AnalysisDraft):
    # Generated exclusively by Python from VerifiedFact records, never by Ollama.
    factual_observations: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    valid: bool
    status: Literal["VALID", "DEGRADED", "INSUFFICIENT_EVIDENCE", "BLOCKED", "INVALID"]
    schema_valid: bool
    claims_valid: bool
    evidence_sufficient: bool
    unsupported_assertions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResearchResult(BaseModel):
    run_id: str
    niche: str
    category: str
    target_region: str
    report_language: str
    mode: Literal["live", "replay", "csv_import"]
    started_at: datetime
    finished_at: datetime
    evidence: list[Evidence]
    analysis: MarketAnalysis
    validation: ValidationResult
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
