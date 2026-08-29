"""AI phone interview — the asynchronous step the whole engine design exists for.

Two stages, because voice latency is a real constraint. Preparation happens before the
call, where there is no latency budget: one LLM call turns the job description, the
resume, and *the scoring breakdown we already computed* into two compressed briefs and
a set of tailored questions. The call itself then carries a compact prompt, so the
model responds fast — a laggy interviewer is immediately obvious to the person on the
phone.

Using the scoring breakdown is the part that makes this more than templating: we know
exactly which criteria were weak, so the interview probes those specific gaps.

`run()` returns SUSPENDED. The run halts with its state in the database and resumes
when VAPI posts the call report back.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register
from app.integrations.llm import LLMError, get_llm
from app.integrations.vapi import VapiClient, VapiError, parse_end_of_call

log = logging.getLogger(__name__)


class Config(BaseModel):
    max_duration_seconds: int = Field(
        default=420, ge=60, le=1800, title="Maximum call length"
    )
    question_count: int = Field(
        default=4, ge=1, le=8, title="Generated questions",
        description="How many job-specific questions to generate on top of the fixed block.",
    )
    max_interviews_per_job: int = Field(
        default=10,
        ge=0,
        le=500,
        title="Maximum interviews per job",
        description="Scoring every applicant is cheap; phoning every applicant is not. "
        "Once this many candidates on a job have been interviewed, later ones are "
        "skipped rather than called. Set 0 for no limit.",
    )
    ask_logistics: bool = Field(
        default=True,
        title="Ask standard logistics questions",
        description="Availability, rate expectation, work authorization, location. "
        "Keeps candidates comparable.",
    )


class InterviewPlan(BaseModel):
    """The preparation stage's output — persisted so the timeline shows *why* the AI
    asked what it asked."""

    candidate_brief: str = Field(
        description="~150 words: background, relevant experience, and gaps to probe."
    )
    role_brief: str = Field(description="~100 words: what this role actually needs.")
    questions: list[str] = Field(
        description="Job-specific questions, each probing a specific gap or claim."
    )
    rationale: list[str] = Field(
        default_factory=list,
        description="For each question, the evidence that prompted it.",
    )


class Output(BaseModel):
    call_id: str = ""
    skipped_reason: str | None = None
    plan: dict
    transcript: str = ""
    recording_url: str | None = None
    interview_score: float | None = None
    recommendation: str | None = None
    rationale: str | None = None
    strengths: list[str] = []
    concerns: list[str] = []
    answers: list[dict] = []


class AIPhoneCall(BaseModule):
    id = "ai_phone_call"
    name = "AI Phone Interview"
    description = (
        "Places an outbound AI voice call that interviews the candidate about their "
        "own experience against this specific role, then returns a transcript and a "
        "structured evaluation."
    )
    category = ModuleCategory.AI
    config_model = Config
    output_model = Output
    is_async = True

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        phone = ctx.candidate.get("phone")
        if not phone:
            # Same reasoning as the SMS step: no number is a data gap, and the
            # assessment report should still be produced and written back.
            return StepResult.ok(
                call_id="", plan={},
                skipped_reason="no phone number on file — cannot place the interview call",
            )

        # A cap, not an error: the candidate was screened and scored, we simply are
        # not phoning everyone. Recorded so the recruiter can see why and call
        # manually if they want to.
        cap = config.max_interviews_per_job
        done = int(ctx.job.get("interviews_started") or 0)
        if cap and done >= cap:
            return StepResult.ok(
                call_id="",
                plan={},
                skipped_reason=(
                    f"interview cap reached for this job ({done}/{cap}) — "
                    "score recorded, no call placed"
                ),
            )

        plan = await self._prepare(ctx, config)
        system_prompt = self._build_prompt(ctx, config, plan)
        name = ctx.candidate.get("first_name") or ctx.candidate.get("full_name", "there")

        client = VapiClient()
        try:
            call = await client.place_call(
                phone_number=phone,
                candidate_name=ctx.candidate.get("full_name", ""),
                system_prompt=system_prompt,
                first_message=(
                    f"Hi, is this {name}? This is Asendia's automated screening call "
                    f"about the {ctx.job.get('title', 'role')} position. "
                    "Do you have about five minutes?"
                ),
                metadata={
                    "run_id": ctx.run_id,
                    "candidate_id": ctx.candidate.get("id"),
                    "job_id": ctx.job.get("id"),
                },
                max_duration_seconds=config.max_duration_seconds,
            )
        except VapiError as exc:
            return StepResult.fail(str(exc))

        # Hand off and stop. The webhook resumes us.
        return StepResult.suspend(
            external_ref=str(call.get("id")),
            call_id=str(call.get("id")),
            plan=plan.model_dump(),
        )

    async def resume(self, ctx: RunContext, config: Config, payload: dict) -> StepResult:
        """Called by the VAPI webhook with the end-of-call report."""
        parsed = parse_end_of_call(payload)
        if parsed.get("ended_reason") in ("assistant-error", "pipeline-error"):
            return StepResult.fail(f"call failed: {parsed['ended_reason']}")

        prior = ctx.steps.get("call", {}).get("output", {})
        return StepResult.ok(plan=prior.get("plan", {}), **parsed)

    # -- preparation --------------------------------------------------------

    async def _prepare(self, ctx: RunContext, config: Config) -> InterviewPlan:
        """Generate briefs and tailored questions from the resume, JD, and score."""
        screening = _find_screening_output(ctx)
        gaps = _describe_gaps(screening)
        resume = (ctx.candidate.get("resume_text") or "")[:10000]

        try:
            return await get_llm().complete_json(
                system=(
                    "You prepare a recruiter's phone screen. Produce briefs that are "
                    "compact enough to sit in a voice agent's system prompt, and "
                    "questions that probe THIS candidate's specific gaps against THIS "
                    "role. Never ask something the resume already answers clearly. "
                    "Each question must be answerable in under a minute of speech."
                ),
                prompt=(
                    f"ROLE: {ctx.job.get('title', '')}\n"
                    f"{(ctx.job.get('description') or '')[:4000]}\n\n"
                    f"SCREENING RESULT: {gaps}\n\n"
                    f"RESUME:\n{resume}\n\n"
                    f"Generate exactly {config.question_count} questions."
                ),
                schema=InterviewPlan,
                mock=_mock_plan(ctx, config, gaps),
                max_tokens=1500,
            )
        except LLMError as exc:
            log.warning("interview preparation degraded to defaults: %s", exc)
            return _mock_plan(ctx, config, gaps)

    def _build_prompt(self, ctx: RunContext, config: Config, plan: InterviewPlan) -> str:
        logistics = (
            "\nAlso confirm, briefly: their availability or notice period, their rate "
            "or salary expectation, their work authorization, and whether the role's "
            "location works for them."
            if config.ask_logistics
            else ""
        )
        questions = "\n".join(f"  {i}. {q}" for i, q in enumerate(plan.questions, 1))
        return f"""You are a professional recruiter conducting a brief phone screen for Asendia.

