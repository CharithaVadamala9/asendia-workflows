"""The module contract.

Every workflow module implements this one interface. A module declares its
configuration as a Pydantic model; the engine derives a JSON Schema from it and serves
that in the module catalog; the frontend renders the configuration form from the
schema. Adding a module is therefore a backend-only change — the UI picks it up with
no code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from app.engine.context import RunContext


class StepStatus(StrEnum):
    COMPLETED = "completed"
    # The step handed off to an external system and is waiting for a callback. The
    # run halts with its state in the database and resumes when the callback lands.
    SUSPENDED = "suspended"
    FAILED = "failed"
    SKIPPED = "skipped"


class ModuleCategory(StrEnum):
    TRIGGER = "trigger"
    ACTION = "action"
    AI = "ai"


class StepResult(BaseModel):
    status: StepStatus
    output: dict[str, Any] = {}
    error: str | None = None
    # Correlation key for a suspended step (e.g. a VAPI call id).
    external_ref: str | None = None

    @classmethod
    def ok(cls, **output: Any) -> StepResult:
        return cls(status=StepStatus.COMPLETED, output=output)

    @classmethod
    def suspend(cls, external_ref: str, **output: Any) -> StepResult:
        return cls(
            status=StepStatus.SUSPENDED, external_ref=external_ref, output=output
        )

    @classmethod
    def fail(cls, error: str, **output: Any) -> StepResult:
        return cls(status=StepStatus.FAILED, error=error, output=output)


class ModuleSpec(BaseModel):
    """Catalog entry for a module, served to the frontend at `GET /api/modules`."""

    id: str
    name: str
    description: str
    category: ModuleCategory
    config_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # True when the module hands off to an external system and completes via callback.
    is_async: bool = False


class EmptyConfig(BaseModel):
    pass


@runtime_checkable
class Module(Protocol):
    """Structural type every module satisfies."""

    id: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]

    def spec(self) -> ModuleSpec: ...

    async def run(self, ctx: RunContext, config: BaseModel) -> StepResult: ...


class BaseModule:
    """Convenience base supplying `spec()` from class attributes."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    category: ClassVar[ModuleCategory] = ModuleCategory.ACTION
    config_model: ClassVar[type[BaseModel]] = EmptyConfig
    output_model: ClassVar[type[BaseModel] | None] = None
    is_async: ClassVar[bool] = False

    def spec(self) -> ModuleSpec:
        return ModuleSpec(
            id=self.id,
            name=self.name,
            description=self.description,
            category=self.category,
            config_schema=self.config_model.model_json_schema(),
            output_schema=(
                self.output_model.model_json_schema() if self.output_model else {}
            ),
            is_async=self.is_async,
        )

    async def run(self, ctx: RunContext, config: BaseModel) -> StepResult:
        raise NotImplementedError

    async def resume(self, ctx: RunContext, config: BaseModel, payload: dict) -> StepResult:
        """Called when an external callback arrives for a suspended step."""
        raise NotImplementedError(f"{self.id} does not support resume")
