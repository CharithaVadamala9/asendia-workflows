"""Assessment report — folds every prior step into one recruiter-facing verdict.

Pure aggregation over what earlier steps already persisted. No new LLM call in the
default path: the screening rubric and the interview each produced a defensible score,
and inventing a third opinion on top of them would obscure rather than clarify.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register


class Config(BaseModel):
    resume_weight: float = Field(
        default=0.4,
        ge=0,
        le=1,
        title="Resume weight in the final score",
        description="The interview carries the remainder. Interviews are more "
        "predictive than resumes, so the default leans on the call.",
    )
    advance_threshold: float = Field(default=70.0, ge=0, le=100)


class Output(BaseModel):
    overall_score: float
    recommendation: str
    headline: str
    resume_score: float | None = None
    interview_score: float | None = None
    strengths: list[str] = []
    concerns: list[str] = []
    narrative: str = ""


class AssessmentReport(BaseModule):
    id = "assessment_report"
    name = "Assessment Report"
    description = (
        "Combines the resume score and the interview evaluation into a single "
        "recommendation with the supporting evidence."
    )
    category = ModuleCategory.ACTION
    config_model = Config
    output_model = Output

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        screening = _find(ctx, "score", "breakdown")
        interview = _find(ctx, "interview_score", "transcript")

        resume_score = screening.get("score")
        # The interview returns 1-10; the rubric returns 0-100. Normalize before
        # combining, or the interview would be worth almost nothing.
        raw_interview = interview.get("interview_score")
        interview_score = float(raw_interview) * 10 if raw_interview is not None else None

        overall = _combine(resume_score, interview_score, config.resume_weight)

        strengths = list(interview.get("strengths") or [])
        concerns = list(interview.get("concerns") or [])
        for criterion in (screening.get("breakdown") or {}).get("criteria", []):
            if criterion.get("normalized", 1) >= 0.9:
                strengths.append(criterion.get("evidence", ""))
            elif criterion.get("normalized", 1) < 0.5:
                concerns.append(criterion.get("evidence", ""))

        # The interview's own recommendation wins when we have one — a human-like
        # conversation surfaces things a resume cannot.
        recommendation = interview.get("recommendation") or (
            "advance" if overall >= config.advance_threshold else "reject"
        )

        name = ctx.candidate.get("full_name", "Candidate")
        title = ctx.job.get("title", "the role")
        headline = f"{name} — {overall:.0f}/100 for {title} — {recommendation.upper()}"

        narrative = _narrative(
            name, title, resume_score, raw_interview, interview, recommendation
        )

        return StepResult.ok(
            overall_score=round(overall, 1),
            recommendation=recommendation,
            headline=headline,
            resume_score=resume_score,
            interview_score=raw_interview,
            strengths=[s for s in strengths if s][:6],
            concerns=[c for c in concerns if c][:6],
            narrative=narrative,
        )


def _find(ctx: RunContext, *required_keys: str) -> dict:
    """Locate a step's output by the keys it contains.

    Steps are addressed by what they produced rather than by step id, so renaming a
    step in the workflow builder does not break the report.
    """
    for step in ctx.steps.values():
        out = step.get("output") or {}
        if any(k in out for k in required_keys):
            return out
    return {}


def _combine(
    resume: float | None, interview: float | None, resume_weight: float
) -> float:
    if resume is None and interview is None:
        return 0.0
    if interview is None:
        return float(resume or 0)
    if resume is None:
        return float(interview)
    return resume * resume_weight + interview * (1 - resume_weight)


def _narrative(
    name: str,
    title: str,
    resume_score: float | None,
    interview_score: float | None,
    interview: dict,
    recommendation: str,
) -> str:
    parts = [f"{name} was screened for {title}."]
    if resume_score is not None:
        parts.append(f"Resume scored {resume_score:.0f}/100 against the role's requirements.")
    if interview_score is not None:
        parts.append(f"The AI phone screen scored {interview_score}/10.")
        if rationale := interview.get("rationale"):
            parts.append(rationale)
    if availability := interview.get("availability"):
        parts.append(f"Availability: {availability}.")
    if rate := interview.get("rate_expectation"):
        parts.append(f"Rate expectation: {rate}.")
    parts.append(f"Recommendation: {recommendation}.")
    return " ".join(parts)


register(AssessmentReport())
