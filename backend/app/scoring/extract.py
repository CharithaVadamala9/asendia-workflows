"""Turning a job and a resume into structured facts we can score.

The job side has a fallback baked in deliberately. JobDiva's typed `Job` read model
carries no `skills`, `experience`, or `securityclearance` at all — those fields exist
only on the *write* models and on `/apiv2/bi/JobDetail`, which the Swagger declares as
an untyped object. So we may or may not get structured requirements at runtime. If we
do, we use them; if we do not, we extract them from the job description with one LLM
call. Scoring is never blocked on which one it gets.

The candidate side is always LLM extraction — resumes are free text by nature.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from enum import IntEnum

from pydantic import BaseModel, Field

from app.integrations.llm import COUNTER as llm_counter
from app.integrations.llm import LLMClient, LLMError

log = logging.getLogger(__name__)


class EducationLevel(IntEnum):
    """Ordinal so requirements can be compared with `>=`."""

    NONE = 0
    HIGH_SCHOOL = 1
    ASSOCIATE = 2
    BACHELOR = 3
    MASTER = 4
    DOCTORATE = 5

    @classmethod
    def parse(cls, text: str | None) -> EducationLevel:
        if not text:
            return cls.NONE
        t = text.lower()
        if any(w in t for w in ("phd", "ph.d", "doctorate", "doctoral")):
            return cls.DOCTORATE
        if any(w in t for w in ("master", "m.s", "msc", "mba", "m.eng")):
            return cls.MASTER
        if any(w in t for w in ("bachelor", "b.s", "bsc", "b.tech", "be ", "b.e")):
            return cls.BACHELOR
        if "associate" in t:
            return cls.ASSOCIATE
        if any(w in t for w in ("high school", "diploma", "ged")):
            return cls.HIGH_SCHOOL
        return cls.NONE


class JobRequirements(BaseModel):
    """What the role actually needs."""

    must_have_skills: list[str] = Field(
        default_factory=list, description="Skills a candidate must have to be viable."
    )
    nice_to_have_skills: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(
        default_factory=list,
        description="Skills or backgrounds that disqualify a candidate.",
    )
    min_years_experience: int = Field(
        default=0, description="Minimum total years of relevant experience."
    )
    education: str = Field(
        default="", description="Minimum education, e.g. 'Bachelor's in Computer Science'."
    )
    location: str = Field(default="", description="City/state, or empty if remote.")
    remote_ok: bool = True
    requires_work_authorization: bool = False
    security_clearance: bool = False

    @property
    def education_level(self) -> EducationLevel:
        return EducationLevel.parse(self.education)


class CandidateProfile(BaseModel):
    """What the candidate actually brings."""

    skills: list[str] = Field(default_factory=list)
    years_experience: float = Field(
        default=0.0, description="Total years of professional experience."
    )
    education: str = Field(default="", description="Highest degree attained.")
    current_title: str = ""
    location: str = ""
    open_to_remote: bool = True
    has_work_authorization: bool | None = Field(
        default=None,
        description="True/false if the resume states it, null if not mentioned.",
    )
    summary: str = Field(default="", description="Two-sentence background summary.")

    @property
    def education_level(self) -> EducationLevel:
        return EducationLevel.parse(self.education)


# Requirements are a property of the job, not the candidate, but scoring runs per
# candidate — so 200 applicants to one job meant 200 identical extractions of the same
# description. Keyed on a content hash rather than the job id, so an edited description
# invalidates itself without any explicit cache busting.
_REQUIREMENTS_CACHE: dict[str, tuple[JobRequirements, str]] = {}

# One lock per job fingerprint, so concurrent runs against the same job collapse into
# a single extraction instead of stampeding. Without this the cache only helps *after*
# the first batch: with concurrency 8, eight runs all miss, all call the model, and all
# write the same answer. The lock turns N concurrent misses into one call and N-1
# waiters.
_REQUIREMENTS_LOCKS: dict[str, asyncio.Lock] = {}


def _job_fingerprint(job: dict) -> str:
    payload = "|".join(
        str(job.get(k) or "")
        for k in ("id", "jobdiva_id", "title", "description", "skills", "experience")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def clear_requirements_cache() -> None:
    """Drop the cache. Used by tests, and available for a long-lived process."""
    _REQUIREMENTS_CACHE.clear()
    _REQUIREMENTS_LOCKS.clear()


async def extract_job_requirements(
    job: dict, llm: LLMClient
) -> tuple[JobRequirements, str]:
    """Get structured requirements for a job.

    Returns the requirements and the source used, so the run timeline can show whether
    we read structured ATS fields or had to infer them from prose.
    """
    fingerprint = _job_fingerprint(job)
    if cached := _REQUIREMENTS_CACHE.get(fingerprint):
        llm_counter.cache_hits += 1
        # Return a copy: callers mutate `location`, and a shared instance would leak
        # one job's overrides into the next caller.
        reqs, source = cached
        return reqs.model_copy(deep=True), source

    lock = _REQUIREMENTS_LOCKS.setdefault(fingerprint, asyncio.Lock())
    async with lock:
        # Re-check inside the lock: whoever held it first has populated the cache.
        if cached := _REQUIREMENTS_CACHE.get(fingerprint):
            llm_counter.cache_hits += 1
            reqs, source = cached
            return reqs.model_copy(deep=True), source
        return await _extract_uncached(job, llm, fingerprint)


async def _extract_uncached(
    job: dict, llm: LLMClient, fingerprint: str
) -> tuple[JobRequirements, str]:
    structured = _from_structured_fields(job)
    if structured is not None:
        _REQUIREMENTS_CACHE[fingerprint] = (structured, "jobdiva_structured")
        return structured.model_copy(deep=True), "jobdiva_structured"

    description = (job.get("description") or "").strip()
    if not description:
        log.warning("job %s has neither structured skills nor a description", job.get("id"))
        return JobRequirements(), "empty"

    try:
        reqs = await llm.complete_json(
            system=(
                "You extract hiring requirements from job descriptions. Report only "
                "what the description states or clearly implies. Do not invent "
                "requirements. If the description does not mention something, leave "
                "it empty rather than guessing a typical value.\n\n"
                "The description may be a bundle covering several requisitions. If so, "
                "extract only the requirements common to the role named in the title, "
                "and ignore location or reference numbers belonging to other postings."
            ),
            prompt=f"Job title: {job.get('title', '')}\n\nDescription:\n{description}",
            schema=JobRequirements,
            mock=_mock_requirements(job),
        )

        # The ATS record is authoritative for location. Real descriptions turn out to
        # be bundle documents covering many requisitions ("26 open reqs"), so a
        # location read out of the text can belong to an entirely different posting —
        # which silently scores every local candidate as being in the wrong place.
        structured_location = _location_of(job)
        if structured_location:
            reqs.location = structured_location
        _REQUIREMENTS_CACHE[fingerprint] = (reqs, "llm_extracted")
        return reqs.model_copy(deep=True), "llm_extracted"
    except LLMError as exc:
        log.warning("job requirement extraction failed: %s", exc)
        return JobRequirements(), "failed"


def _from_structured_fields(job: dict) -> JobRequirements | None:
    """Use JobDiva's own fields when they are actually populated.

    `skills` may arrive as a list, or as a delimited string, depending on which
    endpoint supplied it — the spec models `Skill` as a single-property stub, so we
    accept both shapes.
    """
    skills = _as_list(job.get("skills"))
    if not skills:
        return None

    # Some requirements have no structured field in JobDiva at all — there is no
    # education field on any job model, and work authorization is not modeled either.
    # Read those off the description rather than losing them.
    description = (job.get("description") or "").lower()
    return JobRequirements(
        must_have_skills=skills,
        excluded_skills=_as_list(job.get("excluded_skills")),
        min_years_experience=int(job.get("experience") or 0)
        or _years_from_text(description),
        education=_education_from_text(description),
        location=" ".join(filter(None, [job.get("city"), job.get("state")])).strip(),
        remote_ok="remote" in description,
        requires_work_authorization=any(
            p in description
            for p in ("authorized to work", "work authorization", "no sponsorship")
        ),
        security_clearance=bool(job.get("security_clearance"))
        or "security clearance" in description,
    )


def _location_of(job: dict) -> str:
    """The job's own city/state, which the ATS record is authoritative for."""
    return " ".join(filter(None, [job.get("city"), job.get("state")])).strip()


