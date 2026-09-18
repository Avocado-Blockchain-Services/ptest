"""Pure, conservative mapping from a static snapshot to a test plan."""
from __future__ import annotations

import hashlib

from . import contracts as C


def _reason(code: str, message: str, paths: tuple = ()) -> C.Reason:
    return C.Reason(code=code, message=message, paths=paths)


def _policy_digest(config: C.Config) -> str:
    return hashlib.sha256(repr(config.selection).encode()).hexdigest()


def _matches(path: str, patterns: tuple) -> bool:
    """Policy entries are literal path prefixes, including metacharacters."""
    return any(path == item or path.startswith(item + "/") for item in patterns)


def _overlaps(left: str, right: str) -> bool:
    return _matches(left, (right,)) or _matches(right, (left,))


def _invalid_policy(policy: C.SelectionPolicy, test_roots: tuple = ()) -> bool:
    protected = (policy.input_roots + policy.full_triggers + policy.always
                 + policy.ignored_inputs + test_roots + (".ptest.toml",))
    grouped = tuple(path for group in policy.groups for path in group.sources + group.tests)
    entries = protected + grouped + policy.no_tests + policy.non_input_outputs
    if any(not path or path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/"))
           for path in entries):
        return True
    for prefix in policy.no_tests:
        if any(_overlaps(prefix, item) for item in protected + grouped + policy.non_input_outputs):
            return True
    for output in policy.non_input_outputs:
        if any(_overlaps(output, item) for item in protected + grouped):
            return True
    return False


def _expand(prefixes: tuple, inventory: set[str]) -> set[str] | None:
    selected: set[str] = set()
    for prefix in prefixes:
        matched = {path for path in inventory if _matches(path, (prefix,))}
        if not matched:
            return None
        selected.update(matched)
    return selected


def _full(request: C.RunRequest, snapshot: C.InputSnapshot, code: str, message: str) -> C.Plan:
    return C.Plan(mode=request.mode, execution="full", reasons=(_reason(code, message),),
                  input_digest=snapshot.digest, compatibility=snapshot.compatibility, static_preview=True)


def choose_plan(config: C.Config, snapshot: C.InputSnapshot,
                history: C.HistoryView, request: C.RunRequest) -> C.Plan:
    """Choose only proven group unions; ambiguity is an actual full gate."""
    if request.mode is C.Mode.FULL:
        return _full(request, snapshot, "full-gate-obligation", "explicit full request")
    if request.mode is C.Mode.SCOPED:
        return C.Plan(mode=request.mode, execution="scoped",
                      input_digest=snapshot.digest, compatibility=snapshot.compatibility, static_preview=True)
    policy = config.selection
    if not policy.enabled or not policy.closed_inputs:
        return _full(request, snapshot, "selection-disabled", "selection is not explicitly closed")
    if _invalid_policy(policy, config.runner.test_roots):
        return _full(request, snapshot, "policy-invalid", "exclusion overlaps an input contract or contains an unsafe prefix")
    if snapshot.digest is None or snapshot.compatibility is None or snapshot.limitations or history.limitations:
        return _full(request, snapshot, "unknown-input", "static input evidence is incomplete")
    baseline = history.baseline
    if baseline is None:
        return _full(request, snapshot, "no-baseline", "no compatible baseline exists")
    if history.selection_disabled:
        return _full(request, snapshot, "selection-disabled", "history disabled selection")
    if snapshot.head is None or snapshot.baseline_head != baseline.head:
        return _full(request, snapshot, "incompatible-baseline", "snapshot does not cover this baseline's committed delta")
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
        if change.kind in {"deleted", "renamed"} and paths and paths[0] in inventory:
            return _full(request, snapshot, "incomplete-inventory", "inventory test was deleted or renamed")
        for path in paths:
            if _matches(path, policy.full_triggers + (".ptest.toml",)):
                return _full(request, snapshot, "policy-changed", "full-trigger input changed")
            if _matches(path, policy.no_tests):
                continue
            if change.kind in {"untracked", "ignored"} and _matches(path, policy.non_input_outputs):
                continue
            if change.kind == "ignored" and not _matches(path, policy.ignored_inputs):
                return _full(request, snapshot, "unknown-input", "ignored input is not declared")
            involved = [group for group in policy.groups if _matches(path, group.sources) or _matches(path, group.tests)]
            if not involved:
                return _full(request, snapshot, "unknown-input", "changed path has no declared group")
            if path not in inventory and any(_matches(path, group.tests) for group in involved):
                return _full(request, snapshot, "incomplete-inventory", "changed test is absent from inventory")
            for group in involved:
                expanded = _expand(group.tests, inventory)
                if expanded is None:
                    return _full(request, snapshot, "incomplete-inventory", "group test prefix is absent from inventory")
                selected.update(expanded)
            if path in inventory:
                selected.add(path)
    if not selected:
        return C.Plan(mode=request.mode, execution="none", reasons=(_reason("no-tests-needed", "no relevant inputs changed"),),
                      input_digest=snapshot.digest, compatibility=snapshot.compatibility,
                      baseline_run_id=baseline.run_id, static_preview=True)
    always = _expand(policy.always, inventory)
    if always is None:
        return _full(request, snapshot, "incomplete-inventory", "always test prefix is absent from inventory")
    selected.update(always)
    if not selected.issubset(inventory):
        return _full(request, snapshot, "incomplete-inventory", "selected test is absent from inventory")
    if len(selected) / max(1, len(inventory)) >= policy.full_ratio:
        return _full(request, snapshot, "full-gate-obligation", "selection reaches full ratio")
    return C.Plan(mode=request.mode, execution="selected", files=tuple(sorted(selected)),
                  input_digest=snapshot.digest, compatibility=snapshot.compatibility,
                  baseline_run_id=baseline.run_id, static_preview=True)
