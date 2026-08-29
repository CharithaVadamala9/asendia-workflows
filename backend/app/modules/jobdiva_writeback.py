"""Write results back into JobDiva.

Three kinds of write, each doing a different job:

  1. **Annotation** — `createCandidateNote` with `link2AnOpenJob` puts a human-readable
     summary where recruiters already look, and `createOrUpdateCandidateScreener`
     records the interview as structured Q&A when the tenant has screening enabled.
  2. **Pipeline** — `createSubmittal` then `updateSubmittal` are what actually *move* a
     candidate. Without them we can annotate someone but never advance them.
  3. **Compliance** — `updateTextingOptInOut` records the consent decision behind an
     SMS we sent.

Two rules govern all of it.

**A failed write never fails the run.** The interview happened and is recorded on our
side; losing the mirror to JobDiva is a degradation, not a reason to discard completed
work. Every attempt is reported in `writes[]` with its payload.

**Writes are suppressed unless `JOBDIVA_WRITE_MODE=live`.** Reads stay live regardless,
so the whole pipeline runs against real data while a suppressed write records exactly
what it would have sent. Creating a submittal puts a record in a real recruiter's
queue, so mutation is opt-in rather than a side effect of live credentials.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register
from app.integrations.jobdiva.adapter import WriteResult, get_jobdiva

log = logging.getLogger(__name__)


class Config(BaseModel):
    post_note: bool = Field(
        default=True,
        title="Post a summary note",
        description="Human-readable summary attached to the candidate and the job.",
    )
    post_screener: bool = Field(
        default=True,
        title="Write structured screener answers",
        description="Records interview Q&A in JobDiva's candidate screener. Requires "
        "the screening module to be enabled on the tenant.",
    )
    create_submittal: bool = Field(
        default=True,
        title="Create a submittal for qualified candidates",
        description="Creates the pipeline record that makes a candidate a real "
        "submittal in JobDiva. Skipped for candidates below the score threshold.",
    )
    update_submittal: bool = Field(
        default=True,
        title="Advance the submittal after the interview",
        description="Sets the interview date and notes on the submittal created above.",
    )
    mark_interested: bool = Field(
        default=False,
        title="Flag the candidate as interested",
        description="A lighter signal than a submittal. Off by default to avoid two "
        "overlapping records for the same candidate.",
    )
    record_consent: bool = Field(
        default=True,
        title="Record texting consent",
        description="Logs an OPT_IN against the number we texted, so the consent trail "
        "lives in the ATS rather than only in our logs.",
    )
    reject_applicant: bool = Field(
        default=False,
        title="Reject disqualified applicants in JobDiva",
        description="Off by default — rejection is a human decision. Also requires "
        "reject reasons to be configured on the tenant.",
    )
    include_transcript: bool = Field(
        default=False,
        title="Include the full transcript in the note",
        description="Off by default; transcripts are long and the summary is usually "
        "what a recruiter wants.",
    )


class Output(BaseModel):
    note_id: int | None = None
    submittal_id: int | None = None
    writes: list[dict] = []
    write_mode: str = "suppressed"
    # Convenience flags the UI reads without walking `writes`.
    note_written: bool = False
    screener_written: bool = False


class JobDivaWriteback(BaseModule):
    id = "note_posting"
    name = "JobDiva Write-back"
    description = (
        "Writes screening and interview results back to JobDiva: a summary note, "
        "structured screener answers, and the submittal that moves the candidate "
        "through the pipeline."
    )
    category = ModuleCategory.ACTION
    config_model = Config
    output_model = Output

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        candidate_id = ctx.candidate.get("jobdiva_id")
        job_id = ctx.job.get("jobdiva_id")
        if not candidate_id:
            return StepResult.fail("candidate has no JobDiva id to write back to")

        report = _find(ctx, "headline", "overall_score")
        interview = _find(ctx, "transcript", "interview_score")
        screening = _find(ctx, "breakdown", "score")
        sms = _find(ctx, "provider", "delivered")

        jd = get_jobdiva()
        from app.config import get_settings

        write_mode = get_settings().jobdiva_write_mode
        writes: list[WriteResult] = []
        note_id: int | None = None
        submittal_id: int | None = None

        if ctx.dry_run:
            return StepResult.ok(
                writes=[],
                write_mode="dry_run",
                note_written=False,
                screener_written=False,
            )

        qualified = bool(screening.get("qualified"))
        summary = report.get("narrative") or report.get("headline") or ""

        # --- pipeline: create before update, id persisted on the Application -----
        if config.create_submittal and qualified and job_id:
            result = await _guard(
                jd.create_submittal(
                    candidate_id=candidate_id, job_id=job_id, notes=summary
                )
            )
            writes.append(result)
            submittal_id = _as_int(result.result)

        if config.mark_interested and qualified and job_id:
            writes.append(
                await _guard(jd.mark_interested(candidate_id=candidate_id, job_id=job_id))
            )

        # Fall back to an id from a previous run against the same application, which
        # the executor persisted. Returning the new id in `output` is what lets it do
        # so — modules never write to the database themselves.
        submittal_id = submittal_id or ctx.application.get("jobdiva_submittal_id")
        if config.update_submittal and submittal_id and interview:
            writes.append(
                await _guard(
                    jd.update_submittal(
                        submittal_id=submittal_id,
                        interview_date=datetime.now(UTC),
                        notes=_interview_summary(interview),
                    )
                )
            )

        # --- compliance ----------------------------------------------------------
        if config.record_consent and sms.get("delivered") and sms.get("to"):
            writes.append(
                await _guard(
                    jd.update_texting_consent(
                        candidate_id=candidate_id,
                        phone=str(sms["to"]),
                        opt_in=True,
                        note="Consent recorded after automated screening outreach",
                    )
                )
            )

        if config.reject_applicant and not qualified and job_id:
            writes.append(
                await _guard(
                    jd.reject_applicant(
                        candidate_id=candidate_id,
                        job_id=job_id,
                        reason=screening.get("reason"),
                    )
                )
            )

        # --- annotation ----------------------------------------------------------
        screener_written = False
        if config.post_screener and job_id and interview:
            try:
                screener_written = await jd.post_screener(
                    candidate_id=candidate_id,
                    job_id=job_id,
                    answers=_screener_answers(interview),
                    note=summary,
                )
            except Exception as exc:  # noqa: BLE001 — never fail a run on write-back
                log.exception("screener write-back failed")
                writes.append(
                    WriteResult(op="createOrUpdateCandidateScreener", ok=False, reason=str(exc))
                )
            else:
                if w := getattr(jd, "last_write", None):
                    writes.append(w)

        if config.post_note:
            body = _compose_note(ctx, report, screening, interview, config)
            try:
                note_id = await jd.post_note(
                    candidate_id=candidate_id, job_id=job_id, note=body
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("note write-back failed")
                writes.append(WriteResult(op="createCandidateNote", ok=False, reason=str(exc)))
            else:
                if w := getattr(jd, "last_write", None):
                    writes.append(w)

        return StepResult.ok(
            note_id=note_id,
            submittal_id=submittal_id,
            write_mode=write_mode,
            writes=[w.model_dump() | {"status": w.status} for w in writes],
            note_written=note_id is not None,
            screener_written=screener_written,
        )


async def _guard(coro) -> WriteResult:
    """Turn any write exception into a reported failure rather than a run failure."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        log.exception("JobDiva write failed")
        return WriteResult(op="unknown", ok=False, reason=str(exc))


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _interview_summary(interview: dict) -> str:
    bits = []
    if score := interview.get("interview_score"):
        bits.append(f"AI phone screen: {score}/10")
    if rec := interview.get("recommendation"):
        bits.append(f"recommendation: {rec}")
    if rationale := interview.get("rationale"):
        bits.append(rationale)
    return " — ".join(bits)[:4000]


