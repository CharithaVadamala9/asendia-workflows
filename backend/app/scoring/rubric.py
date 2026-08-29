"""The scoring rubric — our ATS model.

A staffing client will ask "why 72?", and the answer has to survive that question. So
the score is a weighted sum of independently computed criteria, each carrying the
evidence that produced it. The LLM is confined to the one judgment it is uniquely good
at — semantic fit — and contributes a single bounded number rather than the verdict.

Three properties follow from that structure, and each is a talking point:
  - **Explainable.** Every criterion reports its own score, weight, and evidence.
  - **Tunable.** Weights and threshold are module config, set per workflow in the UI.
  - **Degradable.** If the LLM call fails, the deterministic criteria still produce a
    score and the result is marked degraded. The workflow does not halt.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CriterionScore(BaseModel):
    """One line of the score breakdown, as rendered in the dashboard."""

    key: str
    label: str
    # 0.0-1.0, before weighting. Keeping it normalized means weights are comparable.
    normalized: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0)
    # Human-readable justification: "4 of 5 required skills matched".
    evidence: str
    # The specifics behind the evidence, e.g. which skills matched and which did not.
    detail: dict = Field(default_factory=dict)

    @property
    def points(self) -> float:
        return round(self.normalized * self.weight * 100, 1)

    @property
    def max_points(self) -> float:
        return round(self.weight * 100, 1)


class ScoreBreakdown(BaseModel):
    """The complete, explainable result of scoring one candidate against one job."""

    score: float = Field(ge=0.0, le=100.0)
    qualified: bool
    threshold: float
    criteria: list[CriterionScore] = Field(default_factory=list)

    # Set when a hard knockout short-circuited scoring (missing work authorization,
    # an explicitly excluded skill). Score is 0 and no LLM budget was spent.
    knockout: str | None = None
    # True when a criterion could not be computed — most often the LLM call failing.
    # The score is still usable, but the UI flags it and the recruiter knows why.
    degraded: bool = False
    degraded_reason: str | None = None

    @property
    def summary(self) -> str:
        if self.knockout:
            return f"Disqualified: {self.knockout}"
        verdict = "qualified" if self.qualified else "below threshold"
        return f"{self.score:.0f}/100 — {verdict} (threshold {self.threshold:.0f})"


# Defaults. Overridable per workflow through the module's config, which is how the
# same rubric serves a senior engineering role and a warehouse role differently.
DEFAULT_WEIGHTS: dict[str, float] = {
    "must_have_skills": 0.40,
    "experience": 0.20,
    "semantic_fit": 0.25,
    "education": 0.10,
    "location": 0.05,
}

LABELS: dict[str, str] = {
    "must_have_skills": "Must-have skills",
    "experience": "Years of experience",
    "semantic_fit": "Semantic fit (AI)",
    "education": "Education",
    "location": "Location & logistics",
}


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Rescale weights to sum to 1.0.

    Recruiters editing weights in the UI will not make them sum to 1. Rather than
    rejecting the config, we honor the ratios they expressed.
    """
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in weights.items()}


def combine(
    criteria: list[CriterionScore],
    *,
    threshold: float,
    knockout: str | None = None,
    degraded_reason: str | None = None,
) -> ScoreBreakdown:
    """Fold criterion scores into a final breakdown."""
    if knockout:
        return ScoreBreakdown(
            score=0.0,
            qualified=False,
            threshold=threshold,
            criteria=criteria,
            knockout=knockout,
        )

    # Renormalize across the criteria we actually computed, so a dropped criterion
    # lowers confidence rather than silently capping the achievable score.
    live_weight = sum(c.weight for c in criteria)
    if live_weight <= 0:
        score = 0.0
    else:
        score = sum(c.normalized * c.weight for c in criteria) / live_weight * 100

    score = round(min(100.0, max(0.0, score)), 1)
    return ScoreBreakdown(
        score=score,
        qualified=score >= threshold,
        threshold=threshold,
        criteria=criteria,
        degraded=degraded_reason is not None,
        degraded_reason=degraded_reason,
    )
