"""New-applicant trigger.

JobDiva has no webhooks — confirmed across all 749 endpoints in both public Swagger
specs. So detection is a watermark poll of `CandidateApplicationRecords`, which is the
only endpoint combining application semantics, a date range, and pagination. The actual
polling loop lives in `app/polling/poller.py`; this module is the trigger's declaration
and configuration, so it appears in the workflow builder like any other step.

The step itself is a no-op at execution time: by the time a run exists, the applicant
that caused it has already been fetched. It records provenance so the run timeline
starts with *why this run happened*.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register


class Config(BaseModel):
    job_id: int | None = Field(
        default=None,
        title="JobDiva job ID",
        description="Only applicants to this job trigger the workflow. Leave empty to "
        "watch every job.",
    )
    poll_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        title="Poll interval",
        description="How often to check JobDiva for new applications. The effective "
        "floor is set by your tenant's rate limit, read from ApiLimits at startup.",
    )
    initial_lookback_days: int = Field(
        default=30,
        ge=1,
        le=365,
        title="Initial backfill window (days)",
        description="How far back the very first poll looks. Applies once; later polls "
        "advance from the watermark. Set this to cover any existing applicant backlog, "
        "or a fresh install will appear to find nothing.",
    )
    overlap_seconds: int = Field(
        default=90,
        ge=0,
        le=600,
        title="Overlap window",
        description="Re-query this far behind the watermark each poll. Tolerates clock "
        "skew and JobDiva's undocumented timestamp semantics; duplicates are "
        "deduped on the candidate/job pair.",
    )


class Output(BaseModel):
    candidate_id: int | None = None
    job_id: int | None = None
    source: str
    applied_at: str | None = None


class NewApplicants(BaseModule):
    id = "new_applicants"
    name = "New Applicant Trigger"
    description = (
        "Starts the workflow when someone applies to a job in JobDiva. JobDiva has no "
        "webhooks, so this polls with a watermark and deduplicates."
    )
    category = ModuleCategory.TRIGGER
    config_model = Config
    output_model = Output

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        return StepResult.ok(
            candidate_id=ctx.candidate.get("jobdiva_id"),
            job_id=ctx.job.get("jobdiva_id"),
            source=ctx.trigger.get("source", "poller"),
            applied_at=ctx.trigger.get("applied_at"),
        )


register(NewApplicants())
