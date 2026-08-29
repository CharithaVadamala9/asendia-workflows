"""ORM models.

The execution tables (`Run`, `StepRun`) persist the input and output of every step.
That single decision buys three things at once: the run timeline the dashboard renders,
a full audit trail for compliance, and the durable state that lets a suspended run
resume when an external callback arrives.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Stage(StrEnum):
    """Our internal pipeline vocabulary.

    Deliberately ours rather than JobDiva's: these map 1:1 onto engine state, so the
    funnel is a direct readout of what the executor actually did and cannot drift.
    JobDiva's statuses are tenant-configurable (the spec declares no enums for them),
    so we fetch theirs at boot and translate only at the write-back boundary.
    """

    APPLIED = "applied"
    SCREENED = "screened"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    INTERVIEWED = "interviewed"
    RECOMMENDED = "recommended"


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # The workflow template: {"trigger": {...}, "steps": [...]}. Stored as JSON so a
    # new module type needs no schema migration.
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    runs: Mapped[list["Run"]] = relationship(back_populates="workflow")


class Candidate(Base):
    """Local mirror of a JobDiva candidate.

    JobDiva has no webhooks, so we poll and mirror. `jobdiva_id` is int64 upstream.
    """

    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("jobdiva_id", name="uq_candidate_jobdiva_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    jobdiva_id: Mapped[int | None] = mapped_column(Integer, index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    resume_text: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Job(Base):
    """Local mirror of a JobDiva job order."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("jobdiva_id", name="uq_job_jobdiva_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    jobdiva_id: Mapped[int | None] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[str] = mapped_column(Text, default="")
    # Minimum years required. JobDiva exposes this only on IBiData endpoints, so it
    # may arrive empty and the rubric falls back to extracting it from the description.
    experience: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    """A candidate's application to a job — the spine of the job-centric view.

    One row per (candidate, job). The funnel on the job page is a GROUP BY over
    `stage`; the poller upserts on the JobDiva id pair so replayed poll windows
    dedupe rather than duplicating.
    """

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_application_candidate_job"),
        Index("ix_applications_job_stage", "job_id", "stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)

    stage: Mapped[str] = mapped_column(String(20), default=Stage.APPLIED, index=True)
    # Terminal flag rather than a stage, so we keep the stage a candidate reached
    # when they were rejected.
    is_rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)

    score: Mapped[float | None] = mapped_column(Float)
    # Full ScoreBreakdown: every criterion with its evidence. Drives the UI table.
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    interview_score: Mapped[float | None] = mapped_column(Float)

    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    # JobDiva's own submittal id, once we have created or found one.
    jobdiva_submittal_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    candidate: Mapped["Candidate"] = relationship()
    job: Mapped["Job"] = relationship()


class Run(Base):
    """One execution of a workflow against one application."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))

    # pending | running | suspended | completed | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # Index of the next step to execute. A suspended run resumes from here.
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    # Snapshot of the workflow definition at launch, so editing a workflow never
    # changes the meaning of a run already in flight.
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    trigger_source: Mapped[str] = mapped_column(String(50), default="manual")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    workflow: Mapped["Workflow"] = relationship(back_populates="runs")
    application: Mapped["Application | None"] = relationship()
    candidate: Mapped["Candidate | None"] = relationship()
    job: Mapped["Job | None"] = relationship()
    steps: Mapped[list["StepRun"]] = relationship(
        back_populates="run", order_by="StepRun.position", cascade="all, delete-orphan"
    )


class StepRun(Base):
    """One step within a run. Input and output are both persisted."""

    __tablename__ = "step_runs"
    __table_args__ = (
        Index("ix_step_runs_external_ref", "external_ref"),
        Index("ix_step_runs_run_step", "run_id", "step_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(String(80))
    module_id: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)

    # pending | running | suspended | completed | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    # Why a step was skipped — the `when` expression that evaluated false.
    skip_reason: Mapped[str | None] = mapped_column(Text)
    # Correlation key for asynchronous steps: the VAPI call id lands here so the
    # webhook can find the suspended run in one indexed lookup.
    external_ref: Mapped[str | None] = mapped_column(String(120))

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    run: Mapped["Run"] = relationship(back_populates="steps")

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.ended_at:
            return int((self.ended_at - self.started_at).total_seconds() * 1000)
        return None


class PollWatermark(Base):
    """High-water mark for a JobDiva polling stream.

    JobDiva exposes no webhooks, so change detection is a date-range poll. We re-query
    a small overlap behind the watermark to tolerate clock skew and undocumented
    timestamp semantics, then dedupe on the upstream id.
    """

    __tablename__ = "poll_watermarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream: Mapped[str] = mapped_column(String(80), unique=True)
    last_polled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cursor_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
