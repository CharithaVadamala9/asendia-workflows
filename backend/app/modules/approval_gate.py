"""Recruiter approval gate — human-in-the-loop, on the same machinery as the AI call.

This module exists to prove a point about the engine as much as to serve a workflow:
waiting for a recruiter to click a button and waiting for a phone call to finish are
the same problem, and both are solved by returning SUSPENDED. Nothing here is special-
cased for approvals.

Disabled by default so the demo runs unattended, but a staffing client will ask about
the human checkpoint, and the answer is a config toggle rather than a rewrite.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engine.base import BaseModule, ModuleCategory, StepResult
from app.engine.context import RunContext
from app.engine.registry import register


class Config(BaseModel):
    enabled: bool = Field(
        default=False,
        title="Require recruiter approval",
        description="When on, the workflow pauses here until a recruiter approves or "
        "rejects the candidate from the dashboard.",
    )
    note: str = Field(
        default="",
        title="Note for the recruiter",
        description="Shown on the approval card. Supports {{...}} placeholders.",
    )


class Output(BaseModel):
    approved: bool
    skipped: bool = False
    decided_by: str | None = None
    comment: str | None = None


class ApprovalGate(BaseModule):
    id = "approval_gate"
    name = "Recruiter Approval"
    description = (
        "Pauses the workflow until a recruiter approves or rejects the candidate. "
        "Uses the same suspend/resume mechanism as the AI phone call."
    )
    category = ModuleCategory.ACTION
    config_model = Config
    output_model = Output
    is_async = True

    async def run(self, ctx: RunContext, config: Config) -> StepResult:
        if not config.enabled:
            return StepResult.ok(approved=True, skipped=True)
        return StepResult.suspend(
            external_ref=f"approval:{ctx.run_id}", note=config.note, approved=False
        )

    async def resume(self, ctx: RunContext, config: Config, payload: dict) -> StepResult:
        approved = bool(payload.get("approved"))
        if not approved:
            # A rejection is a decision, not an error — it ends the run cleanly and
            # the remaining outreach steps never fire.
            return StepResult.fail(
                payload.get("comment") or "rejected by recruiter",
            )
        return StepResult.ok(
            approved=True,
            decided_by=payload.get("decided_by"),
            comment=payload.get("comment"),
        )


register(ApprovalGate())
