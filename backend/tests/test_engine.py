"""Engine behaviour: suspend/resume, conditions, failure isolation.

The suspend/resume path is the one worth testing hardest — it is the mechanism the AI
phone call and the approval gate both depend on, and it fails silently if the run's
cursor or the correlation key drifts.
"""

from __future__ import annotations

import pytest

from app.engine import executor
from app.engine.context import RunContext
from app.models import Stage, StepRun, Workflow


def _workflow(db, steps: list[dict]) -> Workflow:
    wf = Workflow(name="test", definition={"steps": steps})
    db.add(wf)
    db.commit()
    return wf


# --- condition evaluation --------------------------------------------------


def test_conditions_use_prior_step_output():
    ctx = RunContext(run_id=1)
    ctx.record("screen", {"score": 85, "qualified": True})

    assert ctx.evaluate("{{steps.screen.output.score}} >= 70")
    assert not ctx.evaluate("{{steps.screen.output.score}} >= 90")
    assert ctx.evaluate("{{steps.screen.output.qualified}} == True")


def test_missing_value_makes_ordered_comparisons_false():
    """A skipped upstream step must not crash a downstream condition."""
    ctx = RunContext(run_id=1)
    assert not ctx.evaluate("{{steps.nothing.output.score}} >= 70")


def test_condition_rejects_arbitrary_code():
    """Conditions are user-authored, so the evaluator is a whitelist, not eval()."""
    ctx = RunContext(run_id=1)
    with pytest.raises(ValueError):
        ctx.evaluate("__import__('os').system('echo pwned')")


def test_template_preserves_types_but_interpolates_strings():
    ctx = RunContext(run_id=1, candidate={"first_name": "Priya"})
    ctx.record("screen", {"score": 85})

    assert ctx.render("{{steps.screen.output.score}}") == 85  # not "85"
    assert ctx.render("Hi {{candidate.first_name}}, you scored {{steps.screen.output.score}}") == (
        "Hi Priya, you scored 85"
    )


# --- execution -------------------------------------------------------------


async def test_full_pipeline_suspends_at_the_phone_call(db, application):
    from app.seed import STANDARD_SCREENING

    wf = _workflow(db, STANDARD_SCREENING["steps"])
    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )

    assert run.status == "suspended"
    call_step = next(s for s in run.steps if s.module_id == "ai_phone_call")
    assert call_step.status == "suspended"
    # The correlation key is what the webhook looks the run up by.
    assert call_step.external_ref

    screen = next(s for s in run.steps if s.module_id == "resume_screening")
    assert screen.output["qualified"] is True
    assert screen.output["breakdown"]["criteria"]


async def test_resume_after_call_completes_the_run(db, application):
    from app.seed import STANDARD_SCREENING

    wf = _workflow(db, STANDARD_SCREENING["steps"])
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
            "artifact": {"transcript": "Interviewer: Hi...", "recordingUrl": "https://x/rec.mp3"},
            "analysis": {
                "summary": "Strong candidate.",
                "structuredData": {
                    "interview_score": 8,
                    "recommendation": "advance",
                    "rationale": "Deep FastAPI experience.",
                    "strengths": ["Clear communicator"],
                    "concerns": [],
                },
            },
        },
    )

    assert run.status == "completed"
    assert all(s.status in ("completed", "skipped") for s in run.steps)

    db.refresh(application)
    assert application.stage == Stage.RECOMMENDED
    assert application.interview_score == 8

    report = next(s for s in run.steps if s.module_id == "assessment_report")
    assert report.output["recommendation"] == "advance"
    # 92.5 resume * 0.4 + 80 interview * 0.6 — the interview carries more weight.
    assert 80 < report.output["overall_score"] < 90


async def test_unqualified_candidate_skips_outreach(db, application, candidate):
    from app.seed import STANDARD_SCREENING

    candidate.resume_text = "Tom Okafor. Frontend developer. Skills: React, CSS. 2 years."
    db.commit()

    wf = _workflow(db, STANDARD_SCREENING["steps"])
    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )

    assert run.status == "completed"
    for module_id in ("sms_outreach", "ai_phone_call"):
        step = next(s for s in run.steps if s.module_id == module_id)
        assert step.status == "skipped"
        assert "condition not met" in step.skip_reason

    db.refresh(application)
    assert application.is_rejected
    # A rejected candidate keeps the stage they reached — the report step must not
    # push them to "recommended" or the funnel would misreport.
    assert application.stage != Stage.RECOMMENDED


async def test_knockout_scores_zero_without_spending_an_llm_call(db, application, candidate):
    candidate.resume_text = (
        "Sana Iqbal, Toronto. 5 years. Requires visa sponsorship to work in the US.\n"
        "Skills: Python, FastAPI, PostgreSQL."
    )
    db.commit()

    wf = _workflow(db, [{"id": "screen", "module": "resume_screening", "config": {}}])
    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )

    screen = run.steps[0]
    assert screen.output["score"] == 0
    assert screen.output["qualified"] is False
    assert "work authorization" in screen.output["breakdown"]["knockout"]


async def test_a_failing_step_preserves_earlier_output(db, application, monkeypatch):
    """A late failure must not discard work that already succeeded.

    The failure is injected rather than provoked by missing data — a missing phone
    number is deliberately *not* a failure, since the candidate was still screened.
    """
    from app.engine.registry import get

    async def explode(ctx, config):
        raise RuntimeError("the voice provider is down")

    monkeypatch.setattr(get("ai_phone_call"), "run", explode)

    wf = _workflow(
        db,
        [
            {"id": "screen", "module": "resume_screening", "config": {}},
            {"id": "call", "module": "ai_phone_call", "config": {}},
        ],
    )

    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )

    assert run.status == "failed"
    assert run.steps[0].status == "completed"
    assert run.steps[0].output["score"] > 0  # earlier work survived
    assert run.steps[1].status == "failed"


async def test_approval_gate_suspends_and_a_rejection_stops_outreach(db, application):
    wf = _workflow(
        db,
        [
            {"id": "gate", "module": "approval_gate", "config": {"enabled": True}},
            {"id": "sms", "module": "sms_outreach", "config": {}},
        ],
    )
    run = await executor.execute(
        db,
        executor.start_run(
            db, workflow_id=wf.id, definition=wf.definition, application=application
        ),
    )
    assert run.status == "suspended"

    gate = db.get(StepRun, run.steps[0].id)
    run = await executor.resume(db, gate, {"approved": False, "comment": "not a fit"})

    assert run.status == "failed"
    # The SMS step never ran — a rejection stops outreach rather than merely noting it.
    assert len(run.steps) == 1


async def test_definition_is_snapshotted_onto_the_run(db, application):
    """Editing a workflow must not change a run already in flight."""
    wf = _workflow(db, [{"id": "screen", "module": "resume_screening", "config": {}}])
    run = executor.start_run(
        db, workflow_id=wf.id, definition=wf.definition, application=application
    )

    wf.definition = {"steps": []}
    db.commit()

    run = await executor.execute(db, run)
    assert len(run.steps) == 1
