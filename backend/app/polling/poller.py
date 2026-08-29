"""JobDiva applicant poller — the automatic trigger.

JobDiva exposes no webhooks, so new applications are detected by polling
`CandidateApplicationRecords` over a date range and keeping a high-water mark.

Two details make this correct rather than merely working:

  - **The overlap window.** JobDiva's date filter semantics are undocumented — created
    vs. modified, and in which timezone. A poll starting exactly at the last watermark
    will silently drop records. We re-query a short way behind it and deduplicate,
    because being slightly redundant is much cheaper than being subtly lossy.
  - **Deduplication on (candidate, job).** The overlap guarantees replays, so the
    upsert is what keeps one application from starting three runs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.engine import executor
from app.ingest import upsert_applications, upsert_job
from app.integrations.jobdiva.adapter import get_jobdiva
from app.models import Application, Job, PollWatermark, Run, Workflow

log = logging.getLogger(__name__)

STREAM = "candidate_applications"


async def poll_once() -> int:
    """One polling cycle. Returns the number of runs started."""
    db = SessionLocal()
    try:
        workflows = db.scalars(
            select(Workflow).where(Workflow.is_active.is_(True))
        ).all()
        if not workflows:
            return 0

        # Concurrent across workflows too: with many job postings, serialising them
        # means the last job waits for every earlier one.
        results = await asyncio.gather(
            *(_poll_workflow(SessionLocal(), wf) for wf in workflows),
            return_exceptions=True,
        )
        started = 0
        for wf, result in zip(workflows, results, strict=True):
            if isinstance(result, Exception):
                log.exception("polling workflow %s failed: %s", wf.id, result)
            else:
                started += result
        return started
    except Exception:  # noqa: BLE001 — a poll failure must not kill the scheduler
        log.exception("polling cycle failed")
        return 0
    finally:
        db.close()


async def _poll_workflow(db: Session, workflow: Workflow) -> int:
    trigger = workflow.definition.get("trigger") or {}
    if trigger.get("module") != "new_applicants":
        return 0

    config = trigger.get("config") or {}
    job_id: int | None = config.get("job_id")
    overlap = int(config.get("overlap_seconds", 90))

    watermark = _watermark(
        db,
        f"{STREAM}:{workflow.id}",
        int(config.get("initial_lookback_days", 30)),
    )
    now = datetime.now(UTC)
    since = watermark.cursor_at - timedelta(seconds=overlap)

    records = await get_jobdiva().fetch_new_applications(since, now, job_id)
    if not records:
        watermark.cursor_at = now
        watermark.last_polled_at = now
        db.commit()
        return 0

    # Mirror the whole batch first: enrichment is fetched in a fixed number of calls
    # regardless of batch size, which is what keeps 200 applicants off the rate limit.
    jd = get_jobdiva()
    job = await _job_for(db, jd, records)
    if job is None:
        return 0
    mirrored = await upsert_applications(db, jd, records, job)

    # Only a first sighting starts a run. The overlap window replays rows every cycle,
    # and re-running would phone the same candidate twice.
    pending = [
        app
        for app, is_new in mirrored
        if is_new
        and not db.scalar(
            select(Run).where(
                Run.application_id == app.id, Run.workflow_id == workflow.id
            )
        )
    ]

    runs = [
        executor.start_run(
            db,
            workflow_id=workflow.id,
            definition=workflow.definition,
            application=app,
            trigger_source="poller",
        ).id
        for app in pending
    ]

    watermark.cursor_at = now
    watermark.last_polled_at = now
    db.commit()

    if runs:
        limit = get_settings().max_concurrent_runs
        log.info("executing %s run(s) with concurrency %s", len(runs), limit)
        await _execute_all(runs, limit)
    return len(runs)


async def _execute_all(
    run_ids: list[int], limit: int, session_factory=None
) -> None:
    """Execute runs concurrently, bounded.

    Each task opens its **own** session. A SQLAlchemy Session is not safe for
    concurrent use even in a single-threaded event loop — sharing one across gathered
    tasks corrupts identity-map state and surfaces as baffling intermittent failures
    rather than a clean error.

    `session_factory` is injectable so this is testable against a scratch database
    rather than only the configured one.
    """
    make_session = session_factory or SessionLocal
    semaphore = asyncio.Semaphore(max(1, limit))

    async def one(run_id: int) -> None:
        async with semaphore:
            db = make_session()
            try:
                run = db.get(Run, run_id)
                if run:
                    await executor.execute(db, run)
            except Exception:  # noqa: BLE001 — one bad run must not sink the batch
                log.exception("run %s failed during concurrent execution", run_id)
            finally:
                db.close()

    await asyncio.gather(*(one(rid) for rid in run_ids))


async def _job_for(db: Session, jd, records: list):
    """Resolve (and mirror) the job these applications belong to."""
    job_id = next((r.job_id for r in records if r.job_id), None)
    if job_id is None:
        return None
    job = db.scalar(select(Job).where(Job.jobdiva_id == job_id))
    return job or await upsert_job(db, jd, job_id)


def _watermark(db: Session, stream: str, initial_lookback_days: int = 30) -> PollWatermark:
    """Fetch or create the high-water mark for a stream.

    The first run has no mark and must choose how far back to look. A short window is
    wrong for a tenant with an existing backlog — real applications here are months
    old, so a one-day lookback finds nothing on a fresh install and the trigger appears
    broken when it is merely late. The window is configurable per trigger, and applies
    only once: every subsequent poll advances from the mark.
    """
    mark = db.scalar(select(PollWatermark).where(PollWatermark.stream == stream))
    if mark is None:
        start = datetime.now(UTC) - timedelta(days=initial_lookback_days)
        mark = PollWatermark(stream=stream, cursor_at=start, last_polled_at=start)
        db.add(mark)
        db.commit()
        log.info(
            "first poll of %s — backfilling the last %s days", stream, initial_lookback_days
        )
    return mark


# Why the poller is or is not running, so the UI can say so. A trigger that silently
# fails to start is indistinguishable from "no new applicants" — the worst way for
# this feature to break.
STATUS: dict[str, object] = {
    "running": False,
    "job_id": None,
    "poll_seconds": None,
    "reason": "not started",
}


def poller_status() -> dict[str, object]:
    return dict(STATUS)


def start_scheduler() -> AsyncIOScheduler | None:
    """Start background polling if any active workflow wants it."""
    settings = get_settings()
    db = SessionLocal()
    try:
        workflow = db.scalar(select(Workflow).where(Workflow.is_active.is_(True)))
        if workflow is None:
            STATUS.update(running=False, reason="no active workflow")
            log.info("no active workflow — poller not started")
            return None
        trigger_config = (workflow.definition.get("trigger") or {}).get("config") or {}
        interval = int(trigger_config.get("poll_seconds", 120))
        watched_job = trigger_config.get("job_id")
    finally:
        db.close()

    if settings.jobdiva_mode != "live":
        # In mock mode the fixture always returns the same five applicants, so a
        # running poller would add noise rather than signal. Sync manually instead.
        STATUS.update(
            running=False,
            job_id=watched_job,
            reason="JobDiva is mocked — use POST /api/jobs/sync instead",
        )
        log.info("JobDiva is mocked — poller not started (use POST /api/jobs/sync)")
        return None

    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_once, "interval", seconds=interval, id=STREAM)
    scheduler.start()
    STATUS.update(
        running=True, job_id=watched_job, poll_seconds=interval, reason="running"
    )
    log.info("applicant poller watching job %s every %ss", watched_job, interval)
    return scheduler
