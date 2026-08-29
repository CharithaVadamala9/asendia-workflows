"""Test fixtures.

The suite is hermetic: environment variables are set before `app.config` is imported,
so every provider resolves to its mock regardless of what the developer has in
`backend/.env`. Without this, running the tests with real credentials configured would
call live APIs — slow, non-deterministic, subject to someone else's rate limits, and
capable of writing to a production ATS.

Environment variables take precedence over the `.env` file in pydantic-settings, which
is what makes this override reliable.
"""

from __future__ import annotations

import os
import tempfile

# Point the application's engine at a throwaway file BEFORE app.db is imported.
# Without this the suite runs against ./asendia.db — the working database — and
# test_api's drop_all() deletes real data. It did exactly that once, wiping a seeded
# demo mid-session.
_TEST_DB = tempfile.mkdtemp(prefix="asendia-tests-")

os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TEST_DB}/test.db",
        "JOBDIVA_MODE": "mock",
        "VAPI_MODE": "mock",
        "MAILJET_MODE": "mock",
        "LLM_MODE": "mock",
        "SMS_MODE": "log",
        "VAPI_WEBHOOK_SECRET": "test-secret",
        "DRY_RUN": "false",
        "DEMO_JOB_ID": "4242",
        "DEMO_PHONE_NUMBER": "",
    }
)

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.engine import registry  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _setup() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.jobdiva_mode == "mock", "tests must never hit the live ATS"
    assert _TEST_DB in settings.database_url, (
        "tests must never run against the working database — "
        f"got {settings.database_url}"
    )
    registry.load_builtin_modules()


@pytest.fixture
def db():
    """A fresh in-memory database per test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def job(db):
    from app.models import Job

    j = Job(
        jobdiva_id=4242,
        title="Senior Backend Engineer",
        description="5+ years Python. Must be authorized to work in the US. Bachelor's degree.",
        skills="Python, FastAPI, PostgreSQL",
        experience=5,
        city="Austin",
        state="TX",
    )
    db.add(j)
    db.commit()
    return j


@pytest.fixture
def candidate(db):
    from app.models import Candidate

    c = Candidate(
        jobdiva_id=1001,
        first_name="Priya",
        last_name="Raman",
        email="priya@example.com",
        phone="+15125550101",
        resume_text=(
            "Priya Raman, Austin, TX. 8 years experience. Authorized to work in the US.\n"
            "Bachelor of Science in Computer Science.\n"
            "Skills: Python, FastAPI, PostgreSQL, Docker."
        ),
    )
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def application(db, candidate, job):
    from app.models import Application

    a = Application(candidate_id=candidate.id, job_id=job.id)
    db.add(a)
    db.commit()
    return a
