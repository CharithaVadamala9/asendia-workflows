"""The workflow executor.

Executes a run's steps in order, persisting the config and output of each one. The
design decision that matters is `SUSPENDED`: a step that hands off to an external
system (an AI phone call, a recruiter approval) returns `SUSPENDED` rather than
blocking. The run halts with all its state in the database, and `resume()` picks it up
again when the callback arrives. Nothing is held in memory between the two halves, so a
restart mid-call loses nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.engine import registry
from app.engine.base import StepStatus
from app.engine.context import RunContext
from app.models import Application, Run, StepRun, Stage

log = logging.getLogger(__name__)

# Which pipeline stage a completed module implies. Keeps stage transitions in one
# place instead of scattered through the modules.
STAGE_AFTER: dict[str, Stage] = {
    "resume_screening": Stage.SCREENED,
    "sms_outreach": Stage.CONTACTED,
    "email_notification": Stage.CONTACTED,
    "ai_phone_call": Stage.INTERVIEWED,
    "assessment_report": Stage.RECOMMENDED,
}


def start_run(
    db: Session,
    *,
    workflow_id: int,
    definition: dict,
    application: Application | None = None,
    trigger_source: str = "manual",
    dry_run: bool = False,
) -> Run:
    """Create a run in `pending`. Does not execute it — call `execute()` next.

    The workflow definition is snapshotted onto the run, so editing a workflow never
    changes the meaning of a run already in flight.
    """
    run = Run(
        workflow_id=workflow_id,
        application_id=application.id if application else None,
        candidate_id=application.candidate_id if application else None,
        job_id=application.job_id if application else None,
        definition=definition,
        trigger_source=trigger_source,
        dry_run=dry_run,
        status="pending",
        cursor=0,
    )
    db.add(run)
    db.commit()
    return run


async def execute(db: Session, run: Run) -> Run:
    """Run steps from `run.cursor` until completion, failure, or suspension."""
    steps = run.definition.get("steps", [])
    run.status = "running"
    db.commit()

    ctx = _build_context(db, run)

    while run.cursor < len(steps):
        step_def = steps[run.cursor]
        result = await _execute_step(db, run, ctx, step_def)

        if result is StepStatus.SUSPENDED:
            run.status = "suspended"
            db.commit()
            log.info("run %s suspended at step %s", run.id, step_def.get("id"))
            return run

        if result is StepStatus.FAILED:
            run.status = "failed"
            run.ended_at = datetime.now(UTC)
            db.commit()
            return run

        run.cursor += 1
        db.commit()

    run.status = "completed"
    run.ended_at = datetime.now(UTC)
    db.commit()
    return run


async def resume(db: Session, step_run: StepRun, payload: dict[str, Any]) -> Run:
    """Resume a suspended run by delivering a callback payload to its waiting step."""
    run = step_run.run
    if run.status != "suspended":
        log.warning(
            "resume called on run %s in status %s — ignoring", run.id, run.status
        )
        return run

    module = registry.get(step_run.module_id)
    ctx = _build_context(db, run)
    config = module.config_model.model_validate(step_run.config)

    try:
        result = await module.resume(ctx, config, payload)
    except Exception as exc:  # noqa: BLE001 — a module must never kill the run loop
        log.exception("resume failed for step %s", step_run.id)
        _finish_step(db, step_run, StepStatus.FAILED, {}, error=str(exc))
        run.status = "failed"
        run.error = str(exc)
        run.ended_at = datetime.now(UTC)
        db.commit()
        return run

    _finish_step(db, step_run, result.status, result.output, error=result.error)
    _advance_stage(db, run, step_run.module_id, result.output)

    if result.status is StepStatus.SUSPENDED:
        db.commit()
        return run
    if result.status is StepStatus.FAILED:
        run.status = "failed"
        run.error = result.error
        run.ended_at = datetime.now(UTC)
        db.commit()
        return run

    run.cursor += 1
    db.commit()
    return await execute(db, run)


# --- internals -------------------------------------------------------------


async def _execute_step(
    db: Session, run: Run, ctx: RunContext, step_def: dict
) -> StepStatus:
    step_id = step_def.get("id") or f"step_{run.cursor}"
    module_id = step_def["module"]

    step_run = _get_or_create_step(db, run, step_id, module_id)

    # A `when` condition that evaluates false skips the step, recording why. A
    # malformed condition is a configuration error and fails the run rather than
    # silently running a step the author meant to guard.
    condition = step_def.get("when")
    if condition:
        try:
            if not ctx.evaluate(condition):
                _finish_step(
                    db,
                    step_run,
                    StepStatus.SKIPPED,
                    {},
                    skip_reason=f"condition not met: {condition}",
                )
                ctx.record(step_id, {})
                return StepStatus.SKIPPED
        except ValueError as exc:
            _finish_step(db, step_run, StepStatus.FAILED, {}, error=str(exc))
            run.error = str(exc)
            return StepStatus.FAILED

    module = registry.get(module_id)
    raw_config = ctx.render(step_def.get("config", {}))

    step_run.status = "running"
    step_run.config = raw_config
    step_run.started_at = datetime.now(UTC)
    db.commit()

    try:
        config = module.config_model.model_validate(raw_config)
        result = await module.run(ctx, config)
    except Exception as exc:  # noqa: BLE001 — isolate module failures from the loop
        log.exception("step %s (%s) raised", step_id, module_id)
        _finish_step(db, step_run, StepStatus.FAILED, {}, error=str(exc))
        run.error = f"{step_id}: {exc}"
        return StepStatus.FAILED

    _finish_step(
        db,
        step_run,
        result.status,
        result.output,
        error=result.error,
        external_ref=result.external_ref,
    )
    ctx.record(step_id, result.output)

    if result.status is StepStatus.COMPLETED:
        _advance_stage(db, run, module_id, result.output)
    if result.status is StepStatus.FAILED:
        run.error = f"{step_id}: {result.error}"

    return result.status


def _get_or_create_step(
    db: Session, run: Run, step_id: str, module_id: str
) -> StepRun:
    existing = next((s for s in run.steps if s.step_id == step_id), None)
    if existing:
        return existing
    step_run = StepRun(
        step_id=step_id,
        module_id=module_id,
        position=run.cursor,
        status="pending",
    )
    # Append through the relationship rather than setting run_id directly, so the
    # in-memory collection stays consistent with the database for callers holding a
    # reference to this run.
    run.steps.append(step_run)
    db.add(step_run)
    db.commit()
    return step_run


def _finish_step(
    db: Session,
    step_run: StepRun,
    status: StepStatus,
    output: dict,
    *,
    error: str | None = None,
    skip_reason: str | None = None,
    external_ref: str | None = None,
) -> None:
    step_run.status = status.value
    step_run.output = output or {}
    step_run.error = error
    step_run.skip_reason = skip_reason
    if external_ref:
        step_run.external_ref = external_ref
    if step_run.started_at is None:
        step_run.started_at = datetime.now(UTC)
    # A suspended step is still in flight — leave `ended_at` unset so its duration
    # reflects the whole wait, including the external call.
    if status is not StepStatus.SUSPENDED:
        step_run.ended_at = datetime.now(UTC)
    db.commit()


def _advance_stage(db: Session, run: Run, module_id: str, output: dict) -> None:
    """Move the application forward in the funnel after a step completes."""
    if run.application_id is None:
        return
    app = db.get(Application, run.application_id)
    if app is None:
        return

    if module_id == "resume_screening":
        app.score = output.get("score")
        app.score_breakdown = output.get("breakdown", {})
        app.stage = Stage.QUALIFIED if output.get("qualified") else Stage.SCREENED
        if not output.get("qualified"):
            app.is_rejected = True
            app.reject_reason = output.get("reason") or "did not meet score threshold"
        db.commit()
        return

    if module_id == "ai_phone_call":
        app.interview_score = output.get("interview_score")

    # The write-back step reports the submittal it created; the executor is what
    # persists it, so a later run against the same application can update rather
    # than duplicate it.
    if module_id == "note_posting" and (sid := output.get("submittal_id")):
        app.jobdiva_submittal_id = sid

    # A rejected candidate keeps the stage they actually reached. Letting the report
    # step push them to "recommended" would make the funnel lie.
    if app.is_rejected:
        db.commit()
        return

    stage = STAGE_AFTER.get(module_id)
    if stage:
        app.stage = stage
    db.commit()


def _build_context(db: Session, run: Run) -> RunContext:
    """Rebuild the run context from persisted state.

    Called on both the initial execution and on resume — a resumed run reconstructs
    its context entirely from the database, holding nothing in memory across the wait.
    """
    ctx = RunContext(
        run_id=run.id,
        dry_run=run.dry_run,
        trigger=run.definition.get("trigger", {}),
    )
    if run.candidate:
        c = run.candidate
        ctx.candidate = {
            "id": c.id,
            "jobdiva_id": c.jobdiva_id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "full_name": c.full_name,
            "email": c.email,
            "phone": c.phone,
            "resume_text": c.resume_text,
        }
    if run.application:
        a = run.application
        ctx.application = {
            "id": a.id,
            "stage": a.stage,
            "score": a.score,
            "jobdiva_submittal_id": a.jobdiva_submittal_id,
        }
    if run.job:
        j = run.job
        # How many calls have been *started* on this job — not completed. A call
        # suspends awaiting its webhook, so counting completions lets every concurrent
        # run see zero and place a call anyway. Counting started calls is what makes
        # the cap actually cap.
        #
        # Concurrent runs can still race past the limit by up to the concurrency
        # setting, since each reads the count before any writes. That overshoot is
        # bounded and acceptable; a hard guarantee needs a transactional reservation,
        # which is not worth the contention here.
        interviews = (
            db.query(StepRun)
            .join(Run, StepRun.run_id == Run.id)
            .filter(
                Run.job_id == j.id,
                StepRun.module_id == "ai_phone_call",
                StepRun.status.in_(("suspended", "completed")),
            )
            .count()
        )
        ctx.job = {
            "id": j.id,
            "jobdiva_id": j.jobdiva_id,
            "title": j.title,
            "description": j.description,
            "skills": j.skills,
            "experience": j.experience,
            "interviews_started": interviews,
            "city": j.city,
            "state": j.state,
        }
    for step in run.steps:
        if step.status in ("completed", "suspended"):
            ctx.record(step.step_id, step.output or {})
    return ctx
