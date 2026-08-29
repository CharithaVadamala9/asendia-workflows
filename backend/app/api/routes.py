"""HTTP API.

Grouped into one module because the surface is small and the handlers are thin — the
interesting logic lives in the engine and the modules, and splitting six short routers
across six files would spread it out without clarifying anything.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.engine import executor, registry
from app.models import Application, Candidate, Job, Run, Stage, StepRun, Workflow
from app.polling.poller import poller_status
from app.seed import STANDARD_SCREENING, sync_applicants

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# --- modules ---------------------------------------------------------------


@router.get("/modules", tags=["modules"])
def list_modules() -> list[dict]:
    """The module catalog.

    Config schemas here are derived from each module's Pydantic model, and the
    frontend renders its configuration forms from them — which is why adding a module
    requires no frontend change.
    """
    return [s.model_dump() for s in registry.catalog()]


# --- workflows -------------------------------------------------------------


class WorkflowIn(BaseModel):
    name: str
    description: str = ""
    definition: dict
    is_active: bool = False


@router.get("/workflows", tags=["workflows"])
def list_workflows(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Workflow).order_by(Workflow.id)).all()
    return [
        {
            "id": w.id,
            "name": w.name,
            "description": w.description,
            "is_active": w.is_active,
            "step_count": len(w.definition.get("steps", [])),
            "run_count": db.scalar(
                select(func.count(Run.id)).where(Run.workflow_id == w.id)
            ),
        }
        for w in rows
    ]


@router.get("/workflows/{workflow_id}", tags=["workflows"])
def get_workflow(workflow_id: int, db: Session = Depends(get_db)) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(404, "workflow not found")
    return {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "definition": wf.definition,
        "is_active": wf.is_active,
    }


@router.post("/workflows", tags=["workflows"], status_code=201)
def create_workflow(payload: WorkflowIn, db: Session = Depends(get_db)) -> dict:
    _validate_definition(payload.definition)
    wf = Workflow(**payload.model_dump())
    db.add(wf)
    db.commit()
    return {"id": wf.id}


@router.put("/workflows/{workflow_id}", tags=["workflows"])
def update_workflow(
    workflow_id: int, payload: WorkflowIn, db: Session = Depends(get_db)
) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(404, "workflow not found")
    _validate_definition(payload.definition)
    for key, value in payload.model_dump().items():
        setattr(wf, key, value)
    db.commit()
    return {"id": wf.id}


@router.post("/workflows/from-template", tags=["workflows"], status_code=201)
def create_from_template(db: Session = Depends(get_db)) -> dict:
    wf = Workflow(
        name="Standard Screening (copy)",
        description="Cloned from the standard template.",
        definition=STANDARD_SCREENING,
    )
    db.add(wf)
    db.commit()
    return {"id": wf.id}


def _validate_definition(definition: dict) -> None:
    """Reject a workflow that references unknown modules or misconfigures one.

    Validating at save time means a broken workflow is caught in the builder rather
    than halfway through a candidate's run.
    """
    for i, step in enumerate(definition.get("steps", [])):
        module_id = step.get("module")
        if not module_id:
            raise HTTPException(422, f"step {i} has no module")
        try:
            module = registry.get(module_id)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            module.config_model.model_validate(step.get("config", {}))
        except Exception as exc:  # noqa: BLE001 — surface validation detail to the UI
            raise HTTPException(422, f"step '{step.get('id', i)}': {exc}") from None


# --- jobs and the funnel ---------------------------------------------------


@router.get("/jobs", tags=["jobs"])
def list_jobs(db: Session = Depends(get_db)) -> list[dict]:
    jobs = db.scalars(select(Job).order_by(Job.id)).all()
    return [
        {
            "id": j.id,
            "jobdiva_id": j.jobdiva_id,
            "title": j.title,
            "city": j.city,
            "state": j.state,
            "applicant_count": db.scalar(
                select(func.count(Application.id)).where(Application.job_id == j.id)
            ),
            "funnel": _funnel(db, j.id),
        }
        for j in jobs
    ]


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")

    apps = db.scalars(
        select(Application)
        .where(Application.job_id == job_id)
        .order_by(Application.score.desc().nullslast())
    ).all()

    return {
        "id": job.id,
        "jobdiva_id": job.jobdiva_id,
        "title": job.title,
        "description": job.description,
        "skills": job.skills,
        "experience": job.experience,
        "city": job.city,
        "state": job.state,
        "funnel": _funnel(db, job_id),
        "applicants": [_applicant_row(db, a) for a in apps],
    }


def _funnel(db: Session, job_id: int) -> dict[str, int]:
    """Cumulative counts: how many candidates reached at least each stage.

    A point-in-time GROUP BY reads as broken here. Stages are transitions, not
    resting places — qualifying immediately triggers outreach, so nobody is ever
    *at* "qualified" and the column always showed 0. Likewise "applied" empties as
    soon as anyone is screened. Counting everyone who got at least this far is both
    what a recruitment funnel conventionally means and the only reading where the
    numbers describe what happened.
    """
    rows = db.execute(
        select(Application.stage, func.count(Application.id))
        .where(Application.job_id == job_id)
        .group_by(Application.stage)
    ).all()
    at_stage = {stage.value: 0 for stage in Stage}
    for stage, count in rows:
        at_stage[stage] = count

    order = [s.value for s in Stage]
    counts = {
        stage: sum(at_stage[later] for later in order[i:])
        for i, stage in enumerate(order)
    }
    counts["rejected"] = (
        db.scalar(
            select(func.count(Application.id)).where(
                Application.job_id == job_id, Application.is_rejected.is_(True)
            )
        )
        or 0
    )
    return counts


def _applicant_row(db: Session, app: Application) -> dict:
    latest = db.scalar(
        select(Run)
        .where(Run.application_id == app.id)
        .order_by(Run.id.desc())
        .limit(1)
    )
    return {
        "application_id": app.id,
        "candidate_id": app.candidate_id,
        "name": app.candidate.full_name,
        "email": app.candidate.email,
        "phone": app.candidate.phone,
        "stage": app.stage,
        "is_rejected": app.is_rejected,
        "reject_reason": app.reject_reason,
        "score": app.score,
        "interview_score": app.interview_score,
        "applied_at": app.applied_at,
        "latest_run": {"id": latest.id, "status": latest.status} if latest else None,
    }


@router.post("/jobs/sync", tags=["jobs"])
async def sync_jobs(job_id: int = 4242, db: Session = Depends(get_db)) -> dict:
    """Pull applicants from JobDiva into the local mirror."""
    apps = await sync_applicants(db, job_id)
    return {"synced": len(apps)}


# --- runs ------------------------------------------------------------------


class RunRequest(BaseModel):
    workflow_id: int
    application_id: int
    dry_run: bool = False


@router.post("/runs", tags=["runs"], status_code=202)
def create_run(
    payload: RunRequest, background: BackgroundTasks, db: Session = Depends(get_db)
) -> dict:
    """Start a workflow for one application — the recruiter's manual push."""
    wf = db.get(Workflow, payload.workflow_id)
    app = db.get(Application, payload.application_id)
    if wf is None or app is None:
        raise HTTPException(404, "workflow or application not found")

    run = executor.start_run(
        db,
        workflow_id=wf.id,
        definition=wf.definition,
        application=app,
        trigger_source="manual",
        dry_run=payload.dry_run or get_settings().dry_run,
    )
    # Execute out of band: a run places a phone call and can take minutes, so the
    # request returns as soon as the run is durably created.
    background.add_task(_execute_in_background, run.id)
    return {"run_id": run.id, "status": run.status}


