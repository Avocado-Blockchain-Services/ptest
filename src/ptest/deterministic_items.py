"""Deterministic doctor answers for SELECT-001 and TIMING-001.

These two checklist items need no model call: SELECT-001 is answered from
the child's own runner kind and ``[selection]`` policy, and TIMING-001 is
answered from ptest's own run/timing history. History access is read-only
(``history.read_history`` and ``history.read_history_summaries`` only); the
scheduler is never initialized. ``answers_for`` never raises: anything
unresolvable yields ``{}`` or an ``unknown`` answer, and an uncitable
answer degrades in ``agent_assessment.assemble_child``.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from . import history as history_api

DETERMINISTIC_ITEM_IDS: tuple[str, ...] = ("SELECT-001", "TIMING-001")

_VALID_STATUSES = frozenset({"satisfied", "gap", "unknown", "not-applicable"})

_SLOW_S = 3.0


@dataclass(frozen=True, slots=True)
class DeterministicAnswer:
    """One model-free answer for a deterministic checklist item."""

    item_id: str                           # one of DETERMINISTIC_ITEM_IDS
    status: str                            # satisfied | gap | unknown | not-applicable
    reason: str                            # one plain sentence, no prefix, no backticks
    evidence_paths: tuple[str, ...] = ()   # packet excerpt paths to cite
    finding_summary: str | None = None     # gap only; trusted prose
    finding_change: str | None = None      # gap only; trusted prose

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or (
                self.item_id not in DETERMINISTIC_ITEM_IDS):
            raise ValueError(
                f"answer.item_id {self.item_id!r} is not deterministic")
        if not isinstance(self.status, str) or (
                self.status not in _VALID_STATUSES):
            raise ValueError(f"answer status {self.status!r} is unknown")
        if not isinstance(self.reason, str) or not self.reason:
            raise TypeError("answer.reason must be nonempty prose")
        if "`" in self.reason:
            raise ValueError("answer.reason must carry no backticks")
        if self.reason.startswith("Answered by ptest: "):
            raise ValueError("answer.reason must carry no ptest-owned prefix")
        evidence_paths = self.evidence_paths
        if not isinstance(evidence_paths, (tuple, list)):
            raise TypeError("answer.evidence_paths must be a tuple")
        for path in tuple(evidence_paths):
            if not isinstance(path, str) or not path:
                raise TypeError("answer.evidence_paths entries must be str")
        object.__setattr__(self, "evidence_paths", tuple(evidence_paths))
        if self.status == "gap":
            for field in ("finding_summary", "finding_change"):
                text = getattr(self, field)
                if not isinstance(text, str) or not text:
                    raise TypeError(f"answer.{field} must be nonempty prose")
                if C.aa_prose_is_untrusted(text):
                    raise ValueError(
                        f"answer.{field} carries untrusted content")
        elif (self.finding_summary is not None
                or self.finding_change is not None):
            raise ValueError("non-gap answers carry no finding")


def _cfg(declaration: str) -> str:
    return ".ptest.toml" if declaration == "." else f"{declaration}/.ptest.toml"


def _child_config(resolution, declaration: str):
    """Return the packet child's config, or None when it does not resolve."""
    if not isinstance(resolution, C.ConfigResolution):
        return None
    if declaration == "." and isinstance(resolution.config, C.Config):
        return resolution.config
    manifest = resolution.monorepo
    if manifest is None:
        return None
    from .monorepo import diagnose_child

    diagnosis = diagnose_child(resolution.root, declaration)
    if diagnosis.kind == "ok" and isinstance(diagnosis.config, C.Config):
        return diagnosis.config
    return None


def _checkout_identity(config: C.Config, resolution, declaration: str):
    """Mirror ``cli._checkout`` for the packet child, without side effects."""
    if config.checkout is not None:
        root = config.checkout.root
    elif config.config_path is not None:
        root = config.config_path.parent
    elif declaration == ".":
        root = Path(resolution.root)
    else:
        root = Path(resolution.root) / declaration
    checkout_id = hashlib.sha256(
        os.fsencode(os.path.realpath(root))).hexdigest()[:32]
    return C.CheckoutIdentity(project_id=config.project_id,
                              checkout_id=checkout_id, root=root)


def _select_answer(config: C.Config, cfg: str,
                   evidence: tuple[str, ...]) -> DeterministicAnswer:
    runner = config.runner.kind.value
    if runner in ("vitest", "command"):
        return DeterministicAnswer(
            item_id="SELECT-001", status="not-applicable",
            reason=(f"ptest has no automatic test selection for {runner}; "
                    "every run is scoped or full"),
            evidence_paths=evidence)
    policy = config.selection
    if not policy.enabled:
        return DeterministicAnswer(
            item_id="SELECT-001", status="gap",
            reason=f"selection is disabled in {cfg}",
            evidence_paths=evidence,
            finding_summary=(
                f"Selection is disabled in {cfg}, so every run executes "
                "the full suite and scoped runs cannot narrow to changed "
                "inputs."),
            finding_change=_select_fix(config, cfg))
    if not policy.closed_inputs or not policy.input_roots:
        return DeterministicAnswer(
            item_id="SELECT-001", status="gap",
            reason=f"selection inputs are not declared closed in {cfg}",
            evidence_paths=evidence,
            finding_summary=(
                f"Selection inputs are not declared closed in {cfg}, so "
                "an unknown input cannot widen to the full suite."),
            finding_change=_select_fix(config, cfg))
    return DeterministicAnswer(
        item_id="SELECT-001", status="satisfied",
        reason=(f"selection is enabled with closed inputs "
                f"({len(policy.input_roots)} input roots, "
                f"{len(policy.full_triggers)} full triggers); unknown input "
                "widens to the full suite"),
        evidence_paths=evidence)


