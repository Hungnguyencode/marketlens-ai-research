from .models import Plan, ToolDecision


class AgentPlanner:
    """Bounded planner: an invalid/unavailable model decision falls back deterministically."""
    def __init__(self, allowed_tools: tuple[str, ...], llm=None):
        self.allowed_tools, self.llm = allowed_tools, llm

    def plan(self, niche: str, candidates: list[str]) -> tuple[Plan, str]:
        fallback = Plan(keyword_candidates=candidates[:8],
            decisions=[ToolDecision(tool_name=self.allowed_tools[0], keyword=k, rationale="Deterministic fallback: collect catalog metadata for each keyword.") for k in candidates[:2]],
            rationale="Deterministic fallback plan; no autonomous model decision is claimed.")
        if not self.llm:
            return fallback, "deterministic_fallback"
        try:
            proposed = self.llm(niche=niche, candidates=candidates, allowlist=list(self.allowed_tools))
            plan = Plan.model_validate(proposed)
            if any(d.tool_name not in self.allowed_tools for d in plan.decisions):
                return fallback, "deterministic_fallback_invalid_tool"
            return plan, "llm_plan"
        except Exception:
            return fallback, "deterministic_fallback_invalid_response"
