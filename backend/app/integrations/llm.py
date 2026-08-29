"""Claude client for structured extraction and judgment.

Every call goes through `complete_json`, which forces the model through a tool schema
so the result is validated JSON rather than prose we have to parse. Callers get a
typed Pydantic model back or an exception — never a half-parsed string.

In `mock` mode the deterministic stub lets the whole pipeline run with no API key,
which is what makes the demo independent of network conditions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# Published rates, USD per million tokens. Used only to display an estimate — the
# authoritative number is always the invoice.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class LLMCounter:
    """Counts model calls and tokens, so cost is measured rather than guessed."""

    def __init__(self) -> None:
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def record_usage(self, usage) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0

    def cost(self, model: str) -> float:
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1_000_000

    def reset(self) -> None:
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0


COUNTER = LLMCounter()


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: Any = None

    @property
    def is_live(self) -> bool:
        return self.settings.llm_mode == "live" and bool(
            self.settings.anthropic_api_key
        )

    def _anthropic(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=self.settings.llm_timeout_seconds,
                # The SDK already retries 429s and 5xx with backoff; two attempts is
                # enough here because a failed criterion degrades rather than fails.
                max_retries=2,
            )
        return self._client

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int = 2000,
        mock: T | None = None,
    ) -> T:
        """Call the model and return a validated instance of `schema`.

        `mock` is the value returned when the client is not live — required, so that
        every call site has to think about what its degraded behavior should be.
        """
        COUNTER.calls += 1
        if not self.is_live:
            if mock is None:
                raise LLMError(
                    f"LLM is in mock mode and no mock value was supplied for "
                    f"{schema.__name__}"
                )
            return mock

        tool = {
            "name": "record",
            "description": f"Record the {schema.__name__} result.",
            "input_schema": schema.model_json_schema(),
        }
        try:
            resp = await self._anthropic().messages.create(
                model=self.settings.llm_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "record"},
            )
        except Exception as exc:  # noqa: BLE001 — surface provider errors uniformly
            raise LLMError(f"Claude request failed: {exc}") from exc

        if usage := getattr(resp, "usage", None):
            COUNTER.record_usage(usage)

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                try:
                    return schema.model_validate(block.input)
                except ValidationError as exc:
                    raise LLMError(
                        f"model returned invalid {schema.__name__}: {exc}"
                    ) from exc
        raise LLMError(
            f"no tool_use block in response: {json.dumps(str(resp.content))[:300]}"
        )


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
