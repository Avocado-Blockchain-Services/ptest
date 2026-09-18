"""The single, closed runner registry owned by the NG integration surface.

Adapters remain preparation-only in this slice.  The execution orchestrator is
deliberately not hidden behind this registry: a later slice must authenticate a
guard and a scheduler grant before launching a prepared command.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import contracts as C
from .adapters import pytest as pytest_adapter
from .adapters import simple as simple_adapter
from .adapters import vitest as vitest_adapter


Prepare = Callable[[C.Config, C.Plan, C.Grant, C.AttemptIdentity], C.PreparedRun]


@dataclass(frozen=True, slots=True)
class RunnerAdapter:
    """Closed metadata and preparation seam for one supported profile."""

    kind: C.RunnerKind
    _prepare: Prepare
    _exclusive: bool = False
    automatic_full: bool = False

    def prepare(self, config: C.Config, plan: C.Plan, grant: C.Grant,
                attempt: C.AttemptIdentity) -> C.PreparedRun:
        return self._prepare(config, plan, grant, attempt)

    def requires_exclusive(self, config: C.Config) -> bool:
        if not isinstance(config, C.Config) or config.runner.kind is not self.kind:
            raise TypeError("runner adapter requires matching Config")
        return self._exclusive

    def mode_for_automatic(self) -> str:
        """Return the only safe automatic mode for this profile."""
        return "full" if self.automatic_full else "selected"


_REGISTRY: dict[C.RunnerKind, RunnerAdapter] = {
    C.RunnerKind.PYTEST: RunnerAdapter(
        C.RunnerKind.PYTEST, pytest_adapter.prepare,
    ),
    C.RunnerKind.VITEST: RunnerAdapter(
        C.RunnerKind.VITEST, vitest_adapter.prepare,
    ),
    C.RunnerKind.GO: RunnerAdapter(
        C.RunnerKind.GO, simple_adapter.prepare, automatic_full=True,
    ),
    C.RunnerKind.CARGO: RunnerAdapter(
        C.RunnerKind.CARGO, simple_adapter.prepare, automatic_full=True,
    ),
    C.RunnerKind.COMMAND: RunnerAdapter(
        C.RunnerKind.COMMAND, simple_adapter.prepare,
        _exclusive=True, automatic_full=True,
    ),
}


def adapter_for(kind: C.RunnerKind | str) -> RunnerAdapter:
    """Return a registered adapter; there is intentionally no plugin lookup."""
    try:
        normalized = kind if isinstance(kind, C.RunnerKind) else C.RunnerKind(kind)
        return _REGISTRY[normalized]
    except (KeyError, TypeError, ValueError):
        raise C.Problem(
            code="unsupported-capability",
            message="runner profile is not registered",
            phase="execution",
        ) from None


def registered_kinds() -> tuple[C.RunnerKind, ...]:
    """Return the immutable public registry order used by static summaries."""
    return tuple(_REGISTRY)


__all__ = ["RunnerAdapter", "adapter_for", "registered_kinds"]
