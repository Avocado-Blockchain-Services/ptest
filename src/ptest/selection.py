"""Pure, conservative mapping from a static snapshot to a test plan."""
from __future__ import annotations

import fnmatch
import hashlib

from . import contracts as C


def _reason(code: str, message: str, paths: tuple = ()) -> C.Reason:
    return C.Reason(code=code, message=message, paths=paths)


def _policy_digest(config: C.Config) -> str:
    return hashlib.sha256(repr(config.selection).encode()).hexdigest()


def _matches(path: str, patterns: tuple) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") or fnmatch.fnmatchcase(path, item) for item in patterns)


def _invalid_policy(policy: C.SelectionPolicy) -> bool:
    for prefix in policy.no_tests:
        if _matches(prefix, policy.input_roots + policy.full_triggers + policy.always + policy.non_input_outputs):
            return True
        for group in policy.groups:
            if _matches(prefix, group.sources + group.tests):
                return True
    return False


def _outputs_overlap_inputs(policy: C.SelectionPolicy) -> bool:
    protected = policy.input_roots + policy.full_triggers + policy.always + policy.ignored_inputs
    for output in policy.non_input_outputs:
        if any(_matches(output, (item,)) or _matches(item, (output,)) for item in protected):
            return True
        for group in policy.groups:
            if any(_matches(output, (item,)) or _matches(item, (output,))
                   for item in group.sources + group.tests):
                return True
    return False


def _full(request: C.RunRequest, snapshot: C.InputSnapshot, code: str, message: str) -> C.Plan:
    return C.Plan(mode=request.mode, execution="full", reasons=(_reason(code, message),),
                  input_digest=snapshot.digest, compatibility=snapshot.compatibility, static_preview=True)


def choose_plan(config: C.Config, snapshot: C.InputSnapshot,
                history: C.HistoryView, request: C.RunRequest) -> C.Plan:
    """Choose only proven group unions; ambiguity is an actual full gate."""
    if request.mode is C.Mode.FULL:
        return _full(request, snapshot, "full-gate-obligation", "explicit full request")
    if request.mode is C.Mode.SCOPED:
        return C.Plan(mode=request.mode, execution="scoped", reasons=(_reason("unknown-input", "explicit scoped request"),),
                      input_digest=snapshot.digest, compatibility=snapshot.compatibility, static_preview=True)
    policy = config.selection
    if not policy.enabled or not policy.closed_inputs:
        return _full(request, snapshot, "selection-disabled", "selection is not explicitly closed")
    if _invalid_policy(policy) or _outputs_overlap_inputs(policy):
        return _full(request, snapshot, "policy-invalid", "no-test prefix overlaps an input contract")
    if snapshot.digest is None or snapshot.compatibility is None or snapshot.limitations or history.limitations:
        return _full(request, snapshot, "unknown-input", "static input evidence is incomplete")
    baseline = history.baseline
    if baseline is None:
        return _full(request, snapshot, "no-baseline", "no compatible baseline exists")
    if history.selection_disabled:
        return _full(request, snapshot, "selection-disabled", "history disabled selection")
    if baseline.compatibility != snapshot.compatibility or baseline.policy_digest != _policy_digest(config):
        return _full(request, snapshot, "incompatible-baseline", "baseline compatibility or policy changed")
    if baseline.input_digest != snapshot.digest and not snapshot.changes:
        return _full(request, snapshot, "unknown-input", "fingerprint changed without a classifiable path")
    if not baseline.inventory.complete:
        return _full(request, snapshot, "incomplete-inventory", "baseline inventory is incomplete")
    if any(ob.file is None or ob.test_id is None for ob in history.obligations):
        return _full(request, snapshot, "full-gate-obligation", "whole-gate obligation remains")
    inventory = {record.file for record in baseline.inventory.tests}
    selected: set[str] = set()
    for obligation in history.obligations:
        if obligation.file not in inventory:
            return _full(request, snapshot, "prior-failure", "failed test is absent from inventory")
        selected.add(obligation.file)
    for change in snapshot.changes:
        paths = tuple(path for path in (change.old, change.new) if path is not None)
        if change.kind not in {"added", "modified", "deleted", "renamed", "untracked", "ignored"}:
            return _full(request, snapshot, "unknown-input", "unclassified source change")
        for path in paths:
            if _matches(path, policy.full_triggers) or _matches(path, policy.always):
                return _full(request, snapshot, "policy-changed", "full-trigger input changed")
            if _matches(path, policy.no_tests):
                continue
            if change.kind == "ignored" and not _matches(path, policy.ignored_inputs):
                return _full(request, snapshot, "unknown-input", "ignored input is not declared")
            involved = [group for group in policy.groups if _matches(path, group.sources) or _matches(path, group.tests)]
            if not involved:
                return _full(request, snapshot, "unknown-input", "changed path has no declared group")
            for group in involved:
                selected.update(group.tests)
            if path in inventory:
                selected.add(path)
    if not selected:
        return C.Plan(mode=request.mode, execution="none", reasons=(_reason("no-tests-needed", "no relevant inputs changed"),),
                      input_digest=snapshot.digest, compatibility=snapshot.compatibility,
                      baseline_run_id=baseline.run_id, static_preview=True)
    if not selected.issubset(inventory):
        return _full(request, snapshot, "incomplete-inventory", "selected test is absent from inventory")
    if len(selected) / max(1, len(inventory)) >= policy.full_ratio:
        return _full(request, snapshot, "full-gate-obligation", "selection reaches full ratio")
    return C.Plan(mode=request.mode, execution="selected", files=tuple(sorted(selected)),
                  reasons=(_reason("unknown-input", "declared group selection"),),
                  input_digest=snapshot.digest, compatibility=snapshot.compatibility,
                  baseline_run_id=baseline.run_id, static_preview=True)