THE ROLE
{plan.role_brief}

THE CANDIDATE
{plan.candidate_brief}

QUESTIONS TO ASK
{questions}{logistics}

HOW TO CONDUCT THE CALL
- Be warm, concise, and human. This is a conversation, not an interrogation.
- Ask one question at a time and let them finish. Do not talk over them.
- Follow up briefly when an answer is vague, then move on. Do not belabor a point.
- If they cannot talk now, offer to call back and end politely.
- Never promise an offer, a salary, or a next step you have not been told to promise.
- Keep the whole call under {config.max_duration_seconds // 60} minutes.
- Close by thanking them and saying a recruiter will follow up.

If asked something you do not know, say you will have the recruiter follow up.
"""


def _find_screening_output(ctx: RunContext) -> dict:
    for step in ctx.steps.values():
        out = step.get("output") or {}
        if "breakdown" in out and "score" in out:
            return out
    return {}


def _describe_gaps(screening: dict) -> str:
    """Turn the score breakdown into a plain-language brief of what to probe."""
    if not screening:
        return "No prior screening available."

    parts = [f"Scored {screening.get('score', '?')}/100."]
    for c in (screening.get("breakdown") or {}).get("criteria", []):
        # Anything below 80% of its possible marks is worth a question.
        if c.get("normalized", 1) < 0.8:
            parts.append(f"WEAK — {c.get('label')}: {c.get('evidence')}")
    if concerns := _semantic_concerns(screening):
        parts.append("AI concerns: " + "; ".join(concerns))
    return "\n".join(parts)


def _semantic_concerns(screening: dict) -> list[str]:
    for c in (screening.get("breakdown") or {}).get("criteria", []):
        if c.get("key") == "semantic_fit":
            return (c.get("detail") or {}).get("concerns", [])
    return []


def _mock_plan(ctx: RunContext, config: Config, gaps: str) -> InterviewPlan:
    title = ctx.job.get("title", "the role")
    return InterviewPlan(
        candidate_brief=(
            f"{ctx.candidate.get('full_name', 'The candidate')} applied for {title}. "
            f"{gaps[:400]}"
        ),
        role_brief=f"{title}. {(ctx.job.get('description') or '')[:300]}",
        questions=[
            f"Tell me about your most relevant experience for {title}.",
            "Walk me through a recent project you're proud of and your specific role in it.",
            "What's the most challenging technical problem you've solved recently?",
            "What are you looking for in your next role?",
        ][: config.question_count],
        rationale=["Generated in mock mode — no AI preparation applied."],
    )


register(AIPhoneCall())
