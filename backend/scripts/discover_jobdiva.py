"""JobDiva shape-discovery spike.

60% of JobDiva's V2 endpoints (260 of 431) are declared in the Swagger spec as
`IBiData` — `{"type": "object"}` with no properties. Their real response shapes cannot
be known without calling them. This script calls the handful we depend on and writes
down what actually comes back.

Run it once credentials are in backend/.env:

    cd backend && .venv/bin/python -m scripts.discover_jobdiva

It answers exactly five questions and then stops:

  1. Does the Authorization header want the raw token or `Bearer <token>`?
  2. Does CandidateApplicationRecords carry a job id, or do we need a second call?
  3. Does JobDetail surface skills / experience / securityclearance?
     (This one gates structured job-side scoring.)
  4. What do CandidatesProfileDetail and ResumesTextDetail actually return?
  5. What are this tenant's ApiLimits?

Output: docs/jobdiva-shapes.md (structure only, committed) and
docs/jobdiva-shapes.raw.json (full payloads, gitignored — may contain candidate PII).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.integrations.jobdiva.client import JobDivaClient, JobDivaError, fmt_date

DOCS = Path(__file__).resolve().parents[2] / "docs"

# Fields we specifically hope to find, per endpoint. Discovering their absence is as
# valuable as discovering their presence — it is what selects the fallback path.
LOOKING_FOR = {
    "CandidateApplicationRecords": ["jobid", "jobId", "job_id", "candidateid", "dateapplied"],
    "JobDetail": ["skills", "excludedskills", "experience", "securityclearance", "description"],
    "CandidatesProfileDetail": ["skills", "education", "experience", "titles", "certifications"],
    "ResumesTextDetail": ["resumeid", "resumetext", "text"],
}


async def main() -> int:
    settings = get_settings()
    if not settings.jobdiva_client_id:
        print(
            "No JobDiva credentials found.\n"
            "  cp backend/.env.example backend/.env, then fill in\n"
            "  JOBDIVA_CLIENT_ID / JOBDIVA_USERNAME / JOBDIVA_PASSWORD",
            file=sys.stderr,
        )
        return 1

    client = JobDivaClient(settings)
    findings: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}

    # --- Q1: authenticate, and learn which header format works -----------------
    try:
        await client.authenticate()
        print("[ok] authenticated")
    except JobDivaError as exc:
        print(f"[FAIL] authentication: {exc}", file=sys.stderr)
        await client.aclose()
        return 2

    # --- Q5: rate limits (typed, so this also proves the header format) --------
    limits = await probe(client, findings, raw, "ApiLimits", "/apiv2/bi/ApiLimits")
    print(f"[ok] auth header format resolved to: {client.resolved_scheme!r}")

    # --- Q2: the ingestion cursor ---------------------------------------------
    now = datetime.now(UTC)
    apps = await probe(
        client,
        findings,
        raw,
        "CandidateApplicationRecords",
        "/apiv2/bi/CandidateApplicationRecords",
        fromDate=fmt_date(now - timedelta(days=90)),
        toDate=fmt_date(now),
        pageNumber=1,
        pageSize=5,
    )

    # Use a real candidate/job id from the applications feed where we can, so the
    # follow-up probes exercise data that actually exists in this tenant.
    candidate_id = _first_id(apps, ("candidateid", "candidateId", "CANDIDATEID", "id"))
    job_id = _first_id(apps, ("jobid", "jobId", "JOBID"))

    # --- Q3: job requirements (gates structured scoring) -----------------------
    if job_id:
        await probe(client, findings, raw, "JobDetail", "/apiv2/bi/JobDetail", jobId=job_id)
    else:
        # No application rows to borrow an id from — fall back to any open job.
        open_jobs = await probe(
            client, findings, raw, "OpenJobsList", "/apiv2/bi/OpenJobsList"
        )
        job_id = _first_id(open_jobs, ("jobid", "jobId", "JOBID", "id"))
        if job_id:
            await probe(
                client, findings, raw, "JobDetail", "/apiv2/bi/JobDetail", jobId=job_id
            )

    # --- Q4: candidate enrichment and resume text ------------------------------
    if candidate_id:
        await probe(
            client,
            findings,
            raw,
            "CandidatesProfileDetail",
            "/apiv2/bi/CandidatesProfileDetail",
            candidateIds=[candidate_id],
            extendExperience=True,
        )
        resumes = await probe(
            client,
            findings,
            raw,
            "CandidatesResumesDetail",
            "/apiv2/bi/CandidatesResumesDetail",
            candidateIds=[candidate_id],
        )
        resume_id = _first_id(resumes, ("resumeid", "resumeId", "RESUMEID", "id"))
        if resume_id:
            await probe(
                client,
                findings,
                raw,
                "ResumesTextDetail",
                "/apiv2/bi/ResumesTextDetail",
                resumeIds=[resume_id],
            )
        if job_id:
            # If this works it is strictly better: the resume they applied *with*,
            # not their latest, and it collapses two calls into one.
            await probe(
                client,
                findings,
                raw,
                "CandidateResumeSubmittedtoJob",
                "/apiv2/bi/CandidateResumeSubmittedtoJob",
                candidateId=candidate_id,
                jobId=job_id,
            )

    # --- Vocabulary we must never hardcode -------------------------------------
    for name, path in [
        ("PipelineStages", "/apiv2/bi/PipelineStages"),
        ("ActionTypeList", "/apiv2/bi/ActionTypeList"),
        ("getRejectReasons", "/apiv2/getRejectReasons"),
        ("ScreenerQuestions", "/apiv2/jobdiva/ScreenerQuestions"),
    ]:
        await probe(client, findings, raw, name, path)

    await client.aclose()
    _write_report(findings, raw, client.resolved_scheme, limits)
    print(f"\n[done] wrote {DOCS / 'jobdiva-shapes.md'}")
    return 0


async def probe(
    client: JobDivaClient,
    findings: list[dict],
    raw: dict,
    name: str,
    path: str,
    **params: Any,
) -> Any:
    """Call one endpoint and record its shape. Never raises — a failed probe is data."""
    try:
        body = await client.get(path, **params)
    except JobDivaError as exc:
        print(f"[fail] {name}: {exc}")
        findings.append({"name": name, "path": path, "params": params, "error": str(exc)})
        return None

    raw[name] = body
    shape = describe(body)
    found = _check_for(name, body)
    findings.append(
        {
            "name": name,
            "path": path,
            "params": params,
            "shape": shape,
            "keys": sorted(_all_keys(body))[:60],
            "looking_for": found,
            "count": len(body) if isinstance(body, list) else None,
        }
    )
    print(f"[ok] {name}: {shape}")
    if found:
        for field, present in found.items():
            print(f"        {'✓' if present else '✗'} {field}")
    return body


def describe(value: Any, depth: int = 0) -> str:
    """One-line structural summary, no values — safe to commit."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = ", ".join(
            f"{k}: {describe(v, depth + 1)}" for k, v in list(value.items())[:12]
        )
        more = ", ..." if len(value) > 12 else ""
        return "{" + inner + more + "}"
    if isinstance(value, list):
        return f"[{describe(value[0], depth + 1)}] x{len(value)}" if value else "[]"
    return type(value).__name__


