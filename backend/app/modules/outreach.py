"""Candidate outreach — SMS and email.

Both are templated with the same `{{...}}` interpolation the rest of the engine uses,
so a recruiter edits message copy in the workflow builder rather than in code.

The SMS module checks JobDiva's stored texting consent before sending. That is a
compliance requirement (TCPA), not a nicety — and it is the kind of thing a one-off
automation script quietly skips.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register
from app.integrations.messaging import DeliveryError, send_email, send_sms

log = logging.getLogger(__name__)

DEFAULT_SMS = (
    "Hi {{candidate.first_name}}, this is Asendia about the {{job.title}} role. "
    "Your background looks like a strong match. We'd like to run a short screening "
    "call — you'll get a call shortly. Reply STOP to opt out."
)


class SmsConfig(BaseModel):
    template: str = Field(
        default=DEFAULT_SMS,
        title="Message",
        description="Supports {{candidate.first_name}}, {{job.title}}, and any prior "
        "step output such as {{steps.screen.output.score}}.",
    )
    check_consent: bool = Field(
        default=True,
        title="Check texting consent first",
        description="Verifies the candidate has not opted out in JobDiva before sending.",
    )


class SmsOutput(BaseModel):
    to: str
    body: str
    provider: str
    # Carrier-confirmed delivery. Distinct from `accepted`: a provider returning 2xx
    # has taken the message, not delivered it.
    delivered: bool
    accepted: bool = False
    status: str | None = None
    reason: str | None = None
    message_id: str | None = None


class SmsOutreach(BaseModule):
    id = "sms_outreach"
    name = "SMS Outreach"
    description = (
        "Texts the candidate a templated message. Checks JobDiva texting consent "
        "before sending."
    )
    category = ModuleCategory.ACTION
    config_model = SmsConfig
    output_model = SmsOutput

    async def run(self, ctx: RunContext, config: SmsConfig) -> StepResult:
        phone = ctx.candidate.get("phone")
        if not phone:
            # Not a failure: the candidate has been scored and that result still
            # matters. JobDiva simply has no number on file, which is a gap for a
            # recruiter to fill, not a reason to discard the screening.
            return StepResult.ok(
                to="", body="", provider="none", delivered=False,
                reason="no phone number on file in JobDiva",
            )

        if config.check_consent:
            from app.integrations.jobdiva.adapter import get_jobdiva

            consented = await get_jobdiva().has_texting_consent(
                ctx.candidate.get("jobdiva_id")
            )
            if consented is False:
                # Not a failure — a correct, compliant outcome. The run continues to
                # the call, which is a separate consent basis.
                return StepResult.ok(
                    to=phone,
                    body="",
                    provider="none",
                    delivered=False,
                    reason="candidate has opted out of texting in JobDiva",
                )

        body = ctx.render(config.template)
        try:
            result = await send_sms(to=phone, body=body, dry_run=ctx.dry_run)
        except DeliveryError as exc:
            return StepResult.fail(str(exc))
        return StepResult.ok(**result)


class EmailConfig(BaseModel):
    subject: str = Field(
        default="Your application for {{job.title}}", title="Subject"
    )
    body: str = Field(
        default=(
            "Hi {{candidate.first_name}},\n\n"
            "Thanks for your interest in the {{job.title}} role. We've reviewed your "
            "background and would like to move forward with a short screening "
            "conversation.\n\n"
            "Best,\nThe Asendia Team"
        ),
        title="Message body",
    )
    to_recruiter: bool = Field(
        default=False,
        title="Send to the recruiter instead",
        description="Use for internal notifications rather than candidate outreach.",
    )
    recruiter_email: str = Field(default="", title="Recruiter address")


class EmailOutput(BaseModel):
    to: str
    subject: str
    body: str
    delivered: bool
    reason: str | None = None
    message_id: str | None = None


class EmailNotification(BaseModule):
    id = "email_notification"
    name = "Email Notification"
    description = "Sends a templated email to the candidate or to the recruiter."
    category = ModuleCategory.ACTION
    config_model = EmailConfig
    output_model = EmailOutput

    async def run(self, ctx: RunContext, config: EmailConfig) -> StepResult:
        if config.to_recruiter:
            to, name = config.recruiter_email, "Recruiter"
        else:
            to, name = ctx.candidate.get("email", ""), ctx.candidate.get("full_name", "")
        if not to:
            return StepResult.ok(
                to="", subject="", body="", delivered=False,
                reason="no email address on file in JobDiva",
            )

        try:
            result = await send_email(
                to=to,
                to_name=name,
                subject=ctx.render(config.subject),
                text=ctx.render(config.body),
                dry_run=ctx.dry_run,
            )
        except DeliveryError as exc:
            return StepResult.fail(str(exc))
        return StepResult.ok(**result)


register(SmsOutreach())
register(EmailNotification())
