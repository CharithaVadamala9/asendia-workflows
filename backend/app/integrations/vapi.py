"""VAPI integration — outbound AI phone interviews.

Two details from VAPI's API shape the code here:

  - `analysisPlan.structuredDataPlan.enabled` defaults to **false**, and its
    `timeoutSeconds` defaults to **5**. Leave either alone and `structuredData` comes
    back empty, which looks exactly like a model failure but is not one.
  - The assistant is sent **inline** (a transient assistant) rather than referenced by
    id, so the system prompt can be built per candidate from their resume and the job.

Identity is threaded through `customer.externalId` and `assistant.metadata`, both of
which come back on the webhook — that is how a call result finds its suspended run.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class VapiError(RuntimeError):
    pass


# What we ask the model to extract from the conversation. Mirrors the shape the
# JobDiva screener write-back expects, so no translation layer is needed.
INTERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "description": "One entry per question asked, in the order asked.",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string", "description": "The candidate's answer, summarized faithfully."},
                },
            },
        },
        "availability": {"type": "string", "description": "Start date or notice period as stated."},
        "rate_expectation": {"type": "string", "description": "Compensation expectation as stated."},
        "work_authorization": {"type": "string", "description": "As stated, or 'not discussed'."},
        "interview_score": {
            "type": "number",
            "description": "Overall fit from this conversation. Use only integers 1 through 10.",
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string", "enum": ["advance", "hold", "reject"]},
        "rationale": {"type": "string", "description": "Two or three sentences justifying the recommendation."},
    },
    "required": ["interview_score", "recommendation", "rationale"],
}


class VapiClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.vapi_base_url.rstrip("/")

    @property
    def is_live(self) -> bool:
        return self.settings.vapi_mode == "live" and bool(self.settings.vapi_api_key)

    async def place_call(
        self,
        *,
        phone_number: str,
        candidate_name: str,
        system_prompt: str,
        first_message: str,
        metadata: dict[str, Any],
        max_duration_seconds: int = 600,
    ) -> dict[str, Any]:
        """Start an outbound interview call. Returns `{id, status}`.

        The call is asynchronous by nature: this returns as soon as VAPI accepts it,
        and the result arrives later on the webhook.
        """
        if not self.is_live:
            fake_id = f"mock-call-{metadata.get('run_id', '0')}"
            log.info("[mock] would call %s as %s", phone_number, candidate_name)
            return {"id": fake_id, "status": "queued", "mock": True}

        if not self.settings.vapi_phone_number_id:
            raise VapiError(
                "VAPI_PHONE_NUMBER_ID is not set. VAPI's own free numbers cannot place "
                "outbound calls — import a Twilio number first: "
                "POST https://api.vapi.ai/phone-number"
            )

        payload = {
            "phoneNumberId": self.settings.vapi_phone_number_id,
            "name": f"Screening — {candidate_name}",
            "customer": {
                "number": phone_number,
                "name": candidate_name,
                "externalId": str(metadata.get("run_id", "")),
            },
            "assistant": {
                "name": "Asendia Screening Interviewer",
                "firstMessage": first_message,
                "model": {
                    "provider": self.settings.vapi_model_provider,
                    "model": self.settings.vapi_model,
                    "temperature": 0.4,
                    "messages": [{"role": "system", "content": system_prompt}],
                },
                "transcriber": {"provider": "deepgram", "model": "nova-3"},
                "maxDurationSeconds": max_duration_seconds,
                "endCallPhrases": ["goodbye", "have a great day"],
                "metadata": metadata,
                "artifactPlan": {"recordingEnabled": True},
                "analysisPlan": {
                    "minMessagesThreshold": 2,
                    # Raised from the 5s default: extraction over a full transcript
                    # regularly exceeds it, and a timeout returns empty rather than
                    # erroring — a silent failure that is painful to debug.
                    "summaryPlan": {"enabled": True, "timeoutSeconds": 30},
                    "successEvaluationPlan": {
                        "enabled": True,
                        "rubric": "NumericScale",
                        "timeoutSeconds": 30,
                    },
                    "structuredDataPlan": {
                        "enabled": True,  # defaults to false — must be explicit
                        "timeoutSeconds": 30,
                        "schema": INTERVIEW_SCHEMA,
                    },
                },
                "server": {
                    "url": f"{self.settings.public_base_url}/api/webhooks/vapi",
                    "headers": {"x-asendia-secret": self.settings.vapi_webhook_secret},
                },
                "serverMessages": ["end-of-call-report", "status-update"],
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/call",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.vapi_api_key}"},
            )
        if resp.status_code >= 400:
            raise VapiError(f"VAPI call failed ({resp.status_code}): {resp.text[:400]}")

        data = resp.json()
        log.info("placed VAPI call %s to %s", data.get("id"), phone_number)
        return data


def parse_end_of_call(message: dict[str, Any]) -> dict[str, Any]:
    """Flatten an `end-of-call-report` into the fields we persist.

    The wire format nests everything under `message`; callers pass that inner object.
    """
    artifact = message.get("artifact") or {}
    analysis = message.get("analysis") or {}
    structured = analysis.get("structuredData") or {}

    return {
        "transcript": artifact.get("transcript", ""),
        "recording_url": artifact.get("recordingUrl") or artifact.get("stereoRecordingUrl"),
        "summary": analysis.get("summary", ""),
        "success_evaluation": analysis.get("successEvaluation"),
        "ended_reason": message.get("endedReason"),
        "cost": message.get("cost"),
        "interview_score": structured.get("interview_score"),
        "recommendation": structured.get("recommendation"),
        "rationale": structured.get("rationale"),
        "strengths": structured.get("strengths", []),
        "concerns": structured.get("concerns", []),
        "answers": structured.get("answers", []),
        "availability": structured.get("availability"),
        "rate_expectation": structured.get("rate_expectation"),
        "work_authorization": structured.get("work_authorization"),
    }
