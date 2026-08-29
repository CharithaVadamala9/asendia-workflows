"""End-to-end demo: a new application arrives and is screened.

Simulates the real event by creating an actual application in JobDiva, then running
the exact pipeline the poller would run. Every JobDiva and Claude call is printed, so
the whole flow is visible from "someone applied" through to a scored candidate.

    cd backend && .venv/bin/python -m scripts.demo_apply

Creating the application is a real write, so it requires JOBDIVA_WRITE_MODE=live.
The script sets it for its own process only — it never edits .env — so a stray poll
cycle elsewhere can still never write.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import time
from datetime import UTC, datetime
from urllib.parse import quote

os.environ["JOBDIVA_WRITE_MODE"] = "live"          # this process only

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.engine import executor, registry  # noqa: E402
from app.ingest import upsert_applications, upsert_job  # noqa: E402
from app.integrations.jobdiva import shapes  # noqa: E402
from app.integrations.jobdiva.adapter import ApplicationRecord, get_jobdiva  # noqa: E402
from app.integrations.jobdiva.client import COUNTER as jd_counter  # noqa: E402
from app.integrations.jobdiva.client import JobDivaClient  # noqa: E402
from app.integrations.llm import COUNTER as llm_counter  # noqa: E402
from app.models import Workflow  # noqa: E402

JOB_ID = 26611280   # cloud engineer

RESUME = """JORDAN AVERY
San Francisco, CA | jordan.avery.demo@example.com | +1 415 555 0142
Authorized to work in the US

SUMMARY
Cloud Infrastructure Engineer with 7 years building and operating production
platforms on AWS and Azure. Focused on Kubernetes, infrastructure as code, and
observability.

EXPERIENCE

Senior Cloud Engineer, Meridian Systems (2021 - present)
  Designed and operate a multi-region EKS platform serving 40 million requests a day.
  Migrated 60+ services from EC2 to Kubernetes with zero customer-facing downtime.
  Built the Terraform module library used by every team in the company.
  Own the observability stack: Prometheus, Grafana, and Datadog for APM and alerting.
  Reduced p99 latency 40% by redesigning the ingress and connection pooling layer.

Cloud Engineer, Halcyon Data (2018 - 2021)
  Ran Azure AKS clusters and authored Bicep templates for environment provisioning.
  Implemented CI/CD in GitHub Actions across 30 repositories.
  On-call rotation for a platform with a 99.95% availability target.

EDUCATION
B.S. Computer Science, University of California, Davis, 2018

SKILLS
Kubernetes (EKS, AKS), Docker, Terraform, Bicep, AWS, Azure, Python, Go,
Prometheus, Grafana, Datadog, GitHub Actions, Linux, PostgreSQL, Redis
"""


async def main(job_id: int) -> None:
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("httpx").setLevel(logging.INFO)
    for quiet in ("app.scoring", "app.engine", "app.ingest", "app.seed",
                  "app.integrations.llm"):
        logging.getLogger(quiet).setLevel(logging.WARNING)

    settings = get_settings()
    init_db()
    registry.load_builtin_modules()
    db = SessionLocal()
    workflow = db.query(Workflow).first()

    print("=" * 74)
    print("STEP 1 — a new application arrives in JobDiva")
    print("=" * 74)

    client = JobDivaClient(settings)
    await client.authenticate()
    # JobDiva URL-decodes `textfile` server-side, so a literal "%" in the résumé
    # ("reduced latency 40% by...") is read as a malformed percent-escape and the
    # request fails with a Java URLDecoder error. Percent-encoding it first means the
    # server's decode returns the original text.
    payload = {
        "jobid": job_id,
        "filename": f"jordan_avery_{int(time.time())}.txt",
        "filecontent": base64.b64encode(RESUME.encode()).decode(),
        "textfile": quote(RESUME),
        "resumesource": 1,
    }
    if settings.jobdiva_recruiter_id:
        payload["recruiterid"] = settings.jobdiva_recruiter_id

    result = await client.post(
        "/apiv2/jobdiva/CreateJobApplicationWithResume", json=payload
    )
    print(f"\n  -> JobDiva accepted the application, returned {result!r}\n")

    print("=" * 74)
    print("STEP 2 — the trigger sees it (what the poller does every 120s)")
    print("=" * 74)
    jd_counter.reset()
    jd = get_jobdiva()

    record = None
    for attempt in range(6):
        body = await client.get("/apiv2/bi/JobApplicantsDetail", jobId=job_id)
        rows = shapes.rows(body)
        newest = max(
            rows,
            key=lambda r: shapes.timestamp(r, "DATEAPPLIED", "ACTIONDATE")
            or datetime.min,
            default=None,
        )
        applied = shapes.timestamp(newest or {}, "DATEAPPLIED", "ACTIONDATE")
        if applied and (datetime.now(UTC).replace(tzinfo=None) - applied).total_seconds() < 900:
            record = ApplicationRecord(
                candidate_id=shapes.integer(newest, "CANDIDATEID"),
                job_id=job_id,
                applied_at=applied,
                first_name=shapes.text(newest, "FIRSTNAME"),
                last_name=shapes.text(newest, "LASTNAME"),
                email=shapes.text(newest, "EMAIL") or None,
                resume_id=shapes.text(newest, "RESUMEID") or None,
            )
            break
        print(f"  not indexed yet, retrying ({attempt + 1}/6)...")
        await asyncio.sleep(5)

    await client.aclose()
    if record is None:
        print("\n  JobDiva has not indexed the new application yet. Re-run in a minute.")
        return

    print(f"\n  -> detected candidate {record.candidate_id}, applied {record.applied_at}")

    print("\n" + "=" * 74)
    print("STEP 3 — enrich: fetch résumé and contact details")
    print("=" * 74)
    job = await upsert_job(db, jd, job_id)
    pairs = await upsert_applications(db, jd, [record], job)
    application, is_new = pairs[0]
    cand = application.candidate
    print(f"\n  -> {cand.full_name or '(no name yet)'}  "
          f"résumé {len(cand.resume_text or '')} chars  "
          f"phone {'yes' if cand.phone else 'no'}  new={is_new}")
    print(f"  -> {jd_counter.total} JobDiva calls so far")

    print("\n" + "=" * 74)
    print("STEP 4 — screen against the job")
    print("=" * 74)
    llm_counter.reset()
    definition = dict(workflow.definition)
    definition["steps"] = [s for s in definition["steps"]
                           if s["module"] == "resume_screening"]

    run = await executor.execute(db, executor.start_run(
        db, workflow_id=workflow.id, definition=definition,
        application=application, trigger_source="demo-apply"))

    step = run.steps[0]
    bd = step.output.get("breakdown", {})
    print(f"\n  {step.output.get('summary')}")
    print("  " + "-" * 70)
    for c in bd.get("criteria", []):
        pts, mx = c["normalized"] * c["weight"] * 100, c["weight"] * 100
        print(f"  {c['label']:22} {pts:5.1f}/{mx:<4.0f}  {c['evidence'][:40]}")

    print(f"\n  cost: {llm_counter.calls} model calls, "
          f"${llm_counter.cost(settings.llm_model):.4f}")
    print(f"  total JobDiva calls: {jd_counter.total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=JOB_ID)
    asyncio.run(main(ap.parse_args().job))
