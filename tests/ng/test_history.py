"""Behavioral contracts for durable local history and selection evidence."""
from __future__ import annotations

import json
import sqlite3

import pytest

from ptest import contracts as C
from ptest import history as H


def _snapshot(case, *, digest="11" * 32, compatibility="compat-v1", clean=True,
              changes=()):
    return case.snapshot(
        digest=digest,
        compatibility=compatibility,
        clean=clean,
        changes=changes,
    )


def _result(case, sequence, *, status="passed", mode=C.Mode.FULL,
            before=None, after=None, policy_digest="22" * 32,
            full_gate_eligible=True, phase="complete", checkout=None,
            **overrides):
    after = after if after is not None else before
    plan = C.Plan(
        mode=mode,
        execution="full" if mode is C.Mode.FULL else "scoped",
        input_digest=None if after is None else after.digest,
        compatibility=None if after is None else after.compatibility,
    )
    if checkout is not None:
        overrides["project_id"] = checkout.project_id
        overrides["checkout_id"] = checkout.checkout_id
    return case.result(
        sequence=sequence,
        status=status,
        mode=mode,
        phase=phase,
        plan=plan,
        input_before=before,
        input_after=after,
        policy_digest=policy_digest,
        full_gate_eligible=full_gate_eligible,
        **overrides,
    )


def _publish_failure(case, domain, checkout, *, sequence=1,
                     file="tests/test_a.py", test_id=None,
                     compatibility="compat-v1"):
    inventory = case.inventory((file,), outcome="failed")
    if test_id is not None:
        record = C.TestRecord(
            id=test_id, file=file, outcome=C.Outcome.FAILED,
            setup_s=None, call_s=0.01, teardown_s=None,
        )
        inventory = C.Inventory(
            adapter="pytest", version="9.1.1", complete=True,
            tests=(record,), digest="33" * 32,
        )
    snapshot = _snapshot(case, compatibility=compatibility)
    H.publish_outcome(
        domain, checkout,
        _result(case, sequence, status="failed", before=snapshot, after=snapshot,
                checkout=checkout),
        inventory,
    )


def test_skip_does_not_clear_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    publish_outcome = H.publish_outcome
    publish_outcome(
        domain, checkout, _result(case, 1, status="failed"),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    publish_outcome(
        domain, checkout, _result(case, 2, status="passed"),
        case.inventory(("tests/test_a.py",), outcome="skipped"),
    )
    assert H.read_history(domain, checkout).obligations[0].file == "tests/test_a.py"


def test_complete_pass_clears_only_the_matching_newer_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=4)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    snapshot = _snapshot(case)
    H.publish_outcome(
        domain, checkout,
        _result(case, 5, before=snapshot, after=snapshot, checkout=checkout),
        passed,
    )
    assert H.read_history(domain, checkout).obligations == ()


def test_unrelated_pass_does_not_clear_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout)
    snapshot = _snapshot(case)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, mode=C.Mode.SCOPED, before=snapshot, after=snapshot,
                checkout=checkout),
        case.inventory(("tests/test_other.py",), outcome="passed"),
    )
    obligation = H.read_history(domain, checkout).obligations[0]
    assert obligation.file == "tests/test_a.py"
    assert obligation.test_id == "tests/test_a.py::test_x"


