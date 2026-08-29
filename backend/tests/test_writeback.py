"""JobDiva write path.

The most important test here is the suppression guard: writes must make zero HTTP
calls unless write mode is explicitly live. Creating a submittal puts a record in a
real recruiter's work queue, so "did we actually send it?" is a safety question, not
just a correctness one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.engine import executor
from app.integrations.jobdiva.adapter import LiveJobDiva
from app.integrations.jobdiva.mock import MockJobDiva
from app.models import Workflow


def _settings(write_mode: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        jobdiva_mode="live",
        jobdiva_client_id=1,
        jobdiva_username="u@example.com",
        jobdiva_password="p",
        jobdiva_write_mode=write_mode,
    )


class _ExplodingClient(httpx.AsyncClient):
    """Any HTTP call is a test failure — proves suppression made no request."""

    async def request(self, *a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError("a suppressed write attempted a real HTTP request")


# --- the suppression guard -------------------------------------------------


async def test_suppressed_writes_make_no_http_call():
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._client = _ExplodingClient()
    jd.client._token = "fake"  # skip authentication

    result = await jd.create_submittal(candidate_id=1, job_id=2, notes="hi")

    assert result.suppressed
    assert result.ok  # suppression is not a failure
    assert result.status == "suppressed"
    # The payload is captured so the UI can show what would have been sent.
    assert result.payload["candidateid"] == 1
    assert result.payload["jobid"] == 2


async def test_every_write_operation_respects_suppression():
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._client = _ExplodingClient()
    jd.client._token = "fake"

    results = [
        await jd.create_submittal(candidate_id=1, job_id=2),
        await jd.update_submittal(submittal_id=3, interview_date=datetime.now(UTC)),
        await jd.create_job_application(candidate_id=1, job_id=2),
        await jd.update_texting_consent(candidate_id=1, phone="+15550100", opt_in=True),
        await jd.mark_interested(candidate_id=1, job_id=2),
    ]
    assert all(r.suppressed and r.ok for r in results)


# --- payload correctness ---------------------------------------------------


async def test_create_submittal_omits_status():
    """The status vocabulary is unknown, so we must not guess one.

    No enum exists in the spec, and this tenant's PipelineStages and existing
    submittal statuses are both empty. Sending a guessed value risks writing
    something the ATS does not recognise; omitting it lets JobDiva default.
    """
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._token = "fake"

    result = await jd.create_submittal(candidate_id=1, job_id=2, notes="summary")

    assert "status" not in result.payload
    assert "submittaldate" in result.payload
    assert result.payload["internalnotes"] == "summary"


async def test_texting_consent_uses_the_documented_enum():
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._token = "fake"

    opted_in = await jd.update_texting_consent(
        candidate_id=1, phone="+15550100", opt_in=True
    )
    opted_out = await jd.update_texting_consent(
        candidate_id=1, phone="+15550100", opt_in=False
    )

    assert opted_in.payload["optType"] == "OPT_IN"
    assert opted_out.payload["optType"] == "OPT_OUT"


async def test_notes_are_truncated_to_the_field_limit():
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._token = "fake"
    result = await jd.create_submittal(candidate_id=1, job_id=2, notes="x" * 9000)
    assert len(result.payload["internalnotes"]) == 4000


# --- rejection declines rather than guessing -------------------------------


async def test_reject_declines_when_no_reasons_are_configured():
    """A configuration gap should read as one, not as a mysterious 500."""
    jd = MockJobDiva()
    assert jd.reject_reasons == []

    result = await jd.reject_applicant(candidate_id=1, job_id=2)

    assert not result.ok
    assert not result.suppressed
    assert result.status == "blocked"
    assert "no reject reasons" in result.reason


async def test_reject_uses_a_configured_reason_when_one_exists():
    jd = MockJobDiva()
    jd.reject_reasons = [{"id": "7", "name": "Not qualified"}]

    result = await jd.reject_applicant(candidate_id=1, job_id=2)

    assert result.ok
    assert result.payload["reasonId"] == "7"


# --- wiring into a run -----------------------------------------------------


async def test_qualified_candidate_gets_a_submittal_created_then_updated(
    db, application, monkeypatch
):
    """Ordering matters: the submittal must exist before it can be advanced."""
    mock = MockJobDiva()
    import app.modules.jobdiva_writeback as wb

    monkeypatch.setattr(wb, "get_jobdiva", lambda: mock)

    wf = Workflow(
        name="t",
        definition={
            "steps": [
                {"id": "screen", "module": "resume_screening", "config": {}},
                {"id": "call", "module": "ai_phone_call", "config": {}},
                {"id": "report", "module": "assessment_report", "config": {}},
                {"id": "wb", "module": "note_posting", "config": {}},
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
    assert run.status == "suspended"

    call_step = next(s for s in run.steps if s.module_id == "ai_phone_call")
    run = await executor.resume(
        db,
        call_step,
        {
            "type": "end-of-call-report",
            "endedReason": "customer-ended-call",
            "artifact": {"transcript": "..."},
            "analysis": {
                "structuredData": {
                    "interview_score": 8,
                    "recommendation": "advance",
                    "rationale": "Good.",
                }
            },
        },
    )
    assert run.status == "completed"

    ops = [w.op for w in mock.writes]
    assert ops.index("createSubmittal") < ops.index("updateSubmittal")

    wb_step = next(s for s in run.steps if s.module_id == "note_posting")
    assert wb_step.output["submittal_id"]
    # The executor, not the module, persists it.
    db.refresh(application)
    assert application.jobdiva_submittal_id == wb_step.output["submittal_id"]


async def test_unqualified_candidate_gets_no_submittal(db, application, candidate, monkeypatch):
    mock = MockJobDiva()
    import app.modules.jobdiva_writeback as wb

    monkeypatch.setattr(wb, "get_jobdiva", lambda: mock)

    candidate.resume_text = "Frontend developer. React, CSS. 1 year."
    db.commit()

    wf = Workflow(
        name="t",
        definition={
            "steps": [
                {"id": "screen", "module": "resume_screening", "config": {}},
                {"id": "wb", "module": "note_posting", "config": {}},
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

    assert run.status == "completed"
    assert "createSubmittal" not in [w.op for w in mock.writes]


async def test_write_back_failure_never_fails_the_run(db, application, monkeypatch):
    """Losing the ATS mirror is a degradation, not a reason to discard the work."""
    import app.modules.jobdiva_writeback as wb

    class Broken(MockJobDiva):
        async def create_submittal(self, **kw):
            raise RuntimeError("JobDiva exploded")

        async def post_note(self, **kw):
            raise RuntimeError("JobDiva exploded")

    monkeypatch.setattr(wb, "get_jobdiva", Broken)

    wf = Workflow(
        name="t",
        definition={
            "steps": [
                {"id": "screen", "module": "resume_screening", "config": {}},
                {"id": "wb", "module": "note_posting", "config": {}},
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

    assert run.status == "completed"
    wb_step = next(s for s in run.steps if s.module_id == "note_posting")
    assert wb_step.status == "completed"
    failures = [w for w in wb_step.output["writes"] if not w["ok"]]
    assert failures and "exploded" in failures[0]["reason"]


async def test_no_write_bypasses_the_guard():
    """A guard is only trustworthy if nothing routes around it.

    `post_note` and `post_screener` predate `_write` and originally called the HTTP
    client directly, so notes were still written live while write mode was suppressed.
    This asserts the whole write surface goes through the guard.
    """
    jd = LiveJobDiva(_settings("suppressed"))
    jd.client._client = _ExplodingClient()
    jd.client._token = "fake"

    assert await jd.post_note(candidate_id=1, job_id=2, note="hi") is None
    assert jd.last_write is not None and jd.last_write.suppressed

    assert await jd.post_screener(candidate_id=1, job_id=2, answers=[], note="") is False
    assert jd.last_write.suppressed


async def test_poll_window_tolerates_a_naive_watermark():
    """The watermark is read back from SQLite without a timezone.

    JobDiva's timestamps are normalised to aware, so comparing them against a naive
    watermark raises. This only ever broke the automatic trigger — manual syncs pass
    aware datetimes throughout, which is what hid it. Exercises the live adapter's
    filtering, since the mock ignores the date range entirely.
    """
    from datetime import datetime, timedelta

    from app.integrations.jobdiva.adapter import ApplicationRecord

    jd = LiveJobDiva(_settings("suppressed"))
    applied = datetime.now() - timedelta(days=3)  # noqa: DTZ005 — naive, as JobDiva sends

    async def _stub(job_id: int):
        return [ApplicationRecord(candidate_id=1, job_id=job_id, applied_at=applied)]

    jd._applicants_for_job = _stub  # type: ignore[assignment]

    # Naive on both sides, exactly as the poller supplies them.
    inside = await jd.fetch_new_applications(
        datetime.now() - timedelta(days=30), datetime.now(), 4242  # noqa: DTZ005
    )
    assert len(inside) == 1, "a record inside the window must be returned"

    # And the window still filters — this is not just swallowing the comparison.
    outside = await jd.fetch_new_applications(
        datetime.now() - timedelta(days=1), datetime.now(), 4242  # noqa: DTZ005
    )
    assert outside == []