def _years_from_text(text: str) -> int:
    match = re.search(r"(\d+)\+?\s*years?", text)
    return int(match.group(1)) if match else 0


def _education_from_text(text: str) -> str:
    for phrase in ("phd", "doctorate", "master", "bachelor", "associate", "high school"):
        if phrase in text:
            # Return the sentence so the criterion can show the recruiter the actual
            # wording rather than a normalized label.
            for sentence in re.split(r"[.\n]", text):
                if phrase in sentence:
                    return sentence.strip()[:160]
    return ""


def _as_list(value: object) -> list[str]:
    """Split a delimited field, discarding JobDiva's nullish placeholders.

    Shared with the adapter so the literal string "Null" — which real job records
    carry in SKILLS — never becomes a required skill.
    """
    from app.integrations.jobdiva.shapes import skills

    return skills(value)


async def extract_candidate_profile(
    resume_text: str, candidate: dict, llm: LLMClient
) -> CandidateProfile:
    """Extract structured facts from resume text."""
    if not resume_text.strip():
        return CandidateProfile(summary="No resume text available.")

    # Long resumes cost latency for no benefit — the signal is front-loaded.
    excerpt = resume_text[:12000]
    try:
        return await llm.complete_json(
            system=(
                "You extract structured facts from resumes for candidate screening. "
                "Report only what the resume states. Never infer a skill the resume "
                "does not mention. For years of experience, sum actual employment "
                "dates rather than trusting a self-reported claim."
            ),
            prompt=f"Candidate: {candidate.get('full_name', '')}\n\nResume:\n{excerpt}",
            schema=CandidateProfile,
            mock=_mock_profile(candidate, resume_text),
        )
    except LLMError as exc:
        log.warning("candidate profile extraction failed: %s", exc)
        return CandidateProfile(summary=f"Extraction failed: {exc}")


