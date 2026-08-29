"""Throughput benchmark: N applicants against one job.

Produces the numbers behind the scaling claims, so they can be quoted rather than
estimated. Runs entirely against the mock adapter and mock LLM — the point is to
measure *our* call counts and concurrency, not JobDiva's or Anthropic's latency, and a
benchmark that burns real API quota is one nobody runs twice.

    cd backend && .venv/bin/python -m scripts.benchmark          # 200 applicants
    cd backend && .venv/bin/python -m scripts.benchmark 500 16   # count, concurrency

Reports wall time, throughput, and the two counts that actually matter at scale: how
many JobDiva requests and how many LLM calls were made for N candidates.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta

# Force mocks before app config is imported.
os.environ.update(
    {"JOBDIVA_MODE": "mock", "LLM_MODE": "mock", "VAPI_MODE": "mock", "SMS_MODE": "log"}
)

from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.engine import executor, registry  # noqa: E402
from app.ingest import upsert_applications, upsert_job  # noqa: E402
from app.integrations.jobdiva import mock as jd_mock  # noqa: E402
from app.integrations.jobdiva.adapter import ApplicationRecord  # noqa: E402
from app.integrations.jobdiva.client import COUNTER as jd_counter  # noqa: E402
from app.integrations.llm import COUNTER as llm_counter  # noqa: E402
from app.models import Application, Run  # noqa: E402
from app.polling.poller import _execute_all  # noqa: E402
from app.scoring.extract import clear_requirements_cache  # noqa: E402
from app.seed import seed_workflow  # noqa: E402


class CountingMock(jd_mock.MockJobDiva):
    """Mock adapter that counts requests the way the live client does.

    The mock never makes HTTP calls, so the real counter stays at zero. Counting here
    is what lets the benchmark show batching working: one increment per *request*, not
    per candidate.
    """

    def __init__(self, records: list[ApplicationRecord]):
        super().__init__()
        self._records = records
        self.requests = 0

    async def fetch_new_applications(self, since, until, job_id=None):
        self.requests += 1
        return list(self._records)

    async def get_job(self, job_id: int):
        self.requests += 1
        return jd_mock.JOB

    async def get_contact_details_many(self, candidate_ids):
        # One request per chunk of 50, mirroring the live adapter.
        self.requests += max(1, (len(candidate_ids) + 49) // 50)
        return {cid: {"phone": f"+1555{cid:07d}", "email": f"c{cid}@example.com"} for cid in candidate_ids}

    async def get_resume_texts(self, resume_ids):
        self.requests += max(1, (len(resume_ids) + 49) // 50)
        return {rid: _resume(int(rid.split("_")[0][3:] or 0)) for rid in resume_ids}

    async def get_resume_text(self, candidate_id, job_id=None, resume_id=None):
        self.requests += 1
        return _resume(candidate_id % 1000)


# Resumes are generated against the mock job (a backend engineering role) and vary in
# quality, so the benchmark exercises both branches: candidates who qualify and hit the
# interview cap, and candidates who are screened out. A population where everyone
# passes — or nobody does — measures only half the pipeline.
_SKILL_POOL = ["Python", "FastAPI", "PostgreSQL", "REST APIs", "Docker", "Kubernetes"]


def _resume(i: int) -> str:
    """Deterministic spread: strong, borderline, and weak candidates."""
    tier = i % 3
    skills = {0: _SKILL_POOL, 1: _SKILL_POOL[:3], 2: _SKILL_POOL[:1]}[tier]
    years = {0: 8, 1: 5, 2: 2}[tier]
    degree = {0: "Bachelor of Science in Computer Science", 1: "B.S. Information Systems", 2: ""}[tier]
    return f"""Candidate {i:04d} — Backend Engineer
Austin, TX | {years} years experience | Authorized to work in the US
{degree}

EXPERIENCE
Backend Engineer ({years} years). Built and maintained REST APIs, owned schema design,
shipped services to production.

SKILLS
{", ".join(skills)}
"""


def _records(n: int) -> list[ApplicationRecord]:
    now = datetime.now(UTC)
    return [
        ApplicationRecord(
            candidate_id=900_000 + i,
            job_id=jd_mock.JOB_ID,
            applied_at=now - timedelta(minutes=i),
            first_name="Cand",
            last_name=f"{i:04d}",
            email=f"cand{i}@example.com",
            resume_id=f"900{i}_r",
        )
        for i in range(n)
    ]


async def main(count: int, concurrency: int) -> None:
    settings = get_settings()
    print(f"Benchmark: {count} applicants, one job, concurrency {concurrency}")
    print(f"(mocked JobDiva and LLM — measuring our call counts, not vendor latency)\n")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    registry.load_builtin_modules()
    clear_requirements_cache()
    jd_counter.reset()
    llm_counter.reset()

    db = SessionLocal()
    workflow = seed_workflow(db)
    jd = CountingMock(_records(count))

    # --- ingest ---------------------------------------------------------------
    t0 = time.perf_counter()
    job = await upsert_job(db, jd, jd_mock.JOB_ID)
    records = await jd.fetch_new_applications(None, None, jd_mock.JOB_ID)
    mirrored = await upsert_applications(db, jd, records, job)
    ingest_s = time.perf_counter() - t0
    ingest_requests = jd.requests

    # --- execute --------------------------------------------------------------
    run_ids = [
        executor.start_run(
            db,
            workflow_id=workflow.id,
            definition=workflow.definition,
            application=app,
            trigger_source="benchmark",
        ).id
        for app, _ in mirrored
    ]
    t1 = time.perf_counter()
    await _execute_all(run_ids, concurrency)
    exec_s = time.perf_counter() - t1
    total_s = time.perf_counter() - t0

    # --- results --------------------------------------------------------------
    db2 = SessionLocal()
    runs = db2.query(Run).all()
    apps = db2.query(Application).all()
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    scored = [a for a in apps if a.score is not None]
    interviewed = sum(1 for r in runs if r.status == "suspended")
    qualified = sum(1 for a in apps if not a.is_rejected and a.score is not None)
    capped = sum(
        1
        for s in db2.query(Run).all()
        for st in s.steps
        if st.module_id == "ai_phone_call" and (st.output or {}).get("skipped_reason")
    )

    print(f"  ingest            {ingest_s:7.2f}s   {ingest_requests} JobDiva request(s)")
    print(f"  execute           {exec_s:7.2f}s   {len(run_ids)} runs")
    print(f"  TOTAL             {total_s:7.2f}s")
    print()
    print(f"  throughput        {count / total_s:7.1f} applicants/sec")
    print(f"  JobDiva requests  {ingest_requests:7d}   ({ingest_requests / count:.3f} per applicant)")
    print(f"  LLM calls         {llm_counter.calls:7d}   ({llm_counter.calls / count:.2f} per applicant)")
    print(f"  cache hits        {llm_counter.cache_hits:7d}   (job requirements not re-extracted)")
    print()
    print(f"  scored            {len(scored)}")
    print(f"  qualified         {qualified}")
    print(f"  interviews placed {interviewed}")
    print(f"  capped (not called) {capped}   <- scoring everyone is cheap, phoning everyone is not")
    print(f"  run status        {by_status}")
    db.close()
    db2.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    c = int(sys.argv[2]) if len(sys.argv) > 2 else get_settings().max_concurrent_runs
    asyncio.run(main(n, c))
