"""N9: 0.5 history stays decodable by 0.4.10 (T5, history unchanged).

No new RunResult field, reason code or persisted text: the frozen
0.4.10 field and code lists below are copied from
``git show f478189:src/ptest/contracts.py`` (run payload validator and
``serialize_run_result`` converters) and ``src/ptest/history.py``
(``_PERSISTED_REASON_ALIASES``). Dynamic scoped results, full results
and static-fallback results publish summaries that decode with only
those keys and codes.
"""
from __future__ import annotations

from ptest import contracts as C
from ptest import history as H


# Frozen at f478189 (0.4.10), copied from contracts.py REASON_CODES.
FROZEN_REASON_CODES = frozenset({
    "initialization-required", "unsupported-platform", "unsupported-capability",
    "invalid-config", "unsafe-path",
    "missing-executable", "nested-invocation", "queue-timeout",
    "coordinator-unavailable", "coordinator-corrupt", "protocol-mismatch",
    "capacity-exceeded", "ownership-uncertain",
    "unsupported-detached-descendant", "no-baseline", "incompatible-baseline",
    "unknown-input", "policy-invalid", "policy-changed", "prior-failure",
    "full-gate-obligation", "project-filtered", "changed-during-run", "incomplete-inventory",
    "report-invalid", "state-unavailable", "no-tests-needed",
    "selection-disabled", "selection-not-closed", "scan-limit", "static-evidence-insufficient",
    "probe-isolation-required", "unredacted-command-disclosure",
    "already-exists", "invalid-bound", "execution-timeout",
    "attempt-decision-timeout", "selection-shadow-quarantine",
    "probe-no-conflict-observed", "probe-conflict-observed",
    "parallel-workers",
    "config-uncommitted",
    "post-test-stall",
})

# Frozen persisted-code aliases at f478189.
FROZEN_PERSISTED_REASON_ALIASES = {"post-test-stall": "state-unavailable"}

# Frozen serialize_run_result key sets at f478189 (top level and nested).
FROZEN_RUN_KEYS = frozenset({
    "run_id", "project_id", "checkout_id", "mode", "status", "phase",
    "started_at", "finished_at", "plan", "command", "granted_workers",
    "memory_estimate_mb", "reserved_memory_mb", "runner_exit_code",
    "exit_code", "exit_origin", "signal", "source_valid",
    "full_gate_eligible", "baseline_published", "counts", "timings",
    "attempts", "reasons", "limitations", "artifact_id",
})
FROZEN_PLAN_KEYS = frozenset({
    "mode", "execution", "files", "reasons", "input_digest",
    "compatibility", "baseline_run_id", "static_preview",
})
FROZEN_COMMAND_KEYS = frozenset({
    "kind", "mode", "argument_count", "generated_options", "workers",
    "provenance",
})
FROZEN_COUNTS_KEYS = frozenset({
    "collected", "executed", "passed", "failed", "skipped", "unknown",
})
FROZEN_TIMINGS_KEYS = frozenset({
    "queue", "setup", "collection", "execution", "finalization",
})
FROZEN_ATTEMPT_KEYS = frozenset({
    "attempt_id", "phase", "status", "raw_exit_code", "final_exit_code",
    "source_valid", "inventory_complete", "timings",
})
FROZEN_REASON_KEYS = frozenset({"code", "message", "paths"})


def test_reason_codes_match_0_4_10():
    assert C.REASON_CODES == FROZEN_REASON_CODES


def test_persisted_reason_aliases_unchanged():
    assert H._PERSISTED_REASON_ALIASES == FROZEN_PERSISTED_REASON_ALIASES


def _snapshot(case, **overrides):
    fields = {"digest": "11" * 32, "compatibility": "compat-v1",
              "clean": True, "changes": ()}
    fields.update(overrides)
    return case.snapshot(**fields)


def _result(case, sequence, checkout, *, mode, execution, reasons=(),
            before=None, after=None):
    plan = C.Plan(mode=mode, execution=execution,
                  input_digest=None if after is None else after.digest,
                  compatibility=None if after is None
                  else after.compatibility,
                  reasons=reasons)
    return case.result(
        sequence=sequence, status="passed", mode=mode, phase="complete",
        plan=plan, input_before=before, input_after=after,
        policy_digest="22" * 32, full_gate_eligible=(execution == "full"),
        project_id=checkout.project_id, checkout_id=checkout.checkout_id)


def _codes(value):
    found = set()

    def visit(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "code" and isinstance(item, str):
                    found.add(item)
                else:
                    visit(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)

    visit(value)
    return found


def test_dynamic_scoped_full_and_static_results_decode_frozen(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)

    dynamic = _result(case, 1, checkout, mode=C.Mode.SCOPED,
                      execution="scoped", before=snapshot, after=snapshot)
    full = _result(case, 2, checkout, mode=C.Mode.FULL, execution="full",
                   before=snapshot, after=snapshot)
    static = _result(case, 3, checkout, mode=C.Mode.SCOPED,
                     execution="scoped", before=snapshot, after=snapshot,
                     reasons=(C.Reason(code="selection-disabled",
                                       message="static fallback"),))
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    for result in (dynamic, full, static):
        assert H.publish_outcome(domain, checkout, result,
                                 inventory).committed

    summaries = H.read_history_summaries(domain, checkout)
    assert len(summaries) == 3
    for summary in summaries:
        assert set(summary) <= FROZEN_RUN_KEYS
        assert set(summary["plan"]) <= FROZEN_PLAN_KEYS
        assert set(summary["command"]) <= FROZEN_COMMAND_KEYS
        if summary["counts"] is not None:
            assert set(summary["counts"]) <= FROZEN_COUNTS_KEYS
        if summary["timings"] is not None:
            assert set(summary["timings"]) <= FROZEN_TIMINGS_KEYS
        for attempt in summary["attempts"]:
            assert set(attempt) <= FROZEN_ATTEMPT_KEYS
            if attempt["timings"] is not None:
                assert set(attempt["timings"]) <= FROZEN_TIMINGS_KEYS
        for bucket in (summary["reasons"], summary["limitations"],
                       summary["plan"]["reasons"]):
            for reason in bucket:
                assert set(reason) <= FROZEN_REASON_KEYS
        assert _codes(summary) <= FROZEN_REASON_CODES
        # The frozen 0.4.10 descriptor accepts the persisted row as is.
        C.decode_public_document(C.encode_public_document("run", summary))


def test_no_selection_fields_leak_into_summaries(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    result = _result(case, 1, checkout, mode=C.Mode.SCOPED,
                     execution="scoped", before=snapshot, after=snapshot)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, result, inventory).committed

    (summary,) = H.read_history_summaries(domain, checkout)
    blob = repr(summary)
    for token in ("deselect", "dynamic", "selection audit", "demot",
                  "compatibility fingerprint"):
        assert token not in blob
