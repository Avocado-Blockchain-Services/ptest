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
PrepareAdvanced = Callable[..., C.PreparedRun]
CompoundSupportFn = Callable[..., C.CompoundSupport]
QualifiedProfileFn = Callable[[C.Config], dict[str, str] | None]


def _unsupported_support(config: C.Config, *, qualified_profile=None) -> C.CompoundSupport:
    return C.CompoundSupport(
        selection=False, parallel_identity=False, profile=None,
        limitations=(C.Reason(
            code="unsupported-capability",
            message="runner profile has no qualified compound capability",
        ),),
    )


@dataclass(frozen=True, slots=True)
class RunnerAdapter:
    """Closed metadata and preparation seam for one supported profile."""

    kind: C.RunnerKind
    _prepare: Prepare
    _exclusive: bool = False
    automatic_full: bool = False
    _compound_support: CompoundSupportFn = _unsupported_support
    _qualified_profile: QualifiedProfileFn | None = None
    _prepare_advanced: PrepareAdvanced | None = None

    def prepare(self, config: C.Config, plan: C.Plan, grant: C.Grant,
                attempt: C.AttemptIdentity) -> C.PreparedRun:
        return self._prepare(config, plan, grant, attempt)

    def compound_support(self, config: C.Config, *,
                         qualified_profile: dict[str, str] | None = None) -> C.CompoundSupport:
        if not isinstance(config, C.Config) or config.runner.kind is not self.kind:
            raise TypeError("runner adapter requires matching Config")
        return self._compound_support(config, qualified_profile=qualified_profile)

    def qualified_profile(self, config: C.Config) -> dict[str, str] | None:
        if not isinstance(config, C.Config) or config.runner.kind is not self.kind:
            raise TypeError("runner adapter requires matching Config")
        if self._qualified_profile is None:
            return None
        return self._qualified_profile(config)

    def prepare_advanced(self, config: C.Config, plan: C.Plan, grant: C.Grant,
                         attempt: C.AttemptIdentity,
                         *, expected_runtime_identity: str | None = None) -> C.PreparedRun:
        if self._prepare_advanced is None:
            raise C.Problem(
                code="unsupported-capability",
                message="runner profile has no qualified advanced preparation",
                phase="execution",
            )
        return self._prepare_advanced(
            config, plan, grant, attempt,
            expected_runtime_identity=expected_runtime_identity,
        )

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
        _compound_support=pytest_adapter.compound_support,
        _qualified_profile=pytest_adapter.qualified_profile,
        _prepare_advanced=pytest_adapter.prepare_advanced,
    ),
    C.RunnerKind.VITEST: RunnerAdapter(
        C.RunnerKind.VITEST, vitest_adapter.prepare,
        _compound_support=vitest_adapter.compound_support,
        _qualified_profile=vitest_adapter.qualified_profile,
        _prepare_advanced=vitest_adapter.prepare_advanced,
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
