"""Throughput behaviour: batching, caching, concurrency, and the interview cap.

These assert *counts*, not speed. A timing test would be flaky; "200 candidates cost 2
requests, not 400" is the property that actually matters and it is deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from app.engine import executor
from app.ingest import upsert_applications
from app.integrations.jobdiva.adapter import ApplicationRecord
from app.integrations.jobdiva.mock import MockJobDiva
from app.integrations.llm import COUNTER as llm_counter
from app.integrations.llm import LLMClient
from app.models import Job, Run, Workflow
from app.scoring.extract import clear_requirements_cache, extract_job_requirements


class _CountingAdapter(MockJobDiva):
    """Counts requests rather than candidates, which is the distinction under test."""

    def __init__(self) -> None:
        super().__init__()
        self.contact_calls = 0
        self.resume_calls = 0

    async def get_contact_details_many(self, candidate_ids):
        self.contact_calls += 1
        return {cid: {"phone": f"+1555{cid:07d}", "email": f"c{cid}@x.com"} for cid in candidate_ids}

    async def get_resume_texts(self, resume_ids):
        self.resume_calls += 1
        return {rid: "Python FastAPI PostgreSQL. 6 years experience. B.S." for rid in resume_ids}


def _records(n: int) -> list[ApplicationRecord]:
    return [
        ApplicationRecord(
            candidate_id=5000 + i,
            job_id=4242,
            first_name="C",
            last_name=str(i),
            email=f"c{i}@x.com",
            resume_id=f"r{i}",
        )
        for i in range(n)
    ]


# --- batching --------------------------------------------------------------


async def test_enrichment_cost_is_flat_in_candidate_count(db, job):
    """The whole point: 100 candidates must not mean 200 round trips.

    Every one of these endpoints accepts an array. Calling them per candidate is what
    produced ~400 requests for a job with 200 applicants, against an API that has
    already rate-limited us.
    """
    jd = _CountingAdapter()

    await upsert_applications(db, jd, _records(100), job)

    assert jd.contact_calls == 1
    assert jd.resume_calls == 1


async def test_batch_dedupes_repeated_candidates(db, job):
    """JobDiva returns one row per application action, so candidates repeat."""
    jd = _CountingAdapter()
    duplicated = _records(5) + _records(5)

    results = await upsert_applications(db, jd, duplicated, job)

    assert len(results) == 5


async def test_batch_skips_candidates_already_enriched(db, job):
    """A re-poll of known candidates should cost nothing."""
    jd = _CountingAdapter()
    await upsert_applications(db, jd, _records(10), job)
    assert jd.resume_calls == 1

    await upsert_applications(db, jd, _records(10), job)
    assert jd.resume_calls == 1, "resumes were re-fetched for candidates we already had"


# --- requirements cache ----------------------------------------------------


async def test_job_requirements_extracted_once_per_job():
    """Requirements belong to the job, but scoring runs per candidate."""
    clear_requirements_cache()
    llm_counter.reset()
    llm = LLMClient()
    job = {"id": 1, "title": "Engineer", "description": "Need 5 years.", "skills": ""}

    for _ in range(50):
        await extract_job_requirements(job, llm)

    assert llm_counter.calls == 1
    assert llm_counter.cache_hits == 49


async def test_editing_the_description_invalidates_the_cache():
    """Keyed on content, so a changed job re-extracts without explicit busting."""
    clear_requirements_cache()
    llm_counter.reset()
    llm = LLMClient()

    await extract_job_requirements({"id": 1, "description": "Need 5 years."}, llm)
    await extract_job_requirements({"id": 1, "description": "Need 10 years."}, llm)

    assert llm_counter.calls == 2


async def test_cached_requirements_are_not_shared_mutably():
    """Callers overwrite `location`; a shared instance would leak between jobs."""
    clear_requirements_cache()
    llm = LLMClient()
    job = {"id": 1, "description": "Need 5 years.", "city": "Austin", "state": "TX"}

    first, _ = await extract_job_requirements(job, llm)
    first.location = "MUTATED"
    second, _ = await extract_job_requirements(job, llm)

    assert second.location != "MUTATED"


# --- concurrency -----------------------------------------------------------


async def test_concurrent_runs_each_get_their_own_session(db, job):
    """The riskiest change in the batch.

    A SQLAlchemy Session is not safe for concurrent use even on a single-threaded
    event loop. Sharing one across gathered tasks corrupts identity-map state and
    surfaces as intermittent nonsense rather than a clean failure, so every task must
    open its own.
    """
    from app.polling.poller import _execute_all

    jd = _CountingAdapter()
    apps = [a for a, _ in await upsert_applications(db, jd, _records(12), job)]

    wf = Workflow(
        name="t", definition={"steps": [{"id": "s", "module": "resume_screening", "config": {}}]}
    )
    db.add(wf)
    db.commit()

    run_ids = [
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=a
        ).id
        for a in apps
    ]

    # Each task must build its own session from the same engine as the fixture.
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    await _execute_all(run_ids, limit=6, session_factory=factory)

    # Each run must have completed independently and consistently.
    fresh = db.query(Run).filter(Run.id.in_(run_ids)).all()
    assert len(fresh) == 12
    for r in fresh:
        db.refresh(r)
        assert r.status == "completed", f"run {r.id} ended {r.status}"
        assert len(r.steps) == 1


# --- interview cap ---------------------------------------------------------


async def test_interview_cap_counts_started_calls_not_completed_ones(db, job):
    """A call suspends awaiting its webhook.

    Counting *completed* interviews meant every concurrent run saw zero and placed a
    call anyway — 134 calls went out under a cap of 10 before this was fixed.
    """
    from app.engine.context import RunContext
    from app.engine.registry import get

    module = get("ai_phone_call")
    config = module.config_model(max_interviews_per_job=3)

    ctx = RunContext(
        run_id=1,
        candidate={"phone": "+15550100", "first_name": "A", "full_name": "A B"},
        job={"id": job.id, "title": "T", "interviews_started": 3},
    )
    result = await module.run(ctx, config)

    assert result.output["skipped_reason"]
    assert "cap reached" in result.output["skipped_reason"]
    assert not result.output["call_id"]


async def test_no_cap_when_set_to_zero(db, job):
    from app.engine.context import RunContext
    from app.engine.registry import get

    module = get("ai_phone_call")
    config = module.config_model(max_interviews_per_job=0)
    ctx = RunContext(
        run_id=1,
        candidate={"phone": "+15550100", "first_name": "A", "full_name": "A B"},
        job={"id": job.id, "title": "T", "interviews_started": 999},
    )

    result = await module.run(ctx, config)

    assert result.status.value == "suspended"


async def test_poll_once_runs_end_to_end(db, monkeypatch):
    """Covers the poller's own entry point.

    A missing import inside `_poll_workflow` shipped past the whole suite because no
    test called `poll_once` — every other test exercised the pieces it wires together
    but never the wiring itself.
    """
    from sqlalchemy.orm import sessionmaker

    import app.polling.poller as poller_mod
    from app.seed import seed_workflow

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(poller_mod, "SessionLocal", factory)
    monkeypatch.setattr(poller_mod, "get_jobdiva", MockJobDiva)

    workflow = seed_workflow(db)
    workflow.definition["trigger"]["config"]["job_id"] = 4242
    workflow.is_active = True
    db.commit()

    started = await poller_mod.poll_once()
    assert started > 0, "the poller found no applicants to run"

    # The overlap window replays the same rows; dedupe must hold.
    assert await poller_mod.poll_once() == 0


async def test_concurrent_scoring_does_not_stampede_the_cache():
    """Concurrent runs against one job must collapse to a single extraction.

    A plain cache only helps after the first call completes. With concurrency 8, eight
    runs all miss simultaneously, all call the model, and all write the same answer —
    the cache appears to do nothing on the batch that matters most.
    """
    clear_requirements_cache()
    llm_counter.reset()
    llm = LLMClient()
    job = {"id": 99, "title": "Engineer", "description": "Need 5 years.", "skills": ""}

    await asyncio.gather(*(extract_job_requirements(job, llm) for _ in range(20)))

    assert llm_counter.calls == 1, f"stampede: {llm_counter.calls} concurrent extractions"
    assert llm_counter.cache_hits == 19


async def test_missing_phone_does_not_fail_the_run(db, application, candidate):
    """A candidate with no phone number must still reach the assessment report.

    Two qualified candidates were lost this way against real JobDiva data — scored
    82.5 and 70.0, then discarded because the ATS had no number on file. Missing
    contact details are a data gap for a recruiter to fill, not an execution error.
    """
    candidate.phone = None
    db.commit()

    wf = Workflow(
        name="t",
        definition={
            "steps": [
                {"id": "screen", "module": "resume_screening", "config": {}},
                {"id": "sms", "module": "sms_outreach", "config": {}},
                {"id": "call", "module": "ai_phone_call", "config": {}},
                {"id": "report", "module": "assessment_report", "config": {}},
            ]
        },
    )
    db.add(wf)
    db.commit()

    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )

    assert run.status == "completed", f"run ended {run.status}: {run.error}"
    sms = next(s for s in run.steps if s.module_id == "sms_outreach")
    assert sms.status == "completed"
    assert "no phone number" in sms.output["reason"]
    # The whole point: the screening result survives and reaches the report.
    assert next(s for s in run.steps if s.module_id == "assessment_report").output["overall_score"]


def test_llm_client_sets_an_explicit_timeout():
    """One hung request must not hold a worker slot indefinitely.

    Runs execute concurrently under a semaphore and are gathered, so a single request
    left on the SDK's 10-minute default stalls the entire poll cycle behind it. This
    happened on a real 57-candidate run: 56 scored, one hung, the batch never
    finished.
    """
    from app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="test", llm_mode="live")  # type: ignore[call-arg]
    client = LLMClient(settings)
    underlying = client._anthropic()

    assert settings.llm_timeout_seconds <= 300, "timeout must be well under the SDK default"
    assert underlying.timeout == settings.llm_timeout_seconds


async def test_twilio_acceptance_is_not_reported_as_delivery(monkeypatch):
    """A 201 from Twilio means accepted, not delivered.

    An unregistered US number returns 201 with status "queued" and is then dropped by
    the carrier with error 30034 minutes later. Claiming delivery on the 201 would
    tell a recruiter a candidate had been contacted when they had not.
    """
    import httpx

    from app.config import Settings
    from app.integrations import messaging

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sms_mode="twilio",
        twilio_account_sid="AC123",
        twilio_auth_token="tok",
        twilio_from_number="+15550000",
    )

    class _Resp:
        status_code = 201

        def json(self):
            return {"sid": "SM123", "status": "queued"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    result = await messaging.send_sms(to="+15550001", body="hi", settings=settings)

    assert result["accepted"] is True
    assert result["delivered"] is False, "a queued message must not be reported as delivered"
    assert result["status"] == "queued"
