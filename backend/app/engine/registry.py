"""Module registry.

Modules self-register at import time. The catalog served to the frontend is derived
entirely from what is registered here, so a new module appears in the UI's palette —
with its configuration form — as soon as it is imported.
"""

from __future__ import annotations

from app.engine.base import BaseModule, ModuleSpec

_REGISTRY: dict[str, BaseModule] = {}


def register(module: BaseModule) -> BaseModule:
    if not module.id:
        raise ValueError(f"{type(module).__name__} must define an id")
    if module.id in _REGISTRY:
        raise ValueError(f"duplicate module id {module.id!r}")
    _REGISTRY[module.id] = module
    return module


def get(module_id: str) -> BaseModule:
    try:
        return _REGISTRY[module_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise KeyError(f"unknown module {module_id!r} (known: {known})") from None


def all_modules() -> list[BaseModule]:
    return list(_REGISTRY.values())


def catalog() -> list[ModuleSpec]:
    return [m.spec() for m in _REGISTRY.values()]


def load_builtin_modules() -> None:
    """Import the built-in modules so they register themselves."""
    from app import modules  # noqa: F401