def _screener_answers(interview: dict) -> list[dict]:
    """Map the interview's structured output onto JobDiva screener answers.

    `questionId` 0 means "not mapped to a configured question". This tenant's
    ScreenerQuestions list is empty, so there is nothing to map onto yet; once
    questions exist they can be matched by text.
    """
    answers = [
        {"questionId": 0, "answer": a.get("answer", ""), "note": a.get("question", "")}
        for a in (interview.get("answers") or [])
    ]
    for label, key in (
        ("Availability", "availability"),
        ("Rate expectation", "rate_expectation"),
        ("Work authorization", "work_authorization"),
    ):
        if value := interview.get(key):
            answers.append({"questionId": 0, "answer": str(value), "note": label})
    return answers


def _compose_note(
    ctx: RunContext, report: dict, screening: dict, interview: dict, config: Config
) -> str:
    lines = [f"AI SCREENING — {ctx.job.get('title', '')}", "", report.get("headline", ""), ""]

    if (score := screening.get("score")) is not None:
        lines.append(f"Resume score: {score}/100 ({screening.get('summary', '')})")
        for c in (screening.get("breakdown") or {}).get("criteria", []):
            lines.append(f"  - {c.get('label')}: {c.get('evidence')}")
        lines.append("")

    if interview:
        if s := interview.get("interview_score"):
            lines.append(f"Phone interview: {s}/10 — {interview.get('recommendation', '')}")
        if rationale := interview.get("rationale"):
            lines.append(rationale)
        for label, key in (("Availability", "availability"), ("Rate", "rate_expectation")):
            if v := interview.get(key):
                lines.append(f"  {label}: {v}")
        if url := interview.get("recording_url"):
            lines.append(f"  Recording: {url}")
        lines.append("")

    if strengths := report.get("strengths"):
        lines += ["Strengths:"] + [f"  + {s}" for s in strengths] + [""]
    if concerns := report.get("concerns"):
        lines += ["Concerns:"] + [f"  - {c}" for c in concerns] + [""]

    if config.include_transcript and (t := interview.get("transcript")):
        lines += ["--- TRANSCRIPT ---", t]

    lines.append("Generated automatically by the Asendia workflow engine.")
    return "\n".join(lines)


def _find(ctx: RunContext, *required_keys: str) -> dict:
    for step in ctx.steps.values():
        out = step.get("output") or {}
        if any(k in out for k in required_keys):
            return out
    return {}


register(JobDivaWriteback())
