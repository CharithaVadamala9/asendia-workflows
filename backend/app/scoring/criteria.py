"""Individual scoring criteria, and the top-level `score_candidate` entry point.

Each criterion is computed independently and reports its own evidence. The split
between rules and the LLM is deliberate: rules decide what is objectively checkable
(is this skill present, are there enough years, does the degree clear the bar), and the
LLM decides only what requires judgment (does this experience genuinely match this
role, and is "React" the same thing as "front-end framework").
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.integrations.llm import LLMClient, LLMError
from app.scoring.extract import (
    CandidateProfile,
    JobRequirements,
    extract_candidate_profile,
    extract_job_requirements,
)
from app.scoring.rubric import (
    DEFAULT_WEIGHTS,
    LABELS,
    CriterionScore,
    ScoreBreakdown,
    combine,
    normalize_weights,
)

log = logging.getLogger(__name__)


class SkillMatch(BaseModel):
    """LLM verdict on which required skills the candidate actually demonstrates."""

    matched: list[str] = Field(
        default_factory=list,
        description="Required skills the candidate demonstrably has, by the required "
        "skill's own name. Include equivalents: count 'React' as matching "
        "'front-end framework'.",
    )
    missing: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="", description="One sentence on the judgment calls made.")


class SemanticFit(BaseModel):
    """LLM judgment of overall fit — the one thing rules cannot do."""

    score: int = Field(ge=0, le=10, description="0-10. Does this experience match this role?")
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    reasoning: str = ""


async def score_candidate(
    *,
    candidate: dict,
    job: dict,
    resume_text: str,
    llm: LLMClient,
    threshold: float = 70.0,
    weights: dict[str, float] | None = None,
) -> tuple[ScoreBreakdown, JobRequirements, CandidateProfile]:
    """Score one candidate against one job.

    Returns the breakdown plus both extracted models — the AI interview step reuses
    them to generate its questions rather than re-extracting.
    """
    w = normalize_weights({**DEFAULT_WEIGHTS, **(weights or {})})

    reqs, source = await extract_job_requirements(job, llm)
    profile = await extract_candidate_profile(resume_text, candidate, llm)

    # Be explicit when there is no AI in the loop. On real ATS data the structured
    # requirement fields are often empty, so without an LLM the rubric is scoring
    # against almost nothing — a number the recruiter should not read as confident.
    offline_note = (
        None if llm.is_live
        else "AI scoring unavailable (LLM_MODE=mock) — rules only, low confidence"
    )

    # Knockouts short-circuit before any LLM spend on fit.
    if knockout := _check_knockouts(reqs, profile):
        return (
            combine([], threshold=threshold, knockout=knockout),
            reqs,
            profile,
        )


    criteria: list[CriterionScore] = []
    degraded_reason: str | None = None

    skills_criterion, skills_failed = await _score_skills(reqs, profile, llm, w)
    criteria.append(skills_criterion)

    criteria.append(_score_experience(reqs, profile, w))

    fit_criterion, fit_failed = await _score_semantic_fit(
        reqs, profile, job, resume_text, llm, w
    )
    if fit_criterion:
        criteria.append(fit_criterion)

    criteria.append(_score_education(reqs, profile, w))
    criteria.append(_score_location(reqs, profile, w))

    if offline_note:
        degraded_reason = offline_note
    elif fit_failed or skills_failed:
        degraded_reason = (
            "AI judgment unavailable — scored on deterministic criteria only"
        )

    breakdown = combine(criteria, threshold=threshold, degraded_reason=degraded_reason)
    log.info(
        "scored candidate=%s job=%s → %.1f (%s, requirements from %s)",
        candidate.get("id"),
        job.get("id"),
        breakdown.score,
        "qualified" if breakdown.qualified else "below threshold",
        source,
    )
    return breakdown, reqs, profile


def _check_knockouts(reqs: JobRequirements, profile: CandidateProfile) -> str | None:
    """Hard disqualifiers. Explicit only — never inferred from silence."""
    if reqs.requires_work_authorization and profile.has_work_authorization is False:
        return "role requires work authorization the candidate does not have"

    have = {s.lower() for s in profile.skills}
    for excluded in reqs.excluded_skills:
        if excluded.lower() in have:
            return f"candidate background includes an excluded skill: {excluded}"
    return None


async def _score_skills(
    reqs: JobRequirements,
    profile: CandidateProfile,
    llm: LLMClient,
    w: dict[str, float],
) -> tuple[CriterionScore, bool]:
    """Match required skills, using the LLM only to resolve equivalence."""
    weight = w["must_have_skills"]
    required = reqs.must_have_skills
    if not required:
        return (
            CriterionScore(
                key="must_have_skills",
                label=LABELS["must_have_skills"],
                normalized=1.0,
                weight=weight,
                evidence="No required skills specified for this role.",
            ),
            False,
        )

    # Exact matches are free; only ask the model about the remainder.
    have = {s.lower().strip() for s in profile.skills}
    exact = [r for r in required if r.lower().strip() in have]
    unresolved = [r for r in required if r not in exact]

    matched, failed, reasoning = list(exact), False, ""
    if unresolved and profile.skills:
        try:
            verdict = await llm.complete_json(
                system=(
                    "You judge whether a candidate's skills satisfy a job's required "
                    "skills. Count genuine equivalents and demonstrated experience, "
                    "not merely similar-sounding words. When in doubt, treat the "
                    "skill as missing."
                ),
                prompt=(
                    f"Required skills still unmatched: {unresolved}\n"
                    f"Candidate's skills: {profile.skills}\n"
                    f"Candidate background: {profile.summary}"
                ),
                schema=SkillMatch,
                mock=SkillMatch(matched=[], missing=unresolved),
            )
            matched += [m for m in verdict.matched if m in unresolved]
            reasoning = verdict.reasoning
        except LLMError as exc:
            log.warning("skill matching degraded to exact-match only: %s", exc)
            failed = True

    missing = [r for r in required if r not in matched]
    evidence = f"{len(matched)} of {len(required)} required skills matched"
    if missing:
        evidence += f" — missing: {', '.join(missing)}"

    return (
        CriterionScore(
            key="must_have_skills",
            label=LABELS["must_have_skills"],
            normalized=len(matched) / len(required),
            weight=weight,
            evidence=evidence,
            detail={"matched": matched, "missing": missing, "reasoning": reasoning},
        ),
        failed,
    )


def _score_experience(
    reqs: JobRequirements, profile: CandidateProfile, w: dict[str, float]
) -> CriterionScore:
    weight = w["experience"]
    required = reqs.min_years_experience
    actual = profile.years_experience

    if required <= 0:
        normalized, evidence = 1.0, f"{actual:.0f} years experience; no minimum specified"
    else:
        # Meeting the bar is full marks — exceeding it is not extra credit, which
        # stops a 20-year veteran from washing out a missing required skill.
        normalized = min(1.0, actual / required)
        evidence = f"{actual:.0f} of {required} years required"
        if actual >= required:
            evidence += " — meets requirement"

    return CriterionScore(
        key="experience",
        label=LABELS["experience"],
        normalized=normalized,
        weight=weight,
        evidence=evidence,
        detail={"required_years": required, "actual_years": actual},
    )


async def _score_semantic_fit(
    reqs: JobRequirements,
    profile: CandidateProfile,
    job: dict,
    resume_text: str,
    llm: LLMClient,
    w: dict[str, float],
) -> tuple[CriterionScore | None, bool]:
    """The one criterion that is genuinely a judgment call."""
    weight = w["semantic_fit"]
    try:
        fit = await llm.complete_json(
            system=(
                "You assess how well a candidate's actual experience matches a role. "
                "Judge substance over keywords: someone who has done the work scores "
                "well even with different vocabulary, and someone who lists the right "
                "words without evidence of doing the work scores poorly. Be candid "
                "about concerns."
            ),
            prompt=(
                f"ROLE: {job.get('title', '')}\n"
                f"{(job.get('description') or '')[:4000]}\n\n"
                f"REQUIREMENTS: {reqs.model_dump_json()}\n\n"
                f"CANDIDATE: {profile.model_dump_json()}\n\n"
                f"RESUME:\n{resume_text[:8000]}"
            ),
            schema=SemanticFit,
            mock=SemanticFit(
                score=7,
                strengths=["Relevant background"],
                concerns=["Scored in mock mode — no AI judgment applied"],
                reasoning="Mock mode.",
            ),
            max_tokens=1500,
        )
    except LLMError as exc:
        log.warning("semantic fit unavailable: %s", exc)
        # Drop the criterion entirely rather than guessing. `combine` renormalizes
        # across the remaining weights, so the score stays meaningful.
        return None, True

    return (
        CriterionScore(
            key="semantic_fit",
            label=LABELS["semantic_fit"],
            normalized=fit.score / 10,
            weight=weight,
            evidence=f"{fit.score}/10 — {fit.reasoning}"[:300],
            detail={
                "score": fit.score,
                "strengths": fit.strengths,
                "concerns": fit.concerns,
            },
        ),
        False,
    )


def _score_education(
    reqs: JobRequirements, profile: CandidateProfile, w: dict[str, float]
) -> CriterionScore:
    weight = w["education"]
    required, actual = reqs.education_level, profile.education_level

    if required <= 0:
        normalized = 1.0
        evidence = f"{profile.education or 'not stated'}; no requirement specified"
    elif actual >= required:
        normalized = 1.0
        evidence = f"{profile.education or 'unstated'} meets {reqs.education}"
    else:
        # Partial credit — one level short is not the same as no degree at all.
        normalized = max(0.0, actual / required)
        evidence = f"{profile.education or 'not stated'} below required {reqs.education}"

    return CriterionScore(
        key="education",
        label=LABELS["education"],
        normalized=normalized,
        weight=weight,
        evidence=evidence,
        detail={"required": reqs.education, "actual": profile.education},
    )


def _score_location(
    reqs: JobRequirements, profile: CandidateProfile, w: dict[str, float]
) -> CriterionScore:
    weight = w["location"]

    if reqs.remote_ok or not reqs.location:
        return CriterionScore(
            key="location",
            label=LABELS["location"],
            normalized=1.0,
            weight=weight,
            evidence="Role is remote-friendly",
            detail={"remote_ok": reqs.remote_ok},
        )

    job_loc, cand_loc = reqs.location.lower(), (profile.location or "").lower()
    if not cand_loc:
        normalized, evidence = 0.5, "Candidate location not stated"
    elif any(part in cand_loc for part in job_loc.split() if len(part) > 2):
        normalized, evidence = 1.0, f"Candidate in {profile.location}, role in {reqs.location}"
    elif profile.open_to_remote:
        normalized, evidence = 0.6, "Different location, but open to remote"
    else:
        normalized, evidence = 0.0, f"Candidate in {profile.location}, role requires {reqs.location}"

    return CriterionScore(
        key="location",
        label=LABELS["location"],
        normalized=normalized,
        weight=weight,
        evidence=evidence,
        detail={"job_location": reqs.location, "candidate_location": profile.location},
    )
