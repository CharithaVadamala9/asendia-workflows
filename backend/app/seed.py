"""Seed the database with the default workflow template and demo data.

The template here is the "Standard Screening" workflow the plan describes: trigger →
score → optional approval → SMS → AI call → report → write-back. It is data, not code,
which is the whole point — a recruiter can clone and edit it in the builder.
"""

from __future__ import annotations

import copy
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.jobdiva.adapter import get_jobdiva
from app.models import Application, Candidate, Job, Stage, Workflow

log = logging.getLogger(__name__)

def _standard_screening(job_id: int) -> dict:
    """The default template, pointed at whichever job this deployment watches.

    The trigger's job id has to come from configuration rather than a constant: with a
    hardcoded fixture id the poller runs happily against a job that does not exist in
    the tenant and reports nothing, which looks identical to "no new applicants".
    """
    definition = copy.deepcopy(STANDARD_SCREENING)
    definition["trigger"]["config"]["job_id"] = job_id
    return definition


STANDARD_SCREENING: dict = {
    "trigger": {
        "module": "new_applicants",
        "config": {
            "job_id": 4242,
            "poll_seconds": 120,
            "overlap_seconds": 90,
            "initial_lookback_days": 180,
        },
    },
    "steps": [
        {
            "id": "screen",
            "module": "resume_screening",
            "config": {
                "threshold": 70,
                "weights": {
                    "must_have_skills": 0.40,
                    "experience": 0.20,
                    "semantic_fit": 0.25,
                    "education": 0.10,
                    "location": 0.05,
                },
            },
        },
        {
            "id": "approve",
            "module": "approval_gate",
            "config": {
                "enabled": False,
                "note": "{{candidate.full_name}} scored "
                "{{steps.screen.output.score}} — approve outreach?",
            },
            "when": "{{steps.screen.output.qualified}} == True",
        },
        {
            "id": "sms",
            "module": "sms_outreach",
            "config": {"check_consent": True},
            "when": "{{steps.screen.output.qualified}} == True",
        },
        {
            "id": "call",
            "module": "ai_phone_call",
            "config": {
                "max_duration_seconds": 420,
                "question_count": 4,
                "ask_logistics": True,
            },
            "when": "{{steps.screen.output.qualified}} == True",
        },
        {"id": "report", "module": "assessment_report", "config": {}},
        {
            "id": "writeback",
            "module": "note_posting",
            "config": {"post_screener": True, "post_note": True},
        },
    ],
}


def seed_workflow(db: Session) -> Workflow:
    existing = db.scalar(select(Workflow).where(Workflow.name == "Standard Screening"))
    if existing:
        return existing
    wf = Workflow(
        name="Standard Screening",
        description=(
            "Score every new applicant against the job, text the ones who qualify, "
            "interview them by AI phone call, and write the results back to JobDiva."
        ),
        definition=_standard_screening(get_settings().demo_job_id),
        is_active=True,
    )
    db.add(wf)
    db.commit()
    log.info("seeded workflow %s", wf.id)
    return wf


async def sync_applicants(db: Session, job_id: int | None = None) -> list[Application]:
    """Backfill a job's applicants from JobDiva into local tables.

    Shares its enrichment with the poller via `app.ingest`, and pulls the full history
    rather than a narrow window: the watermarked window belongs to the poller, whereas
    a manual sync is asked for precisely when someone wants everything.
    """
    from datetime import UTC, datetime, timedelta

    from app.ingest import upsert_applications, upsert_job

    jd = get_jobdiva()
    now = datetime.now(UTC)
    if job_id is None:
        job_id = get_settings().demo_job_id

    job = await upsert_job(db, jd, job_id)
    if job is None:
        return []

    records = await jd.fetch_new_applications(now - timedelta(days=3650), now, job_id)

    applications = [app for app, _ in await upsert_applications(db, jd, records, job)]

    log.info("synced %s applications for job %s", len(applications), job_id)
    return applications