async def _execute_in_background(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run:
            await executor.execute(db, run)
    except Exception:  # noqa: BLE001 — a background failure must not be silent
        log.exception("background execution of run %s failed", run_id)
    finally:
        db.close()


@router.get("/runs", tags=["runs"])
def list_runs(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "workflow": r.workflow.name if r.workflow else None,
            "candidate": r.candidate.full_name if r.candidate else None,
            "job": r.job.title if r.job else None,
            "trigger_source": r.trigger_source,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
        }
        for r in runs
    ]


@router.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return {
        "id": run.id,
        "status": run.status,
        "cursor": run.cursor,
        "error": run.error,
        "dry_run": run.dry_run,
        "trigger_source": run.trigger_source,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "workflow": {"id": run.workflow_id, "name": run.workflow.name if run.workflow else None},
        "candidate": {
            "id": run.candidate_id,
            "name": run.candidate.full_name if run.candidate else None,
            "email": run.candidate.email if run.candidate else None,
            "phone": run.candidate.phone if run.candidate else None,
        },
        "job": {"id": run.job_id, "title": run.job.title if run.job else None},
        "steps": [
            {
                "id": s.id,
                "step_id": s.step_id,
                "module_id": s.module_id,
                "status": s.status,
                "config": s.config,
                "output": s.output,
                "error": s.error,
                "skip_reason": s.skip_reason,
                "duration_ms": s.duration_ms,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
            }
            for s in run.steps
        ],
    }


