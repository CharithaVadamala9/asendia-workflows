"""SMS and email delivery, behind provider interfaces.

SMS ships in `log` mode for the demo. That is not laziness: Mailjet's SMS API requires
a prepaid wallet, a Bearer token separate from the email API key, and a documented
48-hour security check after the first deposit. It fails at *send* time rather than at
integration time, so an unactivated wallet would surface as a broken step mid-demo. The
Mailjet call is implemented and one env var away from live; Twilio is wired too, since
the voice demo needs a Twilio account regardless.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    pass


async def send_sms(
    *, to: str, body: str, settings: Settings | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Send an SMS through the configured provider.

    Always returns a record of what was sent (or would have been), so the run timeline
    shows the exact message text regardless of provider.
    """
    s = settings or get_settings()
    record = {"to": to, "body": body, "provider": s.sms_mode, "delivered": False}

    if dry_run or s.sms_mode == "log":
        reason = "dry run" if dry_run else "SMS provider is in log mode"
        log.info("[sms:%s] to=%s body=%r", s.sms_mode, to, body)
        return {**record, "delivered": False, "reason": reason}

    if s.sms_mode == "twilio":
        if not all([s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number]):
            raise DeliveryError("Twilio credentials incomplete")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
                data={"To": to, "From": s.twilio_from_number, "Body": body},
                auth=(s.twilio_account_sid, s.twilio_auth_token or ""),
            )
        if resp.status_code >= 400:
            raise DeliveryError(f"Twilio send failed ({resp.status_code}): {resp.text[:300]}")

        body = resp.json()
        # A 201 means Twilio *accepted* the message, not that a carrier delivered it.
        # An unregistered US number gets 201 + "queued" and is then dropped with error
        # 30034 minutes later. Reporting `delivered: True` here would tell a recruiter
        # a candidate was contacted when they were not — so the accepted state is
        # reported honestly and delivery is confirmed separately.
        return {
            **record,
            "delivered": False,
            "accepted": True,
            "status": body.get("status"),
            "message_id": body.get("sid"),
            "reason": "accepted by Twilio — delivery confirmed via status callback",
        }

    if s.sms_mode == "mailjet":
        if not s.mailjet_sms_token:
            raise DeliveryError(
                "MAILJET_SMS_TOKEN is not set. It is generated in the Mailjet SMS "
                "dashboard and is separate from MAILJET_API_KEY."
            )
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{s.mailjet_base_url}/v4/sms-send",
                json={"From": s.mailjet_sms_from, "To": to, "Text": body},
                headers={"Authorization": f"Bearer {s.mailjet_sms_token}"},
            )
        if resp.status_code >= 400:
            raise DeliveryError(
                f"Mailjet SMS failed ({resp.status_code}): {resp.text[:300]} — "
                "check the SMS wallet is funded and past its 48-hour activation check"
            )
        return {**record, "delivered": True, "message_id": resp.json().get("MessageId")}

    raise DeliveryError(f"unknown SMS provider {s.sms_mode!r}")


async def send_email(
    *,
    to: str,
    to_name: str = "",
    subject: str,
    text: str,
    html: str | None = None,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send a transactional email via Mailjet."""
    s = settings or get_settings()
    record = {"to": to, "subject": subject, "body": text, "delivered": False}

    if dry_run or s.mailjet_mode != "live":
        reason = "dry run" if dry_run else "Mailjet is in mock mode"
        log.info("[email:mock] to=%s subject=%r", to, subject)
        return {**record, "reason": reason}

    if not all([s.mailjet_api_key, s.mailjet_api_secret, s.mailjet_from_email]):
        raise DeliveryError("Mailjet credentials or sender address incomplete")

    payload = {
        "Messages": [
            {
                "From": {"Email": s.mailjet_from_email, "Name": s.mailjet_from_name},
                "To": [{"Email": to, "Name": to_name or to}],
                "Subject": subject,
                "TextPart": text,
                # Docs prose says `HtmlPart`, every working example uses `HTMLPart`.
                "HTMLPart": html or f"<p>{text}</p>",
            }
        ]
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{s.mailjet_base_url}/v3.1/send",
            json=payload,
            auth=(s.mailjet_api_key or "", s.mailjet_api_secret or ""),
        )
    if resp.status_code >= 400:
        raise DeliveryError(
            f"Mailjet send failed ({resp.status_code}): {resp.text[:300]} — "
            "the From address must be a verified sender"
        )

    body = resp.json()
    first = (body.get("Messages") or [{}])[0]
    return {
        **record,
        "delivered": first.get("Status") == "success",
        "message_id": ((first.get("To") or [{}])[0]).get("MessageID"),
    }
