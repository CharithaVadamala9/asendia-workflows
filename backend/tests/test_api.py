"""API surface, including the VAPI webhook that resumes a suspended run."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import Base, engine
from app.main import app


@pytest.fixture
def client():
    """A client over an empty database.

    `conftest` has already redirected DATABASE_URL to a temp directory, so dropping
    tables here is safe. The monkeypatch this replaced did not actually work — it set
    an attribute on the engine after its connection pool was built, so drop_all still
    hit ./asendia.db and destroyed real data.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(engine)


def test_module_catalog_exposes_config_schemas(client):
    modules = client.get("/api/modules").json()
    assert len(modules) == 8

    screening = next(m for m in modules if m["id"] == "resume_screening")
    # The frontend renders its config form from exactly this.
    assert "threshold" in screening["config_schema"]["properties"]
    assert screening["category"] == "ai"

    call = next(m for m in modules if m["id"] == "ai_phone_call")
    assert call["is_async"] is True


def test_workflow_validation_rejects_an_unknown_module(client):
    resp = client.post(
        "/api/workflows",
        json={"name": "bad", "definition": {"steps": [{"id": "x", "module": "nope"}]}},
    )
    assert resp.status_code == 422
    assert "unknown module" in resp.json()["detail"]


def test_workflow_validation_rejects_bad_config(client):
    resp = client.post(
        "/api/workflows",
        json={
            "name": "bad",
            "definition": {
                "steps": [
                    {
                        "id": "s",
                        "module": "resume_screening",
                        "config": {"threshold": 500},  # ceiling is 100
                    }
                ]
            },
        },
    )
    assert resp.status_code == 422


def test_job_funnel_counts_by_stage(client):
    client.post("/api/jobs/sync")
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["applicant_count"] == 5
    assert jobs[0]["funnel"]["applied"] == 5

    detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert len(detail["applicants"]) == 5
    assert detail["title"] == "Senior Backend Engineer"


def test_vapi_webhook_rejects_a_bad_secret(client):
    resp = client.post(
        "/api/webhooks/vapi",
        json={"message": {"type": "end-of-call-report"}},
        headers={"x-asendia-secret": "wrong"},
    )
    assert resp.status_code == 401


def test_vapi_webhook_resumes_the_waiting_run(client):
    """The end-to-end asynchronous path: run suspends, webhook lands, run completes."""
    client.post("/api/jobs/sync")
    job = client.get("/api/jobs").json()[0]
    applicant = next(
        a for a in client.get(f"/api/jobs/{job['id']}").json()["applicants"]
        if a["name"] == "Priya Raman"
    )

    started = client.post(
        "/api/runs",
        json={"workflow_id": 1, "application_id": applicant["application_id"]},
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "suspended"
    call_step = next(s for s in run["steps"] if s["module_id"] == "ai_phone_call")
    call_id = call_step["output"]["call_id"]

    resp = client.post(
        "/api/webhooks/vapi",
        json={
            "message": {
                "type": "end-of-call-report",
                "endedReason": "customer-ended-call",
                "call": {"id": call_id},
                "artifact": {"transcript": "...", "recordingUrl": "https://x/r.mp3"},
                "analysis": {
                    "structuredData": {
                        "interview_score": 9,
                        "recommendation": "advance",
                        "rationale": "Excellent fit.",
                    }
                },
            }
        },
        headers={"x-asendia-secret": get_settings().vapi_webhook_secret},
    )
    assert resp.json()["status"] == "completed"

    final = client.get(f"/api/runs/{run_id}").json()
    report = next(s for s in final["steps"] if s["module_id"] == "assessment_report")
    assert report["output"]["recommendation"] == "advance"

    # And the funnel moved.
    detail = client.get(f"/api/jobs/{job['id']}").json()
    priya = next(a for a in detail["applicants"] if a["name"] == "Priya Raman")
    assert priya["stage"] == "recommended"
    assert priya["interview_score"] == 9


def test_funnel_counts_are_cumulative(client):
    """The funnel must report how far candidates got, not where they are parked.

    Stages are transitions: qualifying immediately triggers outreach, so no candidate
    is ever *at* "qualified". A point-in-time count showed 0 there and 0 at "applied"
    for any job that had been screened, which reads as broken data.
    """
    client.post("/api/jobs/sync")
    job = client.get("/api/jobs").json()[0]

    for a in client.get(f"/api/jobs/{job['id']}").json()["applicants"]:
        client.post(
            "/api/runs",
            json={"workflow_id": 1, "application_id": a["application_id"]},
        )

    funnel = client.get(f"/api/jobs/{job['id']}").json()["funnel"]

    # Everyone applied, and everyone who reached a later stage is still counted here.
    assert funnel["applied"] == job["applicant_count"]
    assert funnel["screened"] == job["applicant_count"]
    # Monotonically non-increasing down the funnel — that is what makes it a funnel.
    order = ["applied", "screened", "qualified", "contacted", "interviewed", "recommended"]
    values = [funnel[s] for s in order]
    assert values == sorted(values, reverse=True), values
