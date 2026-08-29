"""Normalized JobDiva interface.

Everything above this layer works with these models and never sees a raw JobDiva
payload. That containment matters more than usual here: most of the V2 API is untyped
in the Swagger, so the real shapes were discovered by calling it. Keeping the surprises
in one file means a shape change degrades one method instead of spreading through the
modules. The parsing primitives live in `shapes.py`, which documents each quirk.

`get_jobdiva()` returns the live adapter or the mock one based on `JOBDIVA_MODE`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.integrations.jobdiva import shapes
from app.integrations.jobdiva.client import JobDivaClient, JobDivaError, fmt_date

log = logging.getLogger(__name__)


class ApplicationRecord(BaseModel):
    candidate_id: int
    job_id: int | None = None
    applied_at: datetime | None = None
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    # Present on JobApplicantsDetail, which saves a lookup: it is the resume the
    # candidate actually applied with. Note it is a string, not an int.
    resume_id: str | None = None
    status: str = ""


class JobRecord(BaseModel):
    job_id: int
    title: str = ""
    description: str = ""
    skills: str = ""
    experience: int | None = None
    education: str = ""
    city: str | None = None
    state: str | None = None
    remote: bool = False
    security_clearance: bool = False
    status: str = ""


class WriteResult(BaseModel):
    """Outcome of one write to JobDiva.

    Carries the payload whether or not it was sent, so a suppressed write is a
    reviewable artifact rather than a silent no-op: the run timeline shows exactly
    what would have gone to the ATS.
    """

    op: str
    ok: bool = False
    # True when write mode is off. Distinct from a failure — nothing went wrong.
    suppressed: bool = False
    payload: dict[str, Any] = {}
    result: Any = None
    reason: str | None = None

    @property
    def status(self) -> str:
        if self.suppressed:
            return "suppressed"
        return "sent" if self.ok else "blocked"


class JobDivaAdapter(Protocol):
    async def fetch_new_applications(
        self, since: datetime, until: datetime, job_id: int | None = None
    ) -> list[ApplicationRecord]: ...
    async def get_job(self, job_id: int) -> JobRecord | None: ...
    async def get_resume_text(
        self, candidate_id: int, job_id: int | None = None, resume_id: str | None = None
    ) -> str: ...
    async def has_texting_consent(self, candidate_id: int | None) -> bool | None: ...
    async def post_note(
        self, *, candidate_id: int, job_id: int | None, note: str
    ) -> int | None: ...
    async def post_screener(
        self, *, candidate_id: int, job_id: int, answers: list[dict], note: str
    ) -> bool: ...


class LiveJobDiva:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = JobDivaClient(self.settings)
        self._reject_reasons: list[dict[str, str]] | None = None
        # The most recent note/screener write, so the module can report it alongside
        # the writes that return a WriteResult directly.
        self.last_write: WriteResult | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def fetch_new_applications(
        self, since: datetime, until: datetime, job_id: int | None = None
    ) -> list[ApplicationRecord]:
        """List applications, preferring the job-scoped endpoint.

        `JobApplicantsDetail` is better than the date-range `CandidateApplicationRecords`
        for our purposes on three counts: it is scoped to the job we care about, it
        already carries `RESUMEID` (so the resume-text lookup drops from two calls to
        one), and it sits on a separate quota — the date-range endpoint's quota was
        exhausted in this tenant while this one kept serving.

        Date filtering is applied client-side, since this endpoint takes no date range.
        """
        if job_id is not None:
            records = await self._applicants_for_job(job_id)
            if records:
                # Normalise all three sides. The poller's watermark comes back from
                # SQLite naive while JobDiva's timestamps are made aware, and mixing
                # them raises — a failure that only shows on the automatic path,
                # because manual syncs pass aware datetimes throughout.
                lo, hi = _aware(since), _aware(until)
                return [
                    r for r in records
                    if r.applied_at is None or lo <= _aware(r.applied_at) <= hi
                ]

        return await self._applications_by_date(since, until, job_id)

    async def _applicants_for_job(self, job_id: int) -> list[ApplicationRecord]:
        try:
            body = await self.client.get(
                "/apiv2/bi/JobApplicantsDetail", jobId=job_id
            )
        except JobDivaError as exc:
            log.error("JobApplicantsDetail(%s) failed: %s", job_id, exc)
            return []

        out = []
        for row in shapes.rows(body):
            candidate_id = shapes.integer(row, "CANDIDATEID")
            if candidate_id is None:
                continue
            out.append(
                ApplicationRecord(
                    candidate_id=candidate_id,
                    job_id=shapes.integer(row, "JOBID") or job_id,
                    applied_at=shapes.timestamp(row, "DATEAPPLIED", "ACTIONDATE"),
                    first_name=shapes.text(row, "FIRSTNAME"),
                    last_name=shapes.text(row, "LASTNAME"),
                    email=shapes.text(row, "EMAIL") or None,
                    resume_id=shapes.text(row, "RESUMEID") or None,
                    status=shapes.text(row, "STATUS", "ACTION"),
                )
            )
        log.info("fetched %s applicants for job %s", len(out), job_id)
        return out

    async def _applications_by_date(
        self, since: datetime, until: datetime, job_id: int | None
    ) -> list[ApplicationRecord]:
        """Fallback: the date-range stream, when no job is specified."""
        try:
            body = await self.client.get(
                "/apiv2/bi/CandidateApplicationRecords",
                fromDate=fmt_date(since),
                toDate=fmt_date(until),
                pageNumber=1,
                pageSize=200,
            )
        except JobDivaError as exc:
            log.error("CandidateApplicationRecords failed: %s", exc)
            return []

        out = []
        for row in shapes.rows(body):
            candidate_id = shapes.integer(row, "CANDIDATEID")
            if candidate_id is None:
                continue
            row_job = shapes.integer(row, "JOBID")
            if job_id is not None and row_job is not None and row_job != job_id:
                continue
            out.append(
                ApplicationRecord(
                    candidate_id=candidate_id,
                    job_id=row_job,
                    applied_at=shapes.timestamp(row, "DATEAPPLIED", "ACTIONDATE"),
                    first_name=shapes.text(row, "FIRSTNAME"),
                    last_name=shapes.text(row, "LASTNAME"),
                    email=shapes.text(row, "EMAIL") or None,
                    resume_id=shapes.text(row, "RESUMEID") or None,
                    status=shapes.text(row, "STATUS", "ACTION"),
                )
            )
        return out


    # -- batched reads -------------------------------------------------------
    #
    # These endpoints all accept arrays, and calling them one candidate at a time is
    # the difference between ~8 requests and ~400 for a job with 200 applicants — on
    # an API that has already returned 429 on this tenant. The singular methods below
    # delegate here so there is one implementation.
    #
    # Chunked at 50: the spec declares array parameters without a `collectionFormat`
    # and states no maximum, so the real limit is unknown and chunking defensively is
    # the only safe choice.

    CHUNK = 50

    async def get_contact_details_many(
        self, candidate_ids: list[int]
    ) -> dict[int, dict[str, str]]:
        """Phone, email and location for many candidates."""
        out: dict[int, dict[str, str]] = {}
        for chunk in _chunks(candidate_ids, self.CHUNK):
            try:
                body = await self.client.get(
                    "/apiv2/bi/CandidatesDetail", candidateIds=chunk
                )
            except JobDivaError as exc:
                log.warning("CandidatesDetail batch of %s failed: %s", len(chunk), exc)
                continue
            for row in shapes.rows(body):
                cid = shapes.integer(row, "ID", "CANDIDATEID")
                if cid is None:
                    continue
                out[cid] = {
                    "phone": (
                        shapes.text(row, "CELLPHONE")
                        or shapes.text(row, "PHONE1")
                        or shapes.text(row, "HOMEPHONE")
                    ),
                    "email": shapes.text(row, "EMAIL"),
                    "city": shapes.text(row, "CITY"),
                    "state": shapes.text(row, "STATE"),
                }
        return out

    async def get_resume_texts(self, resume_ids: list[str]) -> dict[str, str]:
        """Resume text for many resume ids, keyed by id.

        Resume ids are strings like "19535651872847_2950_11", not integers.
        """
        out: dict[str, str] = {}
        for chunk in _chunks(resume_ids, self.CHUNK):
            try:
                body = await self.client.get(
                    "/apiv2/bi/ResumesTextDetail", resumeIds=chunk
                )
            except JobDivaError as exc:
                log.warning("ResumesTextDetail batch of %s failed: %s", len(chunk), exc)
                continue
            for row in shapes.rows(body):
                rid = shapes.text(row, "GLOBAL_ID", "RESUMEID", "ID")
                text = shapes.text(row, "PLAINTEXT", "RESUMETEXT", "TEXT")
                if rid and text:
                    out[rid] = text
        return out

    async def texting_consent_many(
        self, candidate_ids: list[int]
    ) -> dict[int, bool | None]:
        """Consent per candidate. A missing entry means unknown, not refused."""
        out: dict[int, bool | None] = {}
        for chunk in _chunks(candidate_ids, self.CHUNK):
            try:
                body = await self.client.get(
                    "/apiv2/bi/CandidatesTextingConsent", candidateIds=chunk
                )
            except JobDivaError as exc:
                log.warning("texting consent batch failed: %s", exc)
                continue
            for row in shapes.rows(body):
                cid = shapes.integer(row, "CANDIDATEID", "ID")
                if cid is None:
                    continue
                out[cid] = _consent_of(row)
        return out

    async def get_contact_details(self, candidate_id: int) -> dict[str, str]:
        """Phone and location, which the applicant list does not carry."""
        return (await self.get_contact_details_many([candidate_id])).get(candidate_id, {})

    async def get_job(self, job_id: int) -> JobRecord | None:
        try:
            body = await self.client.get("/apiv2/bi/JobDetail", jobId=job_id)
        except JobDivaError as exc:
            log.error("JobDetail(%s) failed: %s", job_id, exc)
            return None

        found = shapes.rows(body)
        if not found:
            return None
        row = found[0]

        # SKILLS is frequently the literal string "Null" in real data, and there is no
        # experience field at all — the rubric extracts years from the description.
        return JobRecord(
            job_id=job_id,
            title=shapes.text(row, "JOBTITLE", "POSTING_TITLE", "TITLE"),
            description=shapes.plain_text(
                shapes.text(row, "JOBDESCRIPTION")
                or shapes.text(row, "POSTINGDESCRIPTION")
            ),
            skills=", ".join(shapes.skills(shapes.text(row, "SKILLS"))),
            education=shapes.text(row, "REQUIRED_DEGREE", "CRITERIA_DEGREE"),
            city=shapes.text(row, "CITY", "POSTING_CITY") or None,
            state=shapes.text(row, "STATE", "POSTING_STATE") or None,
            remote=shapes.text(row, "ONSITE_REMOTE").lower() != "onsite",
            security_clearance=shapes.boolean(row, "SECURITY_CLEARANCE"),
            status=shapes.text(row, "JOBSTATUS"),
        )

    async def get_resume_text(
        self, candidate_id: int, job_id: int | None = None, resume_id: str | None = None
    ) -> str:
        """Resume plain text.

        When the applicant list gave us a resume id we go straight to the text — that
        is both one call instead of two, and the resume they actually applied with
        rather than whatever they uploaded most recently.
        """
        if resume_id:
            if text := await self._resume_text(resume_id):
                return text

        try:
            body = await self.client.get(
                "/apiv2/bi/CandidatesResumesDetail", candidateIds=[candidate_id]
            )
        except JobDivaError as exc:
            log.error("resume lookup for candidate %s failed: %s", candidate_id, exc)
            return ""

        for row in shapes.rows(body):
            # Resume ids are strings like "19535651872847_2950_11", not integers.
            rid = shapes.text(row, "RESUMEID", "GLOBAL_ID", "ID")
            if rid and (text := await self._resume_text(rid)):
                return text
        return ""

    async def _resume_text(self, resume_id: str) -> str:
        try:
            body = await self.client.get(
                "/apiv2/bi/ResumesTextDetail", resumeIds=[resume_id]
            )
        except JobDivaError as exc:
            log.warning("ResumesTextDetail(%s) failed: %s", resume_id, exc)
            return ""
        for row in shapes.rows(body):
            if text := shapes.text(row, "PLAINTEXT", "RESUMETEXT", "TEXT"):
                return text
        return ""

    async def has_texting_consent(self, candidate_id: int | None) -> bool | None:
        """None means unknown — the caller decides whether to proceed."""
        if candidate_id is None:
            return None
        try:
            body = await self.client.get(
                "/apiv2/bi/CandidatesTextingConsent", candidateIds=[candidate_id]
            )
        except JobDivaError as exc:
            log.warning("texting consent lookup failed: %s", exc)
            return None

        for row in shapes.rows(body):
            return _consent_of(row)
        return None

    async def post_note(
        self, *, candidate_id: int, job_id: int | None, note: str
    ) -> int | None:
        payload: dict[str, Any] = {"candidateid": candidate_id, "note": note}
        if job_id:
            payload["link2AnOpenJob"] = job_id
        if self.settings.jobdiva_recruiter_id:
            payload["recruiterid"] = self.settings.jobdiva_recruiter_id

        # Routed through the same guard as every other write. Notes are the most
        # harmless thing we write, but "harmless" is not the criterion — the guard is
        # only trustworthy if nothing bypasses it.
        result = await self._write(
            "createCandidateNote", "POST", "/apiv2/jobdiva/createCandidateNote",
            json=payload,
        )
        self.last_write = result
        if not result.ok or result.suppressed:
            return None
        try:
            return int(str(result.result).strip())
        except (TypeError, ValueError):
            return None

    async def post_screener(
        self, *, candidate_id: int, job_id: int, answers: list[dict], note: str
    ) -> bool:
        payload = {
            "candidateId": candidate_id,
            "jobId": job_id,
            "recruiterId": self.settings.jobdiva_recruiter_id or 0,
            "note": note,
            "screenerAnswers": answers,
        }
        result = await self._write(
            "createOrUpdateCandidateScreener", "POST",
            "/apiv2/jobdiva/createOrUpdateCandidateScreener", json=payload,
        )
        self.last_write = result
        if result.suppressed:
            return False
        if result.ok:
            return True
        try:
            raise JobDivaError(result.reason or "screener write failed")
        except JobDivaError as exc:
            # "The screening module is off" is a tenant configuration state, not a
            # bug: the endpoint exists but the feature is disabled for this account.
            # Report it distinctly so it is not chased as an integration failure —
            # the summary note still carries the results.
            if "screening module is off" in str(exc).lower():
                log.warning(
                    "JobDiva screening module is disabled for this tenant — "
                    "interview results will go to the candidate note only"
                )
            else:
                log.error("createOrUpdateCandidateScreener failed: %s", exc)
            return False


    # -- writes ------------------------------------------------------------
    #
    # Everything that mutates JobDiva goes through `_write`, which is a no-op unless
    # JOBDIVA_WRITE_MODE=live. Reads stay live regardless, so the whole pipeline can
    # be exercised against real data without touching anyone's work queue.

    async def _write(
        self,
        op: str,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> WriteResult:
        payload = json or params or {}
        if self.settings.jobdiva_write_mode != "live":
            log.info("[write suppressed] %s -> %s %s", op, path, payload)
            return WriteResult(op=op, ok=True, suppressed=True, payload=payload)

        try:
            result = await self.client.request(method, path, json=json, params=params)
        except JobDivaError as exc:
            log.error("%s failed: %s", op, exc)
            return WriteResult(op=op, ok=False, payload=payload, reason=str(exc))
        return WriteResult(op=op, ok=True, payload=payload, result=result)

    async def create_submittal(
        self,
        *,
        candidate_id: int,
        job_id: int,
        notes: str = "",
        recruiter_id: int | None = None,
    ) -> WriteResult:
        """Create the pipeline record that makes a candidate a real submittal.

        `status` is deliberately omitted. The field is a free-text string with no
        vocabulary anywhere — the spec declares no enum, and this tenant's
        PipelineStages and existing submittal statuses are both empty. Sending a
        guessed value risks writing something the ATS does not recognise; omitting it
        lets JobDiva apply its own default, which we then read back.
        """
        body: dict[str, Any] = {
            "candidateid": candidate_id,
            "jobid": job_id,
            "submittaldate": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        if notes:
            body["internalnotes"] = notes[:4000]
        if rid := (recruiter_id or self.settings.jobdiva_recruiter_id):
            body["recruitedbyid"] = rid
        return await self._write(
            "createSubmittal", "POST", "/apiv2/jobdiva/createSubmittal", json=body
        )

    async def update_submittal(
        self,
        *,
        submittal_id: int,
        interview_date: datetime | None = None,
        notes: str = "",
        status: str | None = None,
    ) -> WriteResult:
        """Advance an existing submittal.

        Note the casing inconsistency in JobDiva's own models: createSubmittal uses
        `billrateCurrency` while updateSubmittal uses `billRateCurrency`. We touch
        neither, but the trap is worth recording for whoever extends this.
        """
        body: dict[str, Any] = {"submittalid": submittal_id}
        if interview_date:
            body["interviewdate"] = interview_date.replace(tzinfo=None).isoformat()
        if notes:
            body["internalnotes"] = notes[:4000]
        if status:
            body["status"] = status
        return await self._write(
            "updateSubmittal", "POST", "/apiv2/jobdiva/updateSubmittal", json=body
        )

    async def create_job_application(
        self, *, candidate_id: int, job_id: int, resume_source: str = "Asendia"
    ) -> WriteResult:
        """Record that a candidate applied — the recruiter's manual push."""
        body = {
            "candidateid": candidate_id,
            "jobid": job_id,
            "dateapplied": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "resumesource": resume_source,
        }
        return await self._write(
            "createJobApplication",
            "POST",
            "/apiv2/jobdiva/createJobApplication",
            json=body,
        )

    async def update_texting_consent(
        self, *, candidate_id: int, phone: str, opt_in: bool, note: str = ""
    ) -> WriteResult:
        """Record a texting consent decision.

        `optType` is one of the few genuine enums in the whole spec.
        """
        body = {
            "candidateId": candidate_id,
            "phoneNumber": phone,
            "optType": "OPT_IN" if opt_in else "OPT_OUT",
            "note": note or "Recorded by the Asendia workflow engine",
        }
        return await self._write(
            "updateTextingOptInOut",
            "POST",
            "/apiv2/jobdiva/updateTextingOptInOut",
            json=body,
        )

    async def mark_interested(self, *, candidate_id: int, job_id: int) -> WriteResult:
        """Flag interest — a lighter relation than a submittal.

        A GET that mutates, and it takes query parameters rather than a body.
        """
        return await self._write(
            "MarkCandidateAsInterested",
            "GET",
            "/apiv2/jobdiva/MarkCandidateAsInterested",
            params={"candidateId": candidate_id, "jobId": job_id},
        )

    async def get_reject_reasons(self) -> list[dict[str, str]]:
        """The tenant's configured rejection reasons, cached for the process."""
        if self._reject_reasons is None:
            try:
                body = await self.client.get("/apiv2/getRejectReasons")
                self._reject_reasons = [
                    {"id": shapes.text(r, "ID"), "name": shapes.text(r, "NAME", "REASON")}
                    for r in shapes.rows(body)
                ]
            except JobDivaError as exc:
                log.warning("getRejectReasons failed: %s", exc)
                self._reject_reasons = []
        return self._reject_reasons

    async def reject_applicant(
        self, *, candidate_id: int, job_id: int, reason: str | None = None
    ) -> WriteResult:
        """Reject an application.

        Requires a `reasonId` drawn from the tenant's configured list. This tenant has
        none configured, so rather than sending an invalid id and getting an opaque
        500, we decline up front and say why — the same treatment given to the
        disabled screening module. A configuration gap should read as a configuration
        gap.
        """
        reasons = await self.get_reject_reasons()
        if not reasons:
            return WriteResult(
                op="rejectApplicant",
                ok=False,
                payload={"candidateId": candidate_id, "jobId": job_id},
                reason=(
                    "no reject reasons are configured on this JobDiva tenant — "
                    "an administrator must add them before rejections can be written back"
                ),
            )

        chosen = next(
            (r for r in reasons if reason and reason.lower() in r["name"].lower()),
            reasons[0],
        )
        return await self._write(
            "rejectApplicant",
            "POST",
            "/apiv2/jobdiva/rejectApplicant",
            params={
                "candidateId": candidate_id,
                "jobId": job_id,
                "reasonId": chosen["id"],
            },
        )


def _chunks(items: list, size: int):
    """Yield fixed-size slices, skipping duplicates and preserving order."""
    seen, unique = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    for i in range(0, len(unique), size):
        yield unique[i : i + size]


def _consent_of(row: dict) -> bool | None:
    """Read a consent flag. None means unknown — the caller decides."""
    for key in ("OPTOUT", "ISOPTOUT", "DONOTTEXT"):
        if shapes.find(row, key) is not None:
            return not shapes.boolean(row, key)
    for key in ("CONSENT", "OPTIN", "TEXTINGCONSENT"):
        if shapes.find(row, key) is not None:
            return shapes.boolean(row, key)
    return None


def _aware(dt: datetime) -> datetime:
    """JobDiva returns naive timestamps; compare them in UTC."""
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def get_jobdiva(settings: Settings | None = None) -> JobDivaAdapter:
    s = settings or get_settings()
    if s.jobdiva_mode == "live":
        return LiveJobDiva(s)
    from app.integrations.jobdiva.mock import MockJobDiva

    return MockJobDiva()
