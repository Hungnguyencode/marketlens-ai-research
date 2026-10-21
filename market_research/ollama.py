import json
import re

import httpx
from pydantic import ValidationError

from .analysis import fact_selection_options, materialize_analysis
from .models import AnalysisDraft, AnalysisGenerationDraft, Plan


class OllamaClient:
    def __init__(self, base_url="http://localhost:11434", model="qwen3:4b-instruct", timeout=90, client=None):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.client = client
        self.last_generation_received = False
        self.last_analysis_request_count = 0

    @staticmethod
    def parse_analysis(text: str) -> AnalysisDraft:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            return AnalysisDraft.model_validate_json(cleaned)
        except (ValidationError, ValueError):
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("Ollama response did not contain a valid JSON object")
            return AnalysisDraft.model_validate(json.loads(cleaned[start:end + 1]))

    def analyze(self, niche: str, evidence: list[dict], report_language="Vietnamese", computed_metrics=None,
                verified_facts=None, correction_diagnostics: list[str] | None = None) -> AnalysisDraft:
        from .analysis import build_verified_facts, compute_trend_metrics, select_facts_for_analysis

        measured_keywords = list(dict.fromkeys(str(row.get("keyword", "")) for row in evidence if row.get("keyword")))
        metrics = computed_metrics if computed_metrics is not None else compute_trend_metrics(evidence)
        all_facts = verified_facts if verified_facts is not None else build_verified_facts(evidence, metrics)
        analysis_facts = select_facts_for_analysis(all_facts, metrics)
        options = fact_selection_options(analysis_facts)
        compact = {"measured_keywords": measured_keywords, "fact_selection_options": options}
        schema = AnalysisGenerationDraft.model_json_schema()
        prompt = ("Return exactly one JSON object conforming to this AnalysisGenerationDraft schema. "
          "Python owns all canonical fact IDs and the factual market fields. Never output any fact ID. "
          "For every grounded claim, select only fact aliases from fact_selection_options using alias, exact keyword, and exact observation_period. "
          "These selections are pointers, not proof: the claim's text must still be semantically supported by the selected facts. "
          "For each comparison use previous_four_week_average, recent_four_week_average, and four_week_change selections that match the values stated; use four_week_direction only for direction. "
          "For dated facts select the matching observed_peak or weekly_observation period. Never invent numbers, dates, or alias selections. "
          "Distinguish two-window average change from weekly fluctuations, monotonic growth, long-term trends, search interest, and actual sales/demand. "
          "Do not state customer complaints/preferences, competition intensity, price tier, sales, listing counts, or demand as observed without direct supplied evidence. "
          "Generate product concepts only: concept, intended customer as a hypothesis, creative differentiation rationale, and uncertainties. "
          "Python will add the factual timing context, attach only matching keyword-interest facts, and enforce missing-data limits. Keep exactly two distinct useful concepts. "
          "Do not infer a seasonal pattern from one annual cycle. Keep claims concise; use the supplied measured keyword verbatim. "
          f"Write generated prose in {report_language}. Research niche: {niche}.\n"
          f"Available fact selections (no canonical IDs): {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}")
        client = self.client or httpx.Client(timeout=self.timeout)
        self.last_generation_received = False
        self.last_analysis_request_count = 0
        max_attempts = 1 if correction_diagnostics else 2
        error_text = "; ".join(correction_diagnostics or [])[:1200]
        for attempt in range(max_attempts):
            request_prompt = prompt
            if attempt or correction_diagnostics:
                request_prompt += ("\nCORRECTION: The previous JSON failed MarketAnalysis validation. Return a complete corrected object. "
                    f"Validation diagnostics: {error_text[:1200]}")
            self.last_analysis_request_count += 1
            response = client.post(f"{self.base_url}/api/generate", json={"model": self.model, "stream": False,
                "format": schema, "options": {"temperature": 0, "num_predict": 1400}, "prompt": request_prompt})
            self.last_generation_received = True
            response.raise_for_status()
            try:
                generated = self.parse_generation(response.json().get("response", ""))
                return materialize_analysis(generated, evidence, all_facts, report_language)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                error_text = str(exc)
                if attempt + 1 >= max_attempts:
                    raise ValueError(f"Ollama response failed MarketAnalysis validation after one correction: {error_text[:1200]}") from exc
        raise AssertionError("bounded correction loop should return or raise")

    @staticmethod
    def parse_generation(text: str) -> AnalysisGenerationDraft:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            return AnalysisGenerationDraft.model_validate_json(cleaned)
        except (ValidationError, ValueError):
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("Ollama response did not contain a valid AnalysisGenerationDraft JSON object")
            return AnalysisGenerationDraft.model_validate(json.loads(cleaned[start:end + 1]))

    def diagnose(self) -> dict:
        """Perform a small real generation request against the configured local Ollama endpoint."""
        if self.client:
            response = self.client.post(f"{self.base_url}/api/generate", json={"model": self.model,
                "stream": False, "prompt": "Reply with exactly OK."}, timeout=self.timeout)
        else:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.base_url}/api/generate", json={"model": self.model,
                    "stream": False, "prompt": "Reply with exactly OK."})
        response.raise_for_status()
        payload = response.json()
        answer = str(payload.get("response", "")).strip()
        if not answer:
            raise ValueError("Ollama returned an empty generation response")
        return {"endpoint": f"{self.base_url}/api/generate", "model": payload.get("model", self.model),
                "response": answer, "done": payload.get("done")}

    def plan(self, niche: str, candidates: list[str], allowlist: list[str]) -> dict:
        prompt = ("Return one JSON object with keyword_candidates (1-8 English strings), decisions "
          "(at most 2 objects with tool_name, keyword, rationale), and rationale. Select tool_name only "
          f"from this allowlist: {json.dumps(allowlist)}. Research niche: {niche}. Initial keywords: {json.dumps(candidates)}.")
        client = self.client or httpx.Client(timeout=self.timeout)
        response = client.post(f"{self.base_url}/api/generate", json={"model": self.model, "stream": False,
            "format": "json", "prompt": prompt})
        response.raise_for_status()
        text = response.json().get("response", "")
        try:
            return Plan.model_validate_json(text).model_dump()
        except Exception as exc:
            raise ValueError("Ollama returned an invalid research plan") from exc