def _select_fix(config: C.Config, cfg: str) -> str:
    """Concrete fix naming the closed-inputs policy (and coverage first)."""
    from .adapters import pytest as pytest_adapter

    try:
        qualified = pytest_adapter.qualified_profile(config) is not None
    except Exception:
        qualified = False
    fix = (f"Enable selection with closed_inputs, input_roots, and "
           f"full_triggers in {cfg}.")
    if not qualified and config.runner.kind is C.RunnerKind.PYTEST:
        fix += (" For pytest without the coverage catalog profile, first "
                "add --cov and --cov-report to runner args.")
    return fix


def _completed_full_runs(summaries) -> list:
    runs = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        if summary.get("mode") != "full":
            continue
        if summary.get("status") not in ("passed", "failed"):
            continue
        runs.append(summary)
    return runs


def _whole_run_s(summary: dict) -> float:
    # Keys are the public history payload names emitted by
    # ``contracts._timings_dict`` (``queue``, ``setup``, ``collection``,
    # ``execution``, ``finalization``), not the ``Timings`` attribute names.
    # ``queue`` is included: the serialized run attributes all five parts
    # to the run, and the whole-run figure sums every recorded part.
    timings = summary.get("timings")
    if not isinstance(timings, dict):
        return 0.0
    total = 0.0
    for key in ("queue", "setup", "collection", "execution",
                "finalization"):
        value = timings.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            total += float(value)
    return total


def _timing_answer(domain: C.DomainPaths, config: C.Config, resolution,
                   declaration: str,
                   evidence: tuple[str, ...]) -> DeterministicAnswer:
    checkout = _checkout_identity(config, resolution, declaration)
    view = history_api.read_history(domain, checkout)
    summaries = history_api.read_history_summaries(domain, checkout)
    baseline = view.baseline
    timed = ()
    if baseline is not None:
        timed = tuple(test.call_s for test in baseline.inventory.tests
                      if test.call_s is not None)
    if timed:
        slow = sum(1 for duration in timed if duration > _SLOW_S)
        slowest = max(timed)
        return DeterministicAnswer(
            item_id="TIMING-001", status="satisfied",
            reason=(f"ptest recorded per-test timings for {len(timed)} "
                    f"tests in the last clean full run; {slow} take over "
                    f"3 s (slowest {slowest:.1f} s)"),
            evidence_paths=evidence)
    full_runs = _completed_full_runs(summaries)
    if full_runs:
        seconds = _whole_run_s(full_runs[0])
        return DeterministicAnswer(
            item_id="TIMING-001", status="unknown",
            reason=(f"ptest has whole-run timing only (last full run "
                    f"{seconds:.1f} s); per-test timings are not recorded "
                    "for this runner"),
            evidence_paths=evidence)
    if view.selection_disabled:
        return DeterministicAnswer(
            item_id="TIMING-001", status="unknown",
            reason="ptest history is unavailable, so timing cannot be read",
            evidence_paths=())
    return DeterministicAnswer(
        item_id="TIMING-001", status="unknown",
        reason="no timing history yet: run ptest --full once",
        evidence_paths=())


def _answers_for(domain: C.DomainPaths, resolution: C.ConfigResolution,
                 packet) -> dict[str, DeterministicAnswer]:
    declaration = packet.declaration
    config = _child_config(resolution, declaration)
    if config is None:
        return {}
    cfg = _cfg(declaration)
    excerpt_paths = {excerpt.path for excerpt in packet.excerpts}
    evidence = (cfg,) if cfg in excerpt_paths else ()
    try:
        timing = _timing_answer(domain, config, resolution,
                                declaration, evidence)
    except Exception:
        timing = DeterministicAnswer(
            item_id="TIMING-001", status="unknown",
            reason="ptest history is unavailable, so timing cannot be read",
            evidence_paths=())
    return {
        "SELECT-001": _select_answer(config, cfg, evidence),
        "TIMING-001": timing,
    }


def answers_for(domain: C.DomainPaths, resolution: C.ConfigResolution,
                packet) -> dict[str, DeterministicAnswer]:
    """Answer the deterministic items for one packet; never raises.

    Returns ``{}`` when the packet's child config does not resolve, and an
    ``unknown`` TIMING answer when history is unavailable. Read-only:
    history is read, never written, and the scheduler is never touched.
    """
    try:
        return _answers_for(domain, resolution, packet)
    except Exception:
        return {}


__all__ = ["DETERMINISTIC_ITEM_IDS", "DeterministicAnswer", "answers_for"]
