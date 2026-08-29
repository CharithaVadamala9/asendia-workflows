"""Resume screening — scores a candidate against the job with our ATS rubric."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register
from app.integrations.llm import get_llm
from app.scoring.criteria import score_candidate
from app.scoring.rubric import DEFAULT_WEIGHTS


class Config(BaseModel):
    threshold: float = Field(
        default=70.0,
        ge=0,
        le=100,
        title="Qualify threshold",
        description="Candidates scoring at or above this advance to outreach.",
    )
    weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS),
        title="Criterion weights",
        description=(
            "Relative importance of each criterion. Rescaled to sum to 1, so you can "
            "express ratios without doing the arithmetic."
        ),
    )


class Output(BaseModel):
    score: float
    qualified: bool
    threshold: float
    summary: str
    breakdown: dict
    requirements: dict
    profile: dict
    degraded: bool = False


class ResumeScreening(BaseModule):
    id = "resume_screening"
    name = "Resume Screening"
    description = (
        "Scores the candidate against the job using a weighted rubric: deterministic "
        "criteria for what is checkable, AI judgment only for semantic fit. Returns a "
        "full per-criterion breakdown."
    )
    category = ModuleCategory.AI
    config_model = Config
    output_model = Output

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        resume_text = ctx.candidate.get("resume_text") or ""
        if not resume_text.strip():
            return StepResult.fail("candidate has no resume text to screen")

        breakdown, reqs, profile = await score_candidate(
            candidate=ctx.candidate,
            job=ctx.job,
            resume_text=resume_text,
            llm=get_llm(),
            threshold=config.threshold,
            weights=config.weights,
        )

        return StepResult.ok(
            score=breakdown.score,
            qualified=breakdown.qualified,
            threshold=breakdown.threshold,
            summary=breakdown.summary,
            reason=breakdown.knockout,
            breakdown=breakdown.model_dump(),
            requirements=reqs.model_dump(),
            profile=profile.model_dump(),
            degraded=breakdown.degraded,
        )


register(ResumeScreening())