def test_late_older_pass_cannot_clear_newer_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=9)
    snapshot = _snapshot(case)
    H.publish_outcome(
        domain, checkout,
        _result(case, 8, before=snapshot, after=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations


def test_incompatible_pass_does_not_clear_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, compatibility="pytest-v1")
    snapshot = _snapshot(case, compatibility="pytest-v2")
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=snapshot, after=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations


def test_failed_full_without_test_id_is_a_whole_gate_obligation(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    result = _result(
        case, 1, status="failed", before=None, after=None,
        runner_exit_code=1, exit_code=1, checkout=checkout,
    )
    published = H.publish_outcome(domain, checkout, result, None)
    assert published.committed is True
    obligation = H.read_history(domain, checkout).obligations[0]
    assert obligation.file is None
    assert obligation.test_id is None
    assert obligation.reason == "full-gate-obligation"


def test_coverage_failure_obligation_requires_a_clean_successful_full_gate(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    failed = _result(
        case, 1, status="failed", before=None, after=None,
        runner_exit_code=1, exit_code=1,
        checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, failed,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert any(
        item.file is None and item.reason == "full-gate-obligation"
        for item in H.read_history(domain, checkout).obligations
    )
    snapshot = _snapshot(case)
    dirty = _snapshot(case, clean=False)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=dirty, after=dirty, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert any(
        item.file is None and item.reason == "full-gate-obligation"
        for item in H.read_history(domain, checkout).obligations
    )


def test_dirty_full_pass_cannot_publish_or_replace_baseline(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    clean = _snapshot(case)
    first = H.publish_outcome(
        domain, checkout, _result(case, 1, before=clean, after=clean, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert first.baseline_published is True
    baseline_run = H.read_history(domain, checkout).baseline.run_id
    dirty = _snapshot(case, clean=False, changes=(C.Change(old=None, new="x.py", kind="modified"),))
    result = _result(case, 2, before=dirty, after=dirty, checkout=checkout)
    published = H.publish_outcome(
        domain, checkout, result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.baseline_published is False
    assert H.read_history(domain, checkout).baseline.run_id == baseline_run


def test_mixed_source_full_pass_cannot_seed_baseline(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    before = _snapshot(case, digest="44" * 32)
    after = _snapshot(case, digest="55" * 32)
    published = H.publish_outcome(
        domain, checkout, _result(case, 1, before=before, after=after,
                                  checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.baseline_published is False
    assert H.read_history(domain, checkout).baseline is None


def test_incomplete_or_teardown_failure_cannot_seed_baseline(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    incomplete = _result(
        case, 1, status="incomplete", before=snapshot, after=snapshot,
        source_valid=False, full_gate_eligible=False,
        checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, incomplete,
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    assert H.read_history(domain, checkout).baseline is None
    assert H.read_history(domain, checkout).obligations


def test_teardown_failure_is_not_hidden_by_a_passing_inventory(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    attempt = C.AttemptResult(
        attempt_id="a001", phase="execution", status="failed",
        raw_exit_code=1, final_exit_code=1, source_valid=True,
        inventory_complete=True,
    )
    result = _result(
        case, 1, before=snapshot, after=snapshot, checkout=checkout,
        attempts=(attempt,),
    )
    published = H.publish_outcome(
        domain, checkout, result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.baseline_published is False
    assert any(item.file is None for item in H.read_history(domain, checkout).obligations)


def test_clean_complete_full_pass_publishes_baseline_and_history_payload(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case, digest="88" * 32)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    result = _result(case, 7, before=snapshot, after=snapshot, checkout=checkout)
    published = H.publish_outcome(domain, checkout, result, inventory)
    assert published.committed is True
    assert published.baseline_published is True
    baseline = H.read_history(domain, checkout).baseline
    assert baseline is not None
    assert baseline.run_id == result.run_id
    assert baseline.input_digest == snapshot.digest
    assert baseline.inventory == inventory
    payload = H.read_history_payload(domain, checkout)
    assert payload["summaries"] == [C.serialize_run_result(result)]
    assert payload["obligations"] == []


def test_deleted_id_reconciles_only_after_a_complete_clean_full_inventory(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    snapshot = _snapshot(case)
    full = _result(case, 2, before=snapshot, after=snapshot, checkout=checkout)
    H.publish_outcome(domain, checkout, full, case.inventory((), outcome="passed"))
    assert H.read_history(domain, checkout).obligations == ()


def test_unknown_or_incomplete_inventory_cannot_clear_obligation_or_seed_baseline(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    snapshot = _snapshot(case)
    unknown = case.inventory(("tests/test_a.py",), outcome="unknown", complete=False)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=snapshot, after=snapshot, checkout=checkout),
        unknown,
    )
    view = H.read_history(domain, checkout)
    assert view.baseline is None
    assert view.obligations


def test_history_summaries_reuse_public_run_serializer_without_private_fields(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case, digest="66" * 32)
    policy_digest = "77" * 32
    result = _result(
        case, 1, before=snapshot, after=snapshot, policy_digest=policy_digest,
        checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    summary = H.read_history_summaries(domain, checkout)[0]
    assert summary == C.serialize_run_result(result)
    assert "sequence" not in summary
    assert "input_before" not in summary
    assert "input_after" not in summary
    assert "policy_digest" not in summary
    assert policy_digest not in json.dumps(summary, sort_keys=True)


def test_republishing_a_run_id_cannot_mutate_its_immutable_evidence(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    result = _result(case, 1, before=snapshot, after=snapshot, checkout=checkout)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, result, passed).committed is True
    conflicting = case.inventory(("tests/test_a.py",), outcome="failed")
    retry = H.publish_outcome(domain, checkout, result, conflicting)
    assert retry.committed is False
    assert retry.selection_disabled is True
    assert H.read_history(domain, checkout).selection_disabled is True


def test_absent_history_is_read_only_and_does_not_create_state(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    view = H.read_history(domain, checkout)
    assert view.baseline is None
    assert view.obligations == ()
    assert view.selection_disabled is False
    assert not (domain.root / "checkouts").exists()
    assert not (domain.root / "input-hmac.key").exists()
    assert not domain.ledger.exists()


def test_history_rejects_a_checkout_id_that_does_not_match_the_explicit_root(case):
    domain = case.domain()
    valid = case.checkout(domain)
    forged = C.CheckoutIdentity(
        project_id=valid.project_id, checkout_id="cc" * 16, root=valid.root,
    )
    with pytest.raises(C.Problem, match="unsafe-path"):
        H.read_history(domain, forged)
    assert not (domain.root / "checkouts").exists()


def test_corrupt_private_history_evidence_disables_selection(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    result = _result(case, 1, before=snapshot, after=snapshot, checkout=checkout)
    H.publish_outcome(
        domain, checkout, result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE runs SET input_before = ? WHERE run_id = ?",
        (json.dumps({"clean": "not-a-bool"}), result.run_id),
    )
    connection.commit()
    connection.close()
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert any(reason.code == "coordinator-corrupt" for reason in view.limitations)
    assert (path.parent / "history-disabled.json").exists()


def test_corrupt_baseline_reference_disables_selection_without_repairing_store(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    result = _result(case, 1, before=snapshot, after=snapshot, checkout=checkout)
    H.publish_outcome(
        domain, checkout, result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    connection = sqlite3.connect(path)
    forged = "cc" * 16
    connection.execute(
        "UPDATE baselines SET run_id = ? WHERE singleton = 1", (forged,)
    )
    connection.commit()
    connection.close()
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert any(reason.code == "coordinator-corrupt" for reason in view.limitations)
    assert path.exists()


def test_other_domain_and_checkout_cannot_read_or_clear_history(case):
    first = case.domain()
    first_checkout = case.checkout(first)
    _publish_failure(case, first, first_checkout)
    second = case.domain()
    second_checkout = case.checkout(second)
    assert H.read_history(second, second_checkout).obligations == ()
    assert H.read_history(first, first_checkout).obligations


def test_corrupt_history_disables_selection_without_repairing_or_deleting_store(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout)
    path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    path.write_bytes(b"not-a-sqlite-database")
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert any(reason.code == "coordinator-corrupt" for reason in view.limitations)
    assert path.read_bytes() == b"not-a-sqlite-database"


def test_retention_is_bounded_and_protected_obligation_survives(case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 2)
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    results = []
    for sequence in (2, 3, 4):
        result = _result(case, sequence, status="passed", checkout=checkout)
        results.append(result)
        H.publish_outcome(
            domain, checkout, result,
            case.inventory((f"tests/test_{sequence}.py",), outcome="passed"),
        )
    summaries = H.read_history_summaries(domain, checkout)
    assert len(summaries) <= 2
    assert [item["run_id"] for item in summaries] == [
        results[2].run_id, results[1].run_id,
    ]
    assert H.read_history(domain, checkout).obligations


def test_retention_keeps_the_usable_baseline_even_when_it_exceeds_summary_cap(case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 1)
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline_result = _result(
        case, 1, before=snapshot, after=snapshot, checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, baseline_result,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    dirty = _snapshot(case, clean=False)
    H.publish_outcome(
        domain, checkout, _result(case, 2, before=dirty, after=dirty, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    view = H.read_history(domain, checkout)
    assert view.baseline is not None
    assert view.baseline.run_id == baseline_result.run_id
    assert len(H.read_history_summaries(domain, checkout, limit=2)) == 2


def test_oversized_history_is_uncertain_and_does_not_fallback_to_normal_state(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout)
    history_path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    assert history_path.exists()
    monkeypatch.setattr(H, "HISTORY_MAX_BYTES", 1)
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert any(reason.code == "capacity-exceeded" for reason in view.limitations)
    assert not (domain.root / "normal-state-sentinel").exists()
