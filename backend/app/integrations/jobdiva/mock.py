"""Mock JobDiva adapter with realistic fixtures.

This is what makes the demo independent of the sandbox being reachable, and it is also
how the automated tests exercise the full pipeline without network access.

The candidates are deliberately spread across the quality range — a strong match, a
borderline one, a career changer, and one who fails a hard knockout — so the funnel on
the job page shows a real distribution rather than five identical rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.integrations.jobdiva.adapter import (
    ApplicationRecord,
    JobRecord,
    WriteResult,
)

JOB_ID = 4242

JOB = JobRecord(
    job_id=JOB_ID,
    title="Senior Backend Engineer",
    description=(
        "We are hiring a Senior Backend Engineer to build and scale the APIs behind "
        "our recruitment automation platform.\n\n"
        "You will design REST services, own data models, and take features from "
        "design through production.\n\n"
        "Requirements:\n"
        "- 5+ years of professional backend engineering experience\n"
        "- Strong Python, ideally with FastAPI or a similar async framework\n"
        "- Solid relational database skills (PostgreSQL)\n"
        "- Experience designing and consuming REST APIs\n"
        "- Comfortable with Docker and modern deployment practices\n"
        "- Bachelor's degree in Computer Science or equivalent practical experience\n\n"
        "Nice to have: Kubernetes, event-driven architectures, prior ATS or HR-tech work.\n\n"
        "Remote within the US. Must be authorized to work in the US."
    ),
    skills="Python, FastAPI, PostgreSQL, REST APIs, Docker",
    city="Austin",
    state="TX",
    experience=5,
    education="Bachelor's degree in Computer Science or equivalent",
    remote=True,
)

RESUMES: dict[int, str] = {
    1001: """Priya Raman — Senior Backend Engineer
Austin, TX | priya.raman@example.com | 8 years experience

Bachelor of Science in Computer Science, University of Texas at Austin, 2017.
Authorized to work in the US.

EXPERIENCE
Senior Backend Engineer, Northwind Data (2021-present)
  Led the rebuild of a monolithic billing service into FastAPI microservices serving
  4M requests/day. Designed the PostgreSQL schema and migration path with zero
  downtime. Introduced Docker-based local development across a team of 14.
Backend Engineer, Cartogram (2018-2021)
  Built and maintained REST APIs in Python. Owned the search indexing pipeline.

SKILLS
Python, FastAPI, PostgreSQL, REST APIs, Docker, Kubernetes, Redis, async programming
""",
    1002: """Marcus Webb — Backend Developer
Remote (Denver, CO) | marcus.webb@example.com | 3 years experience

B.S. Information Systems, Colorado State, 2021. US citizen.

EXPERIENCE
Backend Developer, Sightline (2022-present)
  Python services using Flask. Postgres for primary storage. Some Docker.
Junior Developer, Local Agency (2021-2022)
  PHP and MySQL maintenance work.

SKILLS
Python, Flask, PostgreSQL, REST APIs, Docker, Git
""",
    1003: """Elena Vasquez — Engineering Manager / Staff Engineer
Seattle, WA | elena.v@example.com | 12 years experience

M.S. Computer Science, University of Washington, 2013.

EXPERIENCE
Engineering Manager, Brightpath (2020-present)
  Managed 9 engineers across two backend teams. Still hands-on: Python, FastAPI,
  PostgreSQL. Drove the platform's move to Kubernetes.
Staff Engineer, Brightpath (2016-2020)
  Designed the event-driven ingestion architecture. Python, Kafka, Postgres, Docker.

SKILLS
Python, FastAPI, PostgreSQL, REST APIs, Docker, Kubernetes, Kafka, system design
""",
    1004: """Tom Okafor — Frontend Engineer
Chicago, IL | tom.okafor@example.com | 6 years experience

B.A. Design, 2018.

EXPERIENCE
Senior Frontend Engineer, Vellum (2020-present)
  React and TypeScript. Built the component library. Some Node.js BFF work.
Frontend Engineer, Studio Nine (2018-2020)
  React, CSS, accessibility.

SKILLS
React, TypeScript, JavaScript, CSS, Node.js, REST APIs
""",
    1005: """Sana Iqbal — Backend Engineer
Toronto, ON, Canada | sana.iqbal@example.com | 5 years experience

B.Eng Software Engineering, University of Toronto, 2019.
Requires visa sponsorship to work in the US.

EXPERIENCE
Backend Engineer, Maple Systems (2020-present)
  Python and FastAPI services. PostgreSQL. Docker and CI pipelines.

