"""Mirroring JobDiva applicants into local tables.

Shared by both entry points — the automatic poller and the manual sync — because they
need exactly the same enrichment and drifting apart is expensive. The first version of
this logic existed twice; the poller's copy fetched names and emails but not resumes or
phone numbers, so every automatically-triggered run failed at screening with "no resume
text" while manual runs worked fine. One implementation, one behaviour.

Enrichment costs two extra calls per candidate, which is why the applicant list's
`RESUMEID` matters: it takes the resume lookup from two calls to one.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.jobdiva.adapter import ApplicationRecord, JobDivaAdapter
from app.models import Application, Candidate, Job, Stage

log = logging.getLogger(__name__)


async def upsert_job(db: Session, jd: JobDivaAdapter, job_id: int) -> Job | None:
    """Mirror a JobDiva job, creating or refreshing the local row."""
    record = await jd.get_job(job_id)
    if record is None:
        log.warning("job %s not found in JobDiva", job_id)
        return None

    job = db.scalar(select(Job).where(Job.jobdiva_id == job_id))
    if job is None:
        job = Job(jobdiva_id=job_id)
        db.add(job)
    job.title = record.title
    job.description = record.description
    job.skills = record.skills
    job.experience = record.experience
    job.city = record.city
    job.state = record.state
    db.commit()
    return job


async def upsert_applications(
    db: Session, jd: JobDivaAdapter, records: list[ApplicationRecord], job: Job
) -> list[tuple[Application, bool]]:
    """Mirror a batch of applications with a fixed number of API calls.

    This is the difference between ~8 requests and ~400 for a job with 200 applicants.
    Enrichment is fetched for the whole batch up front — two calls regardless of size —
    and each candidate is then upserted from the resulting maps.

    JobDiva returns one row per application *action*, so the same candidate can appear
    several times on one job; they collapse to a single Application for us.
    """
    unique: dict[int, ApplicationRecord] = {}
    for rec in records:
        unique.setdefault(rec.candidate_id, rec)
    if not unique:
        return []

    # Only fetch what is actually missing locally, so a re-poll of known candidates
    # costs nothing.
    known = {
        c.jobdiva_id: c
        for c in db.scalars(
            select(Candidate).where(Candidate.jobdiva_id.in_(unique.keys()))
        )
    }
    need_contact = [
        cid for cid, r in unique.items()
        if not r.phone and not (known.get(cid) and known[cid].phone)
    ]
    need_resume = {
        r.resume_id: cid
        for cid, r in unique.items()
        if r.resume_id and not (known.get(cid) and known[cid].resume_text)
    }

    contacts: dict[int, dict[str, str]] = {}
    resumes: dict[str, str] = {}
    if need_contact and hasattr(jd, "get_contact_details_many"):
        contacts = await jd.get_contact_details_many(need_contact)
    if need_resume and hasattr(jd, "get_resume_texts"):
        resumes = await jd.get_resume_texts(list(need_resume))

    log.info(
        "enriched %s candidates with %s batched call(s)",
        len(unique),
        bool(need_contact) + bool(need_resume),
    )

    out = []
    for cid, rec in unique.items():
        out.append(
            await _upsert_one(
                db, jd, rec, job,
                contact=contacts.get(cid, {}),
                resume_text=resumes.get(rec.resume_id or "", ""),
            )
        )
    return out


async def upsert_application(
    db: Session, jd: JobDivaAdapter, rec: ApplicationRecord, job: Job
) -> tuple[Application, bool]:
    """Mirror one application and everything scoring needs.

    Returns `(application, was_created)`. The flag matters to the poller: the overlap
    window replays rows every cycle, and only a first sighting should start a run.

    Single-candidate convenience wrapper; batches should use `upsert_applications`.
    """
    return await _upsert_one(db, jd, rec, job)


async def _upsert_one(
    db: Session,
    jd: JobDivaAdapter,
    rec: ApplicationRecord,
    job: Job,
    *,
    contact: dict[str, str] | None = None,
    resume_text: str = "",
) -> tuple[Application, bool]:
    """Upsert one application, using pre-fetched enrichment when supplied."""
    candidate = db.scalar(
        select(Candidate).where(Candidate.jobdiva_id == rec.candidate_id)
    )
    if candidate is None:
        candidate = Candidate(jobdiva_id=rec.candidate_id)
        db.add(candidate)

    candidate.first_name = rec.first_name or candidate.first_name
    candidate.last_name = rec.last_name or candidate.last_name
    candidate.email = rec.email or candidate.email
    candidate.applied_at = rec.applied_at or candidate.applied_at
    candidate.phone = rec.phone or candidate.phone

    # The applicant list carries no phone number, and without one there is nobody to
    # call. Prefer the batch-fetched value; fall back to a single lookup only when
    # this was called outside a batch.
    if not candidate.phone:
        if contact is None and hasattr(jd, "get_contact_details"):
            contact = await jd.get_contact_details(rec.candidate_id)
        contact = contact or {}
        candidate.phone = contact.get("phone") or None
        candidate.email = candidate.email or contact.get("email")

    # Resume text is what screening actually scores; a candidate without it fails at
    # the first step. Fetched once and cached locally.
    if not candidate.resume_text:
        candidate.resume_text = resume_text or await jd.get_resume_text(
            rec.candidate_id, job.jobdiva_id, rec.resume_id
        )
    db.commit()

    application = db.scalar(
        select(Application).where(
            Application.candidate_id == candidate.id, Application.job_id == job.id
        )
    )
    if application is not None:
        return application, False

    application = Application(
        candidate_id=candidate.id,
        job_id=job.id,
        stage=Stage.APPLIED,
        applied_at=rec.applied_at,
    )
    db.add(application)
    db.commit()
    return application, True
