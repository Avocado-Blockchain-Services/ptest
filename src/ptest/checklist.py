"""Canonical doctor readiness-assessment checklist catalog.

This module owns the single ordered worksheet catalog consumed by renderers
and guide content. No renderer keeps a second list. Every row starts as
``unknown``; ptest static patterns never fill or upgrade a row.
"""
from __future__ import annotations

import importlib.resources
from dataclasses import dataclass

# NOTE: ptest.contracts derives its checklist constants from CATALOG below,
# so this module must not import contracts at top level (import cycle).
# load_recipe() imports it lazily instead.

_RECIPE_MAX_BYTES = 65536
_RECIPE_FILES = {
    "factories": "recipes/factories.md",
    "databases": "recipes/databases.md",
    "cache": "recipes/cache.md",
    "files-ports": "recipes/files-ports.md",
    "processes": "recipes/processes.md",
    "time-network": "recipes/time-network.md",
}


@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    id: str
    criterion: str
    evidence: str
    recommendation: str
    example: str
    verification: str
    recipe: str | None


CATALOG: tuple[ChecklistEntry, ...] = (
    ChecklistEntry(
        id="FIX-001",
        criterion="Fixtures/factories create fresh test records without weakening assertions.",
        evidence="Cite fixture/factory definitions and callers.",
        recommendation="Prefer a small factory when records vary; pure tests need none.",
        example="Reuse recipes/factories.md.",
        verification="Scoped ptest proves original assertions and inventory remain.",
        recipe="factories",
    ),
    ChecklistEntry(
        id="FIX-002",
        criterion="Mutable fixture state is isolated or reset for every test.",
        evidence="Cite fixture lifetime, mutation, and reset boundaries.",
        recommendation="Replace shared mutable state or prove deterministic reset.",
        example="Reuse recipes/factories.md.",
        verification="Run scoped order/worker permutations through ptest.",
        recipe="factories",
    ),
    ChecklistEntry(
        id="DB-001",
        criterion="Expensive database/server/schema setup is reused per run or worker, not repeated per test.",
        evidence="Cite setup scope and cost.",
        recommendation="Prefer one owned database/schema template per run/worker; no ORM is mandated.",
        example="Reuse recipes/databases.md.",
        verification="Measured scoped ptest run shows setup reuse without semantic loss.",
        recipe="databases",
    ),
    ChecklistEntry(
        id="DB-002",
        criterion="Database identities, records, and cleanup have explicit run/worker ownership.",
        evidence="Cite name derivation and teardown.",
        recommendation="Remove only owned records/namespaces; never infer ownership from a test-like name.",
        example="Reuse recipes/databases.md.",
        verification="Neighbor database/schema sentinel survives concurrent teardown.",
        recipe="databases",
    ),
    ChecklistEntry(
        id="CACHE-001",
        criterion="Redis, Valkey, and other mutable caches are namespaced and cleaned by owner.",
        evidence="Cite key prefix and deletion path.",
        recommendation=(
            "Prefer checkout/run/worker prefixes and owned-key deletion; verify any claimed "
            "disposable-service exclusivity before judging a global-flush hypothesis. "
            "Never recommend blanket flush as a repair."
        ),
        example="Reuse recipes/cache.md.",
        verification="Neighbor key survives concurrent cleanup.",
        recipe="cache",
    ),
    ChecklistEntry(
        id="RESOURCE-001",
        criterion="Writable files and listening ports are uniquely owned and released.",
        evidence="Cite temp-root and port allocation.",
        recommendation="Use run/worker temp roots and OS-assigned ports.",
        example="Reuse recipes/files-ports.md.",
        verification="Concurrent scoped runs use distinct paths/ports and preserve a neighbor sentinel.",
        recipe="files-ports",
    ),
    ChecklistEntry(
        id="NETWORK-001",
        criterion="External network is denied or replaced by a declared isolated fake.",
        evidence="Cite clients, targets, and denial/fake boundary.",
        recommendation="Do not require a live service for ordinary tests.",
        example="Reuse recipes/time-network.md.",
        verification="Scoped ptest succeeds under network denial or against the declared local fake.",
        recipe="time-network",
    ),
    ChecklistEntry(
        id="PROCESS-001",
        criterion="Child processes remain owned, joined, cancelled, and reaped.",
        evidence="Cite spawn and teardown paths.",
        recommendation="Keep descendants in the owned foreground process group; do not detach.",
        example="Reuse recipes/processes.md.",
        verification="Cancellation leaves no owned descendant and does not affect a neighbor process.",
        recipe="processes",
    ),
    ChecklistEntry(
        id="TIME-001",
        criterion="Clocks and synchronization are deterministic.",
        evidence="Cite clock injection and barriers.",
        recommendation="Prefer fake clocks/events over wall-clock sleeps.",
        example="Reuse recipes/time-network.md.",
        verification="Scoped ptest repeats without wall-clock waiting or race-dependent outcome.",
        recipe="time-network",
    ),
    ChecklistEntry(
        id="SELECT-001",
        criterion="Test selection has declared closed inputs and a conservative full fallback.",
        evidence="Cite policy, source-to-test mapping, environment, generated outputs, and full triggers.",
        recommendation="Dynamic unknown input widens to full.",
        example="Static plan previews are insufficient evidence.",
        verification=(
            "Exercise input changes through authorized ptest execution and inspect "
            "actual selection/fallback artifacts; static plan previews are insufficient."
        ),
        recipe=None,
    ),
    ChecklistEntry(
        id="TIMING-001",
        criterion="Per-test timings are measured and interpreted without hiding integration work.",
        evidence="Cite ptest result/history timing.",
        recommendation=(
            "<0.5s healthy, 0.5-<2s inspect, 2-3s optimize, >3s investigate or justify; "
            "these are guidance, not failures."
        ),
        example="Absence of timings stays unknown.",
        verification="Repeat the scoped ptest measurement; absence of timings stays unknown.",
        recipe=None,
    ),
)


def load_recipe(name: str) -> str:
    """Return one bounded packaged recipe; fail closed on unknown/missing data."""
    from . import contracts as C

    relative = _RECIPE_FILES.get(name)
    if relative is None:
        raise C.Problem(code="unsafe-path", message="packaged recipe name is not known",
                        phase="render")
    try:
        text = importlib.resources.files("ptest").joinpath(
            "resources", relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise C.Problem(code="state-unavailable", message="packaged recipe is unavailable",
                        phase="render") from None
    if len(text.encode("utf-8")) > _RECIPE_MAX_BYTES:
        raise C.Problem(code="invalid-bound", message="packaged recipe exceeds its bound",
                        phase="render")
    return text


__all__ = ["CATALOG", "ChecklistEntry", "load_recipe"]
