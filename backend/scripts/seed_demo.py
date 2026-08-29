"""Populate the dashboard from the live JobDiva tenant.

Two phases, deliberately separated by cost:

  1. **Mirror every job that has applicants** — metadata and candidates only. This is
     cheap (a handful of batched API calls, no model usage) and it is what makes the
     dashboard look like a real deployment: many requisitions, real applicant counts.
  2. **Score only the top N jobs** — screening is where the money goes (~$0.04 per
     candidate), so it is spent where someone will actually look.

    cd backend && .venv/bin/python -m scripts.seed_demo            # mirror all, score top 2
    cd backend && .venv/bin/python -m scripts.seed_demo --score 3  # score top 3
    cd backend && .venv/bin/python -m scripts.seed_demo --score 0  # mirror only

Unscored jobs show their applicants at the `applied` stage with an empty funnel, which
is honest: those candidates exist and have not been screened yet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from app.db import SessionLocal, init_db
from app.engine import executor, registry
from app.ingest import upsert_applications, upsert_job
from app.integrations.jobdiva import shapes
from app.integrations.jobdiva.adapter import ApplicationRecord, get_jobdiva
from app.integrations.jobdiva.client import COUNTER as jd_counter
from app.integrations.jobdiva.client import JobDivaClient, JobDivaError
from app.integrations.llm import COUNTER as llm_counter
from app.config import get_settings
from app.models import Application, Job
from app.polling.poller import _execute_all
from app.seed import seed_workflow

log = logging.getLogger(__name__)

CHUNK = 50


async def discover(client: JobDivaClient) -> dict[int, list[ApplicationRecord]]:
    """Find every job in the tenant that actually has applicants.

    `OpenJobsList` is not enough — it filters to status Open, and in this tenant that
    is 1 job out of 652 while every applicant-heavy requisition sits On Hold.
    """
    jobs: dict[int, str] = {}
    offset = 0
    while True:
        body = await client.post(
            "/apiv2/jobdiva/SearchJob", json={"maxReturned": 200, "offset": offset}
        )
        rows = body if isinstance(body, list) else shapes.rows(body)
        if not rows:
            break
        for r in rows:
            if jid := r.get("id"):
                jobs[int(jid)] = str(r.get("job title") or "")
        if len(rows) < 200:
            break
        offset += 200

    print(f"  {len(jobs)} jobs in the tenant")

    # Batched: one request per 50 jobs rather than one per job.
    by_job: dict[int, list[ApplicationRecord]] = {}
    ids = list(jobs)
    for i in range(0, len(ids), CHUNK):
        try:
            body = await client.get(
                "/apiv2/bi/JobsApplicantsDetail", jobIds=ids[i : i + CHUNK]
            )
        except JobDivaError as exc:
            log.warning("applicant lookup chunk failed: %s", exc)
            continue
        for row in shapes.rows(body):
            jid = shapes.integer(row, "JOBID")
            cid = shapes.integer(row, "CANDIDATEID")
            if not jid or not cid:
                continue
            existing = by_job.setdefault(jid, [])
            if any(r.candidate_id == cid for r in existing):
                continue  # one row per application action; collapse to the candidate
            existing.append(
                ApplicationRecord(
                    candidate_id=cid,
                    job_id=jid,
                    applied_at=shapes.timestamp(row, "DATEAPPLIED", "ACTIONDATE"),
                    first_name=shapes.text(row, "FIRSTNAME"),
                    last_name=shapes.text(row, "LASTNAME"),
                    email=shapes.text(row, "EMAIL") or None,
                    resume_id=shapes.text(row, "RESUMEID") or None,
                    status=shapes.text(row, "STATUS", "ACTION"),
                )
            )
    return by_job


async def main(score_top: int) -> None:
    logging.basicConfig(level=logging.ERROR)
    init_db()
    registry.load_builtin_modules()

    settings = get_settings()
    if settings.jobdiva_mode != "live":
        print("JOBDIVA_MODE is not live — nothing to mirror."); return

    db = SessionLocal()
    workflow = seed_workflow(db)
    jd = get_jobdiva()
    client = JobDivaClient(settings)
    await client.authenticate()

    jd_counter.reset()
    llm_counter.reset()
    t0 = time.perf_counter()

    print("Discovering jobs with applicants...")
    by_job = await discover(client)
    await client.aclose()
    ranked = sorted(by_job.items(), key=lambda kv: -len(kv[1]))
    print(f"  {len(ranked)} job(s) have applicants "
          f"({sum(len(v) for v in by_job.values())} applications)\n")

    print("Mirroring metadata + applicants...")
    mirrored: dict[int, list[Application]] = {}
    for jid, records in ranked:
        job = await upsert_job(db, jd, jid)
        if job is None:
            continue
        pairs = await upsert_applications(db, jd, records, job)
        mirrored[jid] = [a for a, _ in pairs]
    mirror_s = time.perf_counter() - t0
    print(f"  mirrored {len(mirrored)} jobs / {sum(len(v) for v in mirrored.values())} "
          f"applicants in {mirror_s:.0f}s using {jd_counter.total} JobDiva calls\n")

    if score_top <= 0:
        print("Scoring skipped (--score 0).")
        _summary(db, settings)
        return

    to_score = [jid for jid, _ in ranked[:score_top]]
    total = sum(len(mirrored.get(j, [])) for j in to_score)
    print(f"Screening the top {len(to_score)} job(s) — {total} candidates "
          f"(~${total * 0.041:.2f})...")

    t1 = time.perf_counter()
    run_ids = [
        executor.start_run(
            db, workflow_id=workflow.id, definition=workflow.definition,
            application=app, trigger_source="poller",
        ).id
        for jid in to_score
        for app in mirrored.get(jid, [])
    ]
    await _execute_all(run_ids, limit=settings.max_concurrent_runs)
    print(f"  scored {len(run_ids)} candidates in {time.perf_counter() - t1:.0f}s\n")
    _summary(db, settings)


def _summary(db, settings) -> None:
    fresh = SessionLocal()   # the workers wrote through their own sessions
    jobs = fresh.query(Job).count()
    apps = fresh.query(Application).all()
    scored = [a for a in apps if a.score is not None]
    cost = llm_counter.cost(settings.llm_model)
    print("=" * 62)
    print(f"  jobs mirrored     {jobs}")
    print(f"  applicants        {len(apps)}")
    print(f"  screened          {len(scored)}")
    print(f"  qualified         {sum(1 for a in scored if not a.is_rejected)}")
    print(f"  JobDiva calls     {jd_counter.total}")
    print(f"  LLM calls         {llm_counter.calls} (cache saved {llm_counter.cache_hits})")
    print(f"  spend             ${cost:.2f}")
    fresh.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", type=int, default=2,
                    help="how many of the largest jobs to screen (0 = mirror only)")
    asyncio.run(main(ap.parse_args().score))