SKILLS
Python, FastAPI, PostgreSQL, REST APIs, Docker
""",
}

CANDIDATES = [
    (1001, "Priya", "Raman", "priya.raman@example.com", "+15125550101"),
    (1002, "Marcus", "Webb", "marcus.webb@example.com", "+13035550102"),
    (1003, "Elena", "Vasquez", "elena.v@example.com", "+12065550103"),
    (1004, "Tom", "Okafor", "tom.okafor@example.com", "+13125550104"),
    (1005, "Sana", "Iqbal", "sana.iqbal@example.com", "+14165550105"),
]


class MockJobDiva:
    """In-memory adapter with the same interface as `LiveJobDiva`."""

    def __init__(self) -> None:
        self.notes: list[dict] = []
        self.screeners: list[dict] = []
        # Every write is recorded so tests can assert on the exact payload sent.
        self.writes: list[WriteResult] = []
        self._next_submittal_id = 55_000_000
        # Empty, mirroring the real tenant — which is what makes rejection decline
        # rather than silently fail.
        self.reject_reasons: list[dict[str, str]] = []
        self.last_write: WriteResult | None = None
        # Candidate 1004 opted out of texting, so a mock run still exercises the
        # consent branch rather than always taking the happy path.
        self.opted_out = {1004}

    async def fetch_new_applications(
        self, since: datetime, until: datetime, job_id: int | None = None
    ) -> list[ApplicationRecord]:
        if job_id is not None and job_id != JOB_ID:
            return []
        now = datetime.now(UTC)
        return [
            ApplicationRecord(
                candidate_id=cid,
                job_id=JOB_ID,
                applied_at=now - timedelta(minutes=10 * i),
                first_name=first,
                last_name=last,
                email=email,
                phone=_demo_phone(i, phone),
            )
            for i, (cid, first, last, email, phone) in enumerate(CANDIDATES)
        ]

    async def get_job(self, job_id: int) -> JobRecord | None:
        return JOB if job_id == JOB_ID else None

    async def get_resume_text(
        self, candidate_id: int, job_id: int | None = None, resume_id: str | None = None
    ) -> str:
        return RESUMES.get(candidate_id, "")

    async def get_contact_details(self, candidate_id: int) -> dict[str, str]:
        for i, (cid, _f, _l, email, phone) in enumerate(CANDIDATES):
            if cid == candidate_id:
                return {"phone": _demo_phone(i, phone), "email": email}
        return {}

    async def has_texting_consent(self, candidate_id: int | None) -> bool | None:
        if candidate_id is None:
            return None
        return candidate_id not in self.opted_out

    async def post_note(
        self, *, candidate_id: int, job_id: int | None, note: str
    ) -> int | None:
        self.notes.append({"candidate_id": candidate_id, "job_id": job_id, "note": note})
        note_id = 900000 + len(self.notes)
        self.last_write = self._record(
            "createCandidateNote",
            {"candidateid": candidate_id, "link2AnOpenJob": job_id},
            note_id,
        )
        return note_id

    async def post_screener(
        self, *, candidate_id: int, job_id: int, answers: list[dict], note: str
    ) -> bool:
        self.screeners.append(
            {"candidate_id": candidate_id, "job_id": job_id, "answers": answers, "note": note}
        )
        self.last_write = self._record(
            "createOrUpdateCandidateScreener",
            {"candidateId": candidate_id, "jobId": job_id, "answers": len(answers)},
            True,
        )
        return True


    # -- writes -------------------------------------------------------------

    def _record(self, op: str, payload: dict, result: object = None) -> WriteResult:
        w = WriteResult(op=op, ok=True, payload=payload, result=result)
        self.writes.append(w)
        return w

    async def create_submittal(
        self, *, candidate_id: int, job_id: int, notes: str = "",
        recruiter_id: int | None = None,
    ) -> WriteResult:
        self._next_submittal_id += 1
        payload = {"candidateid": candidate_id, "jobid": job_id}
        if notes:
            payload["internalnotes"] = notes[:4000]
        if recruiter_id:
            payload["recruitedbyid"] = recruiter_id
        return self._record("createSubmittal", payload, self._next_submittal_id)

    async def update_submittal(
        self, *, submittal_id: int, interview_date: datetime | None = None,
        notes: str = "", status: str | None = None,
    ) -> WriteResult:
        payload: dict = {"submittalid": submittal_id}
        if interview_date:
            payload["interviewdate"] = str(interview_date)
        if notes:
            payload["internalnotes"] = notes[:4000]
        if status:
            payload["status"] = status
        return self._record("updateSubmittal", payload, True)

    async def create_job_application(
        self, *, candidate_id: int, job_id: int, resume_source: str = "Asendia"
    ) -> WriteResult:
        return self._record(
            "createJobApplication",
            {"candidateid": candidate_id, "jobid": job_id, "resumesource": resume_source},
            True,
        )

    async def update_texting_consent(
        self, *, candidate_id: int, phone: str, opt_in: bool, note: str = ""
    ) -> WriteResult:
        return self._record(
            "updateTextingOptInOut",
            {
                "candidateId": candidate_id,
                "phoneNumber": phone,
                "optType": "OPT_IN" if opt_in else "OPT_OUT",
            },
            True,
        )

    async def mark_interested(self, *, candidate_id: int, job_id: int) -> WriteResult:
        return self._record(
            "MarkCandidateAsInterested",
            {"candidateId": candidate_id, "jobId": job_id},
            True,
        )

    async def get_reject_reasons(self) -> list[dict[str, str]]:
        return self.reject_reasons

    async def reject_applicant(
        self, *, candidate_id: int, job_id: int, reason: str | None = None
    ) -> WriteResult:
        if not self.reject_reasons:
            w = WriteResult(
                op="rejectApplicant",
                ok=False,
                payload={"candidateId": candidate_id, "jobId": job_id},
                reason=(
                    "no reject reasons are configured on this JobDiva tenant — "
                    "an administrator must add them before rejections can be written back"
                ),
            )
            self.writes.append(w)
            return w
        return self._record(
            "rejectApplicant",
            {
                "candidateId": candidate_id,
                "jobId": job_id,
                "reasonId": self.reject_reasons[0]["id"],
            },
            True,
        )


def _demo_phone(index: int, default: str) -> str:
    """Route the first candidate to a real phone when one is configured.

    Set DEMO_PHONE_NUMBER in .env to your own number and the strongest candidate's
    screening call actually rings you — which is how the live demo is rehearsed.
    """
    if index == 0 and (real := get_settings().demo_phone_number):
        return real
    return default