class ApprovalRequest(BaseModel):
    approved: bool
    decided_by: str = "recruiter"
    comment: str | None = None


@router.post("/runs/{run_id}/approve", tags=["runs"])
async def approve_run(
    run_id: int, payload: ApprovalRequest, db: Session = Depends(get_db)
) -> dict:
    """Deliver a recruiter's decision to a run waiting at an approval gate."""
    step = db.scalar(
        select(StepRun).where(
            StepRun.run_id == run_id,
            StepRun.module_id == "approval_gate",
            StepRun.status == "suspended",
        )
    )
    if step is None:
        raise HTTPException(404, "no run is waiting for approval here")
    run = await executor.resume(db, step, payload.model_dump())
    return {"run_id": run.id, "status": run.status}


# --- webhooks --------------------------------------------------------------


@router.post("/webhooks/vapi", tags=["webhooks"])
async def vapi_webhook(
    request: Request,
    x_asendia_secret: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """Receive VAPI call events and resume the run that is waiting on the call.

    VAPI has no fixed signature header — the header name and value are whatever you
    configure on the assistant — so this verifies a shared secret in constant time.
    """
    settings = get_settings()
    if not hmac.compare_digest(x_asendia_secret, settings.vapi_webhook_secret):
        raise HTTPException(401, "bad webhook secret")

    body = await request.json()
    message = body.get("message") or body
    event = message.get("type")

    if event != "end-of-call-report":
        # status-update and friends are useful for observability but do not advance
        # the run. Acknowledge and move on.
        log.info("vapi event %s ignored", event)
        return {"ok": True, "ignored": event}

    call_id = str((message.get("call") or {}).get("id") or "")
    step = db.scalar(
        select(StepRun).where(
            StepRun.external_ref == call_id, StepRun.status == "suspended"
        )
    )
    if step is None:
        log.warning("no suspended step for vapi call %s", call_id)
        return {"ok": True, "matched": False}

    run = await executor.resume(db, step, message)
    return {"ok": True, "run_id": run.id, "status": run.status}


# --- integrations ----------------------------------------------------------


@router.get("/integrations", tags=["integrations"])
def integration_status() -> dict:
    """What is live, what is mocked, and what is missing credentials.

    Powers the settings page, and is the fastest way to diagnose a demo that is not
    behaving as expected.
    """
    s = get_settings()
    return {
        "jobdiva": {
            "mode": s.jobdiva_mode,
            "configured": bool(s.jobdiva_client_id and s.jobdiva_username),
            "base_url": s.jobdiva_base_url,
            # Deliberately separate from `mode`: reads can be live while writes are
            # suppressed. The most consequential toggle in the system.
            "write_mode": s.jobdiva_write_mode,
        },
        "vapi": {
            "mode": s.vapi_mode,
            "configured": bool(s.vapi_api_key),
            "phone_number_id": bool(s.vapi_phone_number_id),
            "note": "VAPI free numbers cannot place outbound calls — import a Twilio number.",
        },
        "sms": {"mode": s.sms_mode, "configured": s.sms_mode == "log" or bool(s.twilio_account_sid)},
        "email": {"mode": s.mailjet_mode, "configured": bool(s.mailjet_api_key and s.mailjet_from_email)},
        "llm": {"mode": s.llm_mode, "configured": bool(s.anthropic_api_key), "model": s.llm_model},
        "poller": poller_status(),
        "dry_run": s.dry_run,
        "jobdiva_write_mode": s.jobdiva_write_mode,
        "public_base_url": s.public_base_url,
    }


@router.post("/integrations/jobdiva/test", tags=["integrations"])
async def test_jobdiva() -> dict:
    """Authenticate against JobDiva and report what came back."""
    from app.integrations.jobdiva.client import JobDivaClient, JobDivaError

    settings = get_settings()
    client = JobDivaClient(settings)
    try:
        await client.authenticate()
        limits = await client.get("/apiv2/bi/ApiLimits")
        return {
            "ok": True,
            "auth_scheme": client.resolved_scheme,
            "api_limits": limits,
        }
    except JobDivaError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await client.aclose()


@router.get("/overview", tags=["overview"])
def overview(db: Session = Depends(get_db)) -> dict:
    """Headline numbers for the landing dashboard.

    Deliberately computed from the same tables the run timeline reads, so the summary
    can never disagree with the detail a recruiter drills into.
    """
    apps = db.scalars(select(Application)).all()
    runs = db.scalars(select(Run)).all()
    scored = [a for a in apps if a.score is not None]
    interviewed = [a for a in apps if a.interview_score is not None]

    at_stage: dict[str, int] = {stage.value: 0 for stage in Stage}
    for a in apps:
        at_stage[a.stage] = at_stage.get(a.stage, 0) + 1
    # Cumulative, matching the per-job funnel: reached at least this stage.
    order = [s.value for s in Stage]
    stage_counts = {
        stage: sum(at_stage[later] for later in order[i:])
        for i, stage in enumerate(order)
    }

    # Time actually saved is the honest version of "how much work did this do":
    # a human screen is ~10 minutes a resume, a phone screen ~20.
    minutes_saved = len(scored) * 10 + len(interviewed) * 20

    return {
        "jobs": db.scalar(select(func.count(Job.id))) or 0,
        "applicants": len(apps),
        "scored": len(scored),
        "qualified": sum(1 for a in scored if not a.is_rejected),
        "rejected": sum(1 for a in apps if a.is_rejected),
        "interviewed": len(interviewed),
        "recommended": stage_counts.get(Stage.RECOMMENDED.value, 0),
        "average_score": round(sum(a.score for a in scored) / len(scored), 1) if scored else None,
        "average_interview": (
            round(sum(a.interview_score for a in interviewed) / len(interviewed), 1)
            if interviewed else None
        ),
        "runs": {
            "total": len(runs),
            "completed": sum(1 for r in runs if r.status == "completed"),
            "suspended": sum(1 for r in runs if r.status == "suspended"),
            "failed": sum(1 for r in runs if r.status == "failed"),
        },
        "stages": stage_counts,
        "minutes_saved": minutes_saved,
        "top_candidates": _top_candidates(scored),
        "jobs_breakdown": _jobs_breakdown(db, apps),
    }


def _top_candidates(scored: list[Application], per_job: int = 2, limit: int = 6) -> list[dict]:
    """Best candidates, capped per job.

    A plain global sort is dominated by whichever role happens to score highest —
    with 25 requisitions loaded it showed five people from one job and made the whole
    tenant look like a single opening. Capping per job surfaces the strongest
    candidate in several pipelines, which is what a recruiter actually wants to see.
    """
    out: list[dict] = []
    seen: dict[int, int] = {}
    for a in sorted(
        (a for a in scored if not a.is_rejected),
        key=lambda x: x.score or 0,
        reverse=True,
    ):
        if seen.get(a.job_id, 0) >= per_job:
            continue
        seen[a.job_id] = seen.get(a.job_id, 0) + 1
        out.append(
            {
                "name": a.candidate.full_name,
                "job": a.job.title,
                "job_id": a.job_id,
                "score": a.score,
                "interview_score": a.interview_score,
                "stage": a.stage,
                "application_id": a.id,
            }
        )
        if len(out) >= limit:
            break
    return out


def _jobs_breakdown(db: Session, apps: list[Application]) -> list[dict]:
    """Every requisition with applicants, so the overview reflects the whole tenant.

    Without this the page reports "25 jobs" in a stat tile while showing candidates
    from one of them — a summary that technically agrees with the data but reads as
    though nothing else exists.
    """
    by_job: dict[int, list[Application]] = {}
    for a in apps:
        by_job.setdefault(a.job_id, []).append(a)

    rows = []
    for job in db.scalars(select(Job)):
        group = by_job.get(job.id, [])
        if not group:
            continue
        graded = [a for a in group if a.score is not None]
        rows.append(
            {
                "id": job.id,
                "title": job.title,
                "applicants": len(group),
                "screened": len(graded),
                "qualified": sum(1 for a in graded if not a.is_rejected),
                "top_score": max((a.score for a in graded), default=None),
            }
        )
    return sorted(rows, key=lambda r: (-r["screened"], -r["applicants"]))


@router.get("/health", tags=["health"])
def health() -> dict:
    return {"ok": True, "time": datetime.now(UTC).isoformat()}