# --- mock-mode values ------------------------------------------------------
# These keep the whole pipeline runnable with no API key. They read from the seeded
# fixture data rather than returning a fixed blob, so a mock run still exercises the
# real scoring arithmetic.


def _mock_requirements(job: dict) -> JobRequirements:
    """Requirements without an LLM.

    Deliberately does NOT invent skills. An earlier version returned a hardcoded tech
    stack, which produced confidently wrong scoring on a real phlebotomy job — every
    candidate marked down for lacking FastAPI. Mock mode should degrade honestly:
    take what the text plainly states, and leave the rest empty so those criteria
    simply do not penalize anyone.
    """
    description = (job.get("description") or "").lower()
    return JobRequirements(
        must_have_skills=_as_list(job.get("skills")),
        min_years_experience=int(job.get("experience") or 0) or _years_from_text(description),
        education=_education_from_text(description),
        location=" ".join(filter(None, [job.get("city"), job.get("state")])).strip(),
        remote_ok="remote" in description,
        requires_work_authorization=any(
            p in description
            for p in ("authorized to work", "work authorization", "no sponsorship")
        ),
    )


def _mock_profile(candidate: dict, resume_text: str) -> CandidateProfile:
    lower = resume_text.lower()
    found = [
        s
        for s in (
            "Python", "FastAPI", "PostgreSQL", "REST APIs", "Docker", "React",
            "Kubernetes", "Flask", "TypeScript", "Kafka", "Node.js",
        )
        if s.lower() in lower
    ]
    years = 0.0
    if match := re.search(r"(\d+)\+?\s*years", resume_text, re.I):
        years = float(match.group(1))

    # Only decide work authorization when the resume actually says something. Silence
    # stays None so a knockout is never triggered by an omission.
    authorized: bool | None = None
    if any(p in lower for p in ("authorized to work", "us citizen", "green card")):
        authorized = True
    elif any(p in lower for p in ("requires visa", "require sponsorship", "requires sponsorship")):
        authorized = False

    education = ""
    for phrase, label in (
        ("m.s", "Master of Science"), ("master", "Master's degree"),
        ("b.eng", "Bachelor of Engineering"), ("b.s", "Bachelor of Science"),
        ("bachelor", "Bachelor's degree"), ("b.a", "Bachelor of Arts"),
    ):
        if phrase in lower:
            education = label
            break

    # Match "City, ST" but skip the first line, which is the candidate's name and
    # frequently looks identical to a city/state pair ("Alicia Moreno, CP").
    location = ""
    US_STATES = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
        "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
        "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
        "VA","WA","WV","WI","WY","DC",
    }
    for line in resume_text.splitlines()[1:12]:
        if match := re.search(r"([A-Z][a-zA-Z .]{2,24}),\s*([A-Z]{2})\b", line):
            if match.group(2) in US_STATES:
                location = f"{match.group(1).strip()}, {match.group(2)}"
                break

    return CandidateProfile(
        skills=found,
        years_experience=years,
        education=education,
        current_title=candidate.get("current_title", ""),
        location=location,
        open_to_remote="remote" in lower,
        has_work_authorization=authorized,
        summary=f"{candidate.get('full_name', 'Candidate')} — {years:.0f}y experience, "
        f"{len(found)} matched skills.",
    )
