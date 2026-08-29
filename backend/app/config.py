"""Application configuration.

Every external provider has a `*_MODE` toggle (`live` | `mock`). The engine resolves
providers through `integrations/` factories that read these values, so a demo can run
end to end with no network access, and individual providers can be flipped to live
independently as credentials are verified.
"""

from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["live", "mock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """Treat an empty env var as absent.

        A .env copied from the template has every optional key present but blank
        (`JOBDIVA_RECRUITER_ID=`). Without this, those blanks reach the parser as
        empty strings and fail validation on any non-string field — which turns a
        perfectly reasonable config file into a startup crash.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        # A dotenv line like `KEY=   # explanation` parses the comment as the value,
        # so an apparently-unset key silently becomes a bogus credential that fails
        # far away from the cause. Treat it as unset.
        return None if text == "" or text.startswith("#") else value

    app_name: str = "Asendia Workflows"
    database_url: str = "sqlite:///./asendia.db"
    # Public base URL of this backend. VAPI posts call results here, so during local
    # development this must be a tunnel (ngrok/cloudflared), not localhost.
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = ["http://localhost:5173"]

    # --- JobDiva ATS -----------------------------------------------------------
    # Single public host; there is no separate sandbox domain. A sandbox is a
    # different `clientid` on the same host.
    jobdiva_mode: Mode = "mock"
    jobdiva_base_url: str = "https://api.jobdiva.com"
    jobdiva_client_id: int | None = None
    jobdiva_username: str | None = None
    jobdiva_password: str | None = None
    # The API declares an apiKey security scheme with no `Bearer` prefix. Unverified
    # until we authenticate, so the client tries raw first and falls back.
    jobdiva_auth_scheme: Literal["raw", "bearer", "auto"] = "auto"
    jobdiva_recruiter_id: int | None = None
    # Deliberately separate from `jobdiva_mode`, and defaulting to suppressed.
    # Reads can be live — real jobs, real applicants, real resumes — while writes are
    # captured and displayed instead of sent. Creating a submittal puts a record in a
    # real recruiter's work queue, so mutation is opt-in rather than a side effect of
    # pointing the app at live credentials.
    jobdiva_write_mode: Literal["suppressed", "live"] = "suppressed"

    # --- VAPI (AI voice) -------------------------------------------------------
    vapi_mode: Mode = "mock"
    vapi_base_url: str = "https://api.vapi.ai"
    vapi_api_key: str | None = None
    vapi_phone_number_id: str | None = None
    # Shared secret we set on assistant.server.headers and verify on the webhook.
    vapi_webhook_secret: str = "dev-secret-change-me"
    # The model VAPI runs *during* the call. VAPI uses its own provider credentials
    # for this, not ours, so if the account has no Anthropic key these two let us
    # switch providers without touching code.
    vapi_model_provider: str = "anthropic"
    vapi_model: str = "claude-sonnet-5"

    # --- Mailjet (email) -------------------------------------------------------
    mailjet_mode: Mode = "mock"
    mailjet_base_url: str = "https://api.mailjet.com"
    mailjet_api_key: str | None = None
    mailjet_api_secret: str | None = None
    mailjet_from_email: str | None = None
    mailjet_from_name: str = "Asendia Recruiting"

    # --- SMS -------------------------------------------------------------------
    # `log` records the exact message without sending. Mailjet SMS requires a funded
    # wallet plus a 48-hour security check, so `log` is the demo default.
    sms_mode: Literal["log", "mailjet", "twilio"] = "log"
    mailjet_sms_token: str | None = None
    mailjet_sms_from: str = "Asendia"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None

    # --- LLM -------------------------------------------------------------------
    llm_mode: Mode = "mock"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5"
    # Per-request ceiling. The SDK default is 10 minutes, which is far too long here:
    # runs execute concurrently under a semaphore, so one hung request holds a worker
    # slot and — because the batch is gathered — stalls the whole cycle behind it.
    llm_timeout_seconds: float = 90.0

    # Global kill switch: execute every step and record what *would* have been sent,
    # but perform no outbound side effects.
    dry_run: bool = False

    # When set, the top-ranked seeded candidate uses this number, so a mock-data run
    # places a real call to your own phone. This is how the live demo is rehearsed.
    demo_phone_number: str | None = None
    # Which JobDiva job the dashboard syncs by default.
    demo_job_id: int = 4242

    # How many workflow runs execute at once. Each holds its own database session and
    # makes several LLM round trips, so this trades latency against LLM rate limits.
    max_concurrent_runs: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