def _all_keys(value: Any, acc: set[str] | None = None, depth: int = 0) -> set[str]:
    acc = acc if acc is not None else set()
    if depth > 4:
        return acc
    if isinstance(value, dict):
        for k, v in value.items():
            acc.add(k)
            _all_keys(v, acc, depth + 1)
    elif isinstance(value, list):
        for item in value[:3]:
            _all_keys(item, acc, depth + 1)
    return acc


def _check_for(name: str, body: Any) -> dict[str, bool]:
    wanted = LOOKING_FOR.get(name)
    if not wanted:
        return {}
    keys_lower = {k.lower() for k in _all_keys(body)}
    return {f: f.lower() in keys_lower for f in wanted}


def _first_id(body: Any, candidates: tuple[str, ...]) -> int | None:
    """Pull the first plausible id out of an unknown response shape."""
    rows = body if isinstance(body, list) else [body]
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        # Nested envelopes are common in BI responses — search one level down too.
        for container in [row, *(v for v in row.values() if isinstance(v, dict))]:
            for key in candidates:
                for actual, value in container.items():
                    if actual.lower() == key.lower() and isinstance(value, (int, str)):
                        try:
                            return int(value)
                        except (TypeError, ValueError):
                            continue
            nested = next(
                (v for v in container.values() if isinstance(v, list) and v), None
            )
            if nested:
                found = _first_id(nested, candidates)
                if found:
                    return found
    return None


def _write_report(
    findings: list[dict], raw: dict, scheme: str, limits: Any
) -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / "jobdiva-shapes.raw.json").write_text(json.dumps(raw, indent=2, default=str))

    lines = [
        "# JobDiva API — discovered response shapes",
        "",
        f"Probed {datetime.now(UTC).isoformat()} against `api.jobdiva.com`.",
        "",
        "The V2 Swagger declares 260 of 431 endpoints as `IBiData` — an object with no",
        "properties — so these shapes were discovered by calling the API, not read from",
        "the spec. Structure only; raw payloads are in the gitignored `.raw.json`.",
        "",
        "## Resolved unknowns",
        "",
        f"- **Auth header format:** `Authorization: {'<token>' if scheme == 'raw' else 'Bearer <token>'}` (`{scheme}`)",
    ]

    def finding(name: str) -> dict | None:
        return next((f for f in findings if f["name"] == name), None)

    apps = finding("CandidateApplicationRecords")
    if apps and "looking_for" in apps:
        has_job = any(
            v for k, v in apps["looking_for"].items() if k.lower().startswith("job")
        )
        lines.append(
            f"- **CandidateApplicationRecords carries a job id:** "
            f"{'yes — single-call ingestion' if has_job else 'NO — needs a second call to CandidatesApplicationsList'}"
        )

    job = finding("JobDetail")
    if job and "looking_for" in job:
        structured = [k for k, v in job["looking_for"].items() if v]
        missing = [k for k, v in job["looking_for"].items() if not v]
        lines.append(
            f"- **JobDetail structured requirements:** present: "
            f"{', '.join(f'`{k}`' for k in structured) or 'none'}"
            + (f" · missing: {', '.join(f'`{k}`' for k in missing)}" if missing else "")
        )
        lines.append(
            "  → "
            + (
                "structured job-side scoring is viable"
                if structured
                else "**fall back to LLM extraction from the job description**"
            )
        )

    if limits:
        lines += ["", "## Rate limits (this tenant)", "", "```json", json.dumps(limits, indent=2)[:2000], "```"]

    lines += ["", "## Endpoint shapes", ""]
    for f in findings:
        lines.append(f"### `{f['path']}`")
        if f.get("params"):
            lines.append(f"Params: `{f['params']}`")
        if "error" in f:
            lines += ["", f"**FAILED** — {f['error']}", ""]
            continue
        if f.get("count") is not None:
            lines.append(f"Rows: {f['count']}")
        lines += ["", "```", f["shape"][:1500], "```", ""]
        if f.get("keys"):
            lines += ["Keys seen: " + ", ".join(f"`{k}`" for k in f["keys"]), ""]

    (DOCS / "jobdiva-shapes.md").write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
