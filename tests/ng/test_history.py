"""Behavioral contracts for durable local history and selection evidence."""
from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import io
import json
import os
import pwd
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import files as F
from ptest import history as H


def _tree_snapshot(root: Path) -> tuple:
    if not root.exists():
        return ()
    paths = (root, *sorted(root.rglob("*"), key=lambda item: item.as_posix()))
    snapshot = []
    for path in paths:
        stat = path.lstat()
        data = path.read_bytes() if path.is_file() else None
        snapshot.append((
            path.relative_to(root).as_posix(), stat.st_mode, stat.st_uid,
            stat.st_nlink, stat.st_ino, stat.st_size, stat.st_mtime_ns, data,
        ))
    return tuple(snapshot)


def _normal_home(case, monkeypatch) -> Path:
    home = F.ensure_private_dir(case.base, "account-home")
    local = F.ensure_private_dir(home, ".local")
    state = F.ensure_private_dir(local, "state")
    ptest = F.ensure_private_dir(state, "ptest")
    coordination = F.ensure_private_dir(ptest, "coordination")
    config = F.ensure_private_dir(home, ".config")
    config_ptest = F.ensure_private_dir(config, "ptest")
    F.create_exclusive(config_ptest, "machine.toml", b"max_slots = 1\n")
    F.create_exclusive(coordination, "normal-ledger", b"fixture-owned\n")
    account = pwd.getpwuid(os.getuid())
    real_home = Path(account.pw_dir)
    monkeypatch.setenv("HOME", str(home))
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(variable, raising=False)
    fake_account = pwd.struct_passwd((*account[:5], str(home), account.pw_shell))
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: fake_account)
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: fake_account)
    # T2 is absent in this wave-1 checkout. When integrated, its account
    # lookup uses the same patched pwd module; forbid normal resolution too.
    if importlib.util.find_spec("ptest.platform") is not None:
        from ptest import platform

        def forbidden_domain(*_args, **_kwargs):
            pytest.fail("history tried to resolve a normal domain")

        monkeypatch.setattr(platform, "domain_paths", forbidden_domain)

    def guard_open(original):
        def guarded(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and kwargs.get("dir_fd") is None:
                target = Path(os.fsdecode(path)).absolute()
                assert not target.is_relative_to(real_home), "real account-home access"
            return original(path, *args, **kwargs)
        return guarded

    for module, name in ((builtins, "open"), (io, "open"),
                         *((os, name) for name in ("open", "stat", "lstat", "mkdir", "rmdir", "unlink", "chmod"))):
        monkeypatch.setattr(module, name, guard_open(getattr(module, name)))
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        from urllib.parse import unquote
        target = unquote(os.fsdecode(database)).removeprefix("file:").split("?", 1)[0]
        assert not Path(target).absolute().is_relative_to(real_home), "real account SQLite access"
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    return home


def _store_path(domain, checkout):
    return domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"


def _downgrade_store_to_pre_t12a(path: Path) -> None:
    """Remove only T12a's additive schema, preserving a valid base store."""
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE selection_quarantine")
    connection.execute("DROP TABLE attempt_evidence")
    connection.execute("DROP TABLE comparison_receipts")
    connection.execute("ALTER TABLE baselines RENAME TO baselines_t12a")
    connection.execute(
        "CREATE TABLE baselines ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "run_id TEXT NOT NULL, sequence INTEGER NOT NULL, head TEXT NOT NULL, "
        "input_digest TEXT NOT NULL, compatibility TEXT NOT NULL, "
        "inventory TEXT NOT NULL, policy_digest TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO baselines "
        "SELECT singleton, run_id, sequence, head, input_digest, compatibility, "
        "inventory, policy_digest, created_at FROM baselines_t12a"
    )
    connection.execute("DROP TABLE baselines_t12a")
    connection.commit()
    connection.close()


def _full_error():
    error = sqlite3.OperationalError("database or disk is full")
    error.sqlite_errorcode = sqlite3.SQLITE_FULL
    return error


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


def _shadow_outcome(case, checkout, *, sequence, run_id, snapshot,
                    policy_digest, full_outcome="failed",
                    expected_quarantine=None, baseline_run_id=None):
    selected_result = C.AttemptResult(
        attempt_id="a001", phase="execution", status=C.Status.PASSED,
        raw_exit_code=0, final_exit_code=0, source_valid=True,
        inventory_complete=True)
    full_status = (C.Status.PASSED if full_outcome == "passed"
                   else C.Status.FAILED)
    full_code = 0 if full_status is C.Status.PASSED else 23
    full_result = C.AttemptResult(
        attempt_id="a002", phase="execution", status=full_status,
        raw_exit_code=full_code, final_exit_code=full_code,
        source_valid=True, inventory_complete=True)
    selected_inventory = case.inventory(
        ("tests/test_a.py",), outcome="passed")
    full_inventory = case.inventory(
        ("tests/test_a.py", "tests/test_b.py"), outcome="passed")
    if full_outcome == "failed":
        full_inventory = replace(
            full_inventory,
            tests=(full_inventory.tests[0], replace(
                full_inventory.tests[1], outcome=C.Outcome.FAILED)),
        )
    selected_plan = C.Plan(
        mode=C.Mode.SHADOW, execution="selected",
        files=("tests/test_a.py",), input_digest=snapshot.digest,
        compatibility=snapshot.compatibility,
        baseline_run_id=baseline_run_id)
    result = replace(_result(
        case, sequence, run_id=run_id, mode=C.Mode.SHADOW,
        status=full_status, before=snapshot, after=snapshot,
        policy_digest=policy_digest, checkout=checkout,
        runner_exit_code=full_code, exit_code=full_code,
        full_gate_eligible=False,
        attempts=(selected_result, full_result)), plan=selected_plan)
    selected = C.AttemptEvidence(
        attempt_id="a001", result=selected_result,
        inventory=selected_inventory, terminal_complete=True,
        parallel_identity=False, runtime_identity="66" * 32)
    full = C.AttemptEvidence(
        attempt_id="a002", result=full_result, inventory=full_inventory,
        terminal_complete=True, parallel_identity=False,
        runtime_identity="66" * 32)
    verdict = "matched" if full_outcome == "passed" else "suspected-miss"
    return result, C.ShadowComparison(
        selected=selected, full=full, verdict=verdict,
        expected_quarantine=expected_quarantine)


def _evidence(attempt_id, records, *, status=C.Status.PASSED,
              terminal_complete=True, inventory_complete=True):
    code = 0 if status is C.Status.PASSED else 23
    attempt = C.AttemptResult(
        attempt_id=attempt_id, phase="execution", status=status,
        raw_exit_code=code, final_exit_code=code, source_valid=True,
        inventory_complete=inventory_complete,
    )
    inventory = C.Inventory(
        adapter="pytest", version="9.1.1", complete=inventory_complete,
        tests=tuple(
            C.TestRecord(
                id=f"{file}::{name}", file=file, outcome=outcome,
                setup_s=None, call_s=0.01, teardown_s=None,
            )
            for file, name, outcome in records
        ),
        digest="55" * 32,
    )
    return C.AttemptEvidence(
        attempt_id=attempt_id, result=attempt, inventory=inventory,
        terminal_complete=terminal_complete, parallel_identity=False,
        runtime_identity="66" * 32,
    )


def _seed_quarantine(case, domain, checkout):
    snapshot = _snapshot(case, compatibility="compat-v1")
    policy = "22" * 32
    baseline = replace(_result(
        case, 0, before=snapshot, after=snapshot,
        policy_digest=policy, checkout=checkout), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    divergent, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="1" * 32,
        snapshot=snapshot, policy_digest=policy,
        baseline_run_id=baseline.run_id,
    )
    assert H.publish_shadow_outcome(
        domain, checkout, divergent, comparison).committed
    quarantine = H.read_history(domain, checkout).selection_quarantine
    assert quarantine is not None
    return snapshot, policy, quarantine


@pytest.mark.parametrize(
    ("selected_records", "selected_status", "full_records", "full_status",
     "terminal_complete", "expected"),
    (
        (
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),), C.Status.PASSED,
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),
             ("tests/test_b.py", "test_b", C.Outcome.PASSED)), C.Status.PASSED,
            True, "matched",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.FAILED),), C.Status.FAILED,
            (("tests/test_a.py", "test_a", C.Outcome.FAILED),
             ("tests/test_b.py", "test_b", C.Outcome.PASSED)), C.Status.FAILED,
            True, "matched",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.FAILED),), C.Status.FAILED,
            (("tests/test_a.py", "test_a", C.Outcome.FAILED),
             ("tests/test_b.py", "test_b", C.Outcome.FAILED)), C.Status.FAILED,
            True, "suspected-miss",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),), C.Status.PASSED,
            (("tests/test_a.py", "test_a", C.Outcome.FAILED),
             ("tests/test_b.py", "test_b", C.Outcome.PASSED)), C.Status.FAILED,
            True, "unclassified-divergence",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),), C.Status.PASSED,
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),
             ("tests/test_b.py", "test_b", C.Outcome.FAILED)), C.Status.FAILED,
            True, "suspected-miss",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),), C.Status.PASSED,
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),
             ("tests/test_b.py", "test_b", C.Outcome.PASSED)), C.Status.FAILED,
            True, "unclassified-divergence",
        ),
        (
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),), C.Status.PASSED,
            (("tests/test_a.py", "test_a", C.Outcome.PASSED),
             ("tests/test_b.py", "test_b", C.Outcome.PASSED)), C.Status.PASSED,
            False, "incomplete",
        ),
    ),
)
def test_shadow_verdict_uses_test_identity_and_inventory(
        selected_records, selected_status, full_records, full_status,
        terminal_complete, expected):
    selected = _evidence(
        "a001", selected_records, status=selected_status,
        terminal_complete=terminal_complete,
    )
    full = _evidence(
        "a002", full_records, status=full_status,
        terminal_complete=terminal_complete,
    )

    assert H._derived_shadow_verdict(selected, full) == expected


@pytest.mark.parametrize(
    ("selected_identity", "full_identity"),
    (
        (None, "66" * 32),
        ("66" * 32, None),
        ("66" * 32, "77" * 32),
    ),
)
def test_shadow_verdict_requires_matching_observed_runtime_identities(
        selected_identity, full_identity):
    selected = replace(_evidence(
        "a001", (("tests/test_a.py", "test_a", C.Outcome.PASSED),),
    ), runtime_identity=selected_identity)
    full = replace(_evidence(
        "a002",
        (("tests/test_a.py", "test_a", C.Outcome.PASSED),
         ("tests/test_b.py", "test_b", C.Outcome.FAILED)),
        status=C.Status.FAILED,
    ), runtime_identity=full_identity)

    assert H._derived_shadow_verdict(selected, full) == "incomplete"


@pytest.mark.parametrize(
    ("selected_identity", "full_identity"),
    (
        (None, "66" * 32),
        ("66" * 32, None),
        ("66" * 32, "77" * 32),
    ),
)
def test_incomplete_runtime_identity_comparison_persists_without_quarantine_transition(
        case, selected_identity, full_identity):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot, policy, quarantine = _seed_quarantine(case, domain, checkout)
    result, comparison = _shadow_outcome(
        case, checkout, sequence=2, run_id="2" * 32,
        snapshot=snapshot, policy_digest=policy,
        expected_quarantine=quarantine, baseline_run_id="0" * 32,
    )
    comparison = replace(
        comparison,
        selected=replace(
            comparison.selected, runtime_identity=selected_identity),
        full=replace(comparison.full, runtime_identity=full_identity),
        verdict="incomplete",
    )

    published = H.publish_shadow_outcome(
        domain, checkout, result, comparison)

    assert published.committed is True
    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        evidence = connection.execute(
            "SELECT attempt_id, runtime_identity FROM attempt_evidence "
            "WHERE run_id = ? ORDER BY attempt_id", (result.run_id,),
        ).fetchall()
        receipt = connection.execute(
            "SELECT verdict FROM comparison_receipts WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        obligation = connection.execute(
            "SELECT sequence, reason FROM obligations "
            "WHERE obligation_key = ?", ("test:tests/test_b.py::test_x",),
        ).fetchone()
    assert evidence == [
        ("a001", selected_identity), ("a002", full_identity)]
    assert receipt == ("incomplete",)
    assert obligation == (2, "prior-failure")
    assert H.read_history(domain, checkout).selection_quarantine == quarantine


@pytest.mark.parametrize(
    ("interruption", "expected_evidence"),
    (("timeout", ("a001", "a002")),
     ("cancelled", ("a001",)),
     ("source-invalidated", ("a001",))),
)
def test_incomplete_shadow_persists_observations_and_whole_gate_obligation(
        case, interruption, expected_evidence):
    domain = case.domain()
    checkout = case.checkout(domain)
    before = _snapshot(case)
    baseline = replace(_result(
        case, 0, before=before, after=before, checkout=checkout,
        policy_digest="22" * 32,
    ), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published

    selected = _evidence(
        "a001", (("tests/test_a.py", "test_a", C.Outcome.FAILED),),
        status=C.Status.FAILED,
    )
    not_run = C.AttemptResult(
        attempt_id="a002", phase="execution", status=C.Status.NOT_RUN,
        raw_exit_code=None, final_exit_code=None, source_valid=False,
        inventory_complete=False,
    )
    after = before
    status = C.Status.INCOMPLETE
    signal = None
    reasons = (C.Reason(code="execution-timeout", message="full timed out"),)
    full = None
    if interruption == "timeout":
        full_result = C.AttemptResult(
            attempt_id="a002", phase="execution", status=C.Status.INCOMPLETE,
            raw_exit_code=-15, final_exit_code=70, source_valid=True,
            inventory_complete=False,
        )
        full = C.AttemptEvidence(
            attempt_id="a002", result=full_result, inventory=None,
            terminal_complete=False, parallel_identity=False,
            runtime_identity="66" * 32,
        )
    else:
        full_result = not_run
        if interruption == "cancelled":
            status = C.Status.CANCELLED
            signal = 2
            reasons = ()
        else:
            after = _snapshot(case, digest="77" * 32)
            reasons = (C.Reason(
                code="changed-during-run", message="source changed"),)
    plan = C.Plan(
        mode=C.Mode.SHADOW, execution="selected",
        files=("tests/test_a.py",), input_digest=before.digest,
        compatibility=before.compatibility, baseline_run_id=baseline.run_id,
    )
    result = replace(_result(
        case, 1, run_id="7" * 32, mode=C.Mode.SHADOW, status=status,
        before=before, after=after, checkout=checkout,
        policy_digest="22" * 32, runner_exit_code=17, exit_code=17,
        source_valid=interruption != "source-invalidated",
        full_gate_eligible=False,
        attempts=(selected.result, full_result), reasons=reasons,
    ), plan=plan, signal=signal)
    comparison = C.ShadowComparison(
        selected=selected, full=full, verdict="incomplete",
        expected_quarantine=None,
    )

    published = H.publish_shadow_outcome(
        domain, checkout, result, comparison)

    assert published.committed is True
    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert tuple(row[0] for row in connection.execute(
            "SELECT attempt_id FROM attempt_evidence WHERE run_id = ? "
            "ORDER BY attempt_id", (result.run_id,),
        )) == expected_evidence
        obligations = connection.execute(
            "SELECT obligation_key, reason FROM obligations ORDER BY obligation_key"
        ).fetchall()
    assert ("test:tests/test_a.py::test_a", "prior-failure") in obligations
    assert ("whole-gate", "full-gate-obligation") in obligations
    assert H.read_history(domain, checkout).selection_quarantine is None


def test_shadow_quarantine_requires_exact_newer_corrected_comparison(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    old_snapshot = _snapshot(case, compatibility="compat-v1")
    old_policy = "22" * 32
    initial = replace(_result(
        case, 0, before=old_snapshot, after=old_snapshot,
        policy_digest=old_policy, checkout=checkout), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, initial,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed")).baseline_published
    divergent, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="1" * 32,
        snapshot=old_snapshot, policy_digest=old_policy,
        baseline_run_id=initial.run_id)

    published = H.publish_shadow_outcome(
        domain, checkout, divergent, comparison)
    assert published.committed is True
    quarantine = H.read_history(domain, checkout).selection_quarantine
    assert quarantine is not None
    assert quarantine.verdict == "suspected-miss"
    assert any(item.code == "selection-shadow-quarantine"
               for item in H.read_history(domain, checkout).limitations)

    ordinary = replace(
        _result(case, 2, before=old_snapshot, after=old_snapshot,
                checkout=checkout),
        run_id="2" * 32)
    assert H.publish_outcome(
        domain, checkout, ordinary,
        case.inventory(("tests/test_a.py",), outcome="passed")).committed
    assert H.read_history(domain, checkout).selection_quarantine == quarantine

    corrected_snapshot = _snapshot(case, compatibility="compat-v2")
    corrected_policy = "33" * 32
    baseline = replace(
        _result(
            case, 3, before=corrected_snapshot, after=corrected_snapshot,
            policy_digest=corrected_policy, checkout=checkout),
        run_id="3" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed")).baseline_published

    unrelated = _store_path(domain, checkout).parent / "unrelated-health.json"
    unrelated.write_text("sentinel")
    matched, corrected = _shadow_outcome(
        case, checkout, sequence=4, run_id="4" * 32,
        snapshot=corrected_snapshot, policy_digest=corrected_policy,
        full_outcome="passed", expected_quarantine=quarantine,
        baseline_run_id=baseline.run_id)
    assert H.publish_shadow_outcome(
        domain, checkout, matched, corrected).committed
    assert H.read_history(domain, checkout).selection_quarantine is None
    assert unrelated.read_text() == "sentinel"


@pytest.mark.parametrize(
    ("scenario", "baseline_compatibility", "baseline_policy"),
    (
        ("older-sequence", "compat-v2", "33" * 32),
        ("stale-expected-row", "compat-v2", "33" * 32),
        ("same-policy-and-compatibility", "compat-v1", "22" * 32),
        ("timeout", "compat-v2", "33" * 32),
    ),
)
def test_ineligible_shadow_cannot_clear_quarantine(
        case, scenario, baseline_compatibility, baseline_policy):
    domain = case.domain()
    checkout = case.checkout(domain)
    _, _, quarantine = _seed_quarantine(case, domain, checkout)
    snapshot = _snapshot(case, compatibility=baseline_compatibility)
    baseline = replace(_result(
        case, 3, before=snapshot, after=snapshot,
        policy_digest=baseline_policy, checkout=checkout), run_id="3" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    sequence = 1 if scenario == "older-sequence" else 4
    result, comparison = _shadow_outcome(
        case, checkout, sequence=sequence, run_id="4" * 32,
        snapshot=snapshot, policy_digest=baseline_policy,
        full_outcome="passed", expected_quarantine=quarantine,
        baseline_run_id=baseline.run_id,
    )
    if scenario == "stale-expected-row":
        comparison = replace(
            comparison,
            expected_quarantine=replace(quarantine, run_id="f" * 32),
        )
    elif scenario == "timeout":
        full_result = replace(
            comparison.full.result, status=C.Status.INCOMPLETE,
            raw_exit_code=-15, final_exit_code=70,
            inventory_complete=False,
        )
        full = replace(
            comparison.full, result=full_result, inventory=None,
            terminal_complete=False,
        )
        result = replace(
            result, status=C.Status.INCOMPLETE,
            runner_exit_code=-15, exit_code=70,
            attempts=(comparison.selected.result, full_result),
            reasons=(C.Reason(
                code="execution-timeout", message="full timed out"),),
        )
        comparison = replace(
            comparison, full=full, verdict="incomplete")

    assert H.publish_shadow_outcome(
        domain, checkout, result, comparison).committed
    assert H.read_history(domain, checkout).selection_quarantine == quarantine


@pytest.mark.parametrize("correction", ["policy", "compatibility"])
def test_new_baseline_with_one_real_correction_can_clear_exact_quarantine(
        case, correction):
    domain = case.domain()
    checkout = case.checkout(domain)
    _, old_policy, quarantine = _seed_quarantine(case, domain, checkout)
    compatibility = "compat-v2" if correction == "compatibility" else "compat-v1"
    policy = "33" * 32 if correction == "policy" else old_policy
    snapshot = _snapshot(case, compatibility=compatibility)
    baseline = replace(_result(
        case, 3, before=snapshot, after=snapshot,
        policy_digest=policy, checkout=checkout), run_id="3" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    result, comparison = _shadow_outcome(
        case, checkout, sequence=4, run_id="4" * 32,
        snapshot=snapshot, policy_digest=policy,
        full_outcome="passed", expected_quarantine=quarantine,
        baseline_run_id=baseline.run_id,
    )

    assert H.publish_shadow_outcome(
        domain, checkout, result, comparison).committed
    assert H.read_history(domain, checkout).selection_quarantine is None


def test_other_checkout_comparison_cannot_clear_quarantine(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _, _, quarantine = _seed_quarantine(case, domain, checkout)
    other_root = F.ensure_private_dir(domain.root, "other-checkout")
    other = C.CheckoutIdentity(
        project_id=checkout.project_id,
        checkout_id=hashlib.sha256(
            os.fsencode(os.path.realpath(other_root))).hexdigest()[:32],
        root=other_root,
    )
    snapshot = _snapshot(case, compatibility="compat-v2")
    baseline = replace(_result(
        case, 3, before=snapshot, after=snapshot,
        policy_digest="33" * 32, checkout=other), run_id="3" * 32)
    assert H.publish_outcome(
        domain, other, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    result, comparison = _shadow_outcome(
        case, other, sequence=4, run_id="4" * 32,
        snapshot=snapshot, policy_digest="33" * 32,
        full_outcome="passed", expected_quarantine=quarantine,
        baseline_run_id=baseline.run_id,
    )

    assert H.publish_shadow_outcome(
        domain, other, result, comparison).committed
    assert H.read_history(domain, checkout).selection_quarantine == quarantine
    assert H.read_history(domain, other).selection_quarantine is None


def test_health_markers_and_quarantine_bytes_are_preserved_together(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _, _, quarantine = _seed_quarantine(case, domain, checkout)
    directory = _store_path(domain, checkout).parent
    payloads = {
        "history-disabled.json": b'{"version":1,"code":"coordinator-corrupt"}',
        "history-capacity.json": (
            b'{"version":1,"code":"capacity-exceeded","sequence":9}'),
        "history-publication.json": (
            b'{"version":1,"code":"selection-disabled","sequence":9}'),
    }
    for name, payload in payloads.items():
        F.create_exclusive(directory, name, payload)
    before = {name: (directory / name).read_bytes() for name in payloads}
    snapshot = _snapshot(case, compatibility="compat-v2")
    result, comparison = _shadow_outcome(
        case, checkout, sequence=4, run_id="4" * 32,
        snapshot=snapshot, policy_digest="33" * 32,
        full_outcome="passed", expected_quarantine=quarantine,
        baseline_run_id="3" * 32,
    )

    published = H.publish_shadow_outcome(
        domain, checkout, result, comparison)

    assert published.committed is False
    assert published.reasons[0].code == "coordinator-corrupt"
    assert {name: (directory / name).read_bytes()
            for name in payloads} == before
    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert connection.execute(
            "SELECT run_id FROM selection_quarantine WHERE singleton = 1"
        ).fetchone() == (quarantine.run_id,)


def test_probe_persists_ordered_attempt_evidence_without_fabricated_inventory(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    attempts = tuple(
        C.AttemptResult(
            attempt_id=f"a{index:03d}", phase="execution",
            status=C.Status.PASSED, raw_exit_code=0, final_exit_code=0,
            source_valid=True, inventory_complete=True)
        for index in (1, 2))
    plan = C.Plan(
        mode=C.Mode.PROBE, execution="scoped",
        input_digest=snapshot.digest,
        compatibility=snapshot.compatibility)
    result = replace(_result(
        case, 1, run_id="5" * 32, mode=C.Mode.PROBE,
        before=snapshot, after=snapshot, checkout=checkout,
        attempts=attempts), plan=plan)
    evidence = tuple(
        C.AttemptEvidence(
            attempt_id=attempt.attempt_id, result=attempt,
            inventory=case.inventory((f"tests/test_{index}.py",),
                                     outcome="passed"),
            terminal_complete=True, parallel_identity=True,
            runtime_identity="66" * 32)
        for index, attempt in enumerate(attempts, 1))

    published = H.publish_probe_outcome(
        domain, checkout, result, evidence)
    assert published.committed is True
    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert connection.execute(
            "SELECT inventory FROM runs WHERE run_id = ?",
            (result.run_id,)).fetchone() == (None,)
        assert connection.execute(
            "SELECT attempt_id FROM attempt_evidence WHERE run_id = ? "
            "ORDER BY attempt_id", (result.run_id,)).fetchall() == [
                ("a001",), ("a002",)]
    assert H.read_history(domain, checkout).selection_quarantine is None


def test_pruning_compound_run_removes_attempts_and_receipt_atomically(
        case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 1)
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    first, first_comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="8" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        full_outcome="passed",
    )
    second, second_comparison = _shadow_outcome(
        case, checkout, sequence=2, run_id="9" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        full_outcome="passed",
    )
    assert H.publish_shadow_outcome(
        domain, checkout, first, first_comparison).committed
    assert H.publish_shadow_outcome(
        domain, checkout, second, second_comparison).committed

    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert connection.execute(
            "SELECT run_id FROM runs ORDER BY sequence"
        ).fetchall() == [(second.run_id,)]
        assert connection.execute(
            "SELECT DISTINCT run_id FROM attempt_evidence"
        ).fetchall() == [(second.run_id,)]
        assert connection.execute(
            "SELECT run_id FROM comparison_receipts"
        ).fetchall() == [(second.run_id,)]


def test_compound_evidence_counts_toward_logical_quota(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    result, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="8" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        full_outcome="passed",
    )
    records = tuple(
        ("tests/test_a.py", f"test_{index}_{'x' * 1000}", C.Outcome.PASSED)
        for index in range(200)
    )
    selected = _evidence("a001", records)
    full = _evidence(
        "a002", (*records,
                 ("tests/test_b.py", "test_b", C.Outcome.PASSED)),
    )
    result = replace(result, attempts=(selected.result, full.result))
    comparison = replace(comparison, selected=selected, full=full)
    assert H.publish_shadow_outcome(
        domain, checkout, result, comparison).committed

    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        run_rows = connection.execute(
            "SELECT run_id, sequence, summary, input_before, input_after, "
            "policy_digest, source_digest, compatibility, mode, status, inventory "
            "FROM runs"
        ).fetchall()
        run_bytes = sum(
            sum(len(str(value).encode()) for value in row[1:] if value is not None)
            for row in run_rows
        )
        compound_rows = connection.execute(
            "SELECT run_id, attempt_id, result, inventory, terminal_complete, "
            "parallel_identity, runtime_identity FROM attempt_evidence"
        ).fetchall()
        compound_rows += connection.execute(
            "SELECT run_id, verdict, expected_quarantine FROM comparison_receipts"
        ).fetchall()
        compound_bytes = sum(
            sum(len(str(value).encode()) for value in row if value is not None)
            for row in compound_rows
        )
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        reserve = page_size * 4
        limit = reserve + run_bytes + 1
        assert limit < reserve + run_bytes + compound_bytes
        monkeypatch.setattr(H, "HISTORY_MAX_BYTES", limit)

        class LogicalQuotaConnection:
            def execute(self, statement, parameters=()):
                if statement == "PRAGMA page_count":
                    return type("PageCount", (), {"fetchone": lambda self: (0,)})()
                return connection.execute(statement, parameters)

        connection.execute("BEGIN IMMEDIATE")
        removed = H._prune(
            LogicalQuotaConnection(), now=H._candidate_time(result))
        connection.commit()
        assert removed is True
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempt_evidence").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM comparison_receipts").fetchone() == (0,)


@pytest.mark.parametrize("orphan", ["attempt", "receipt"])
def test_orphaned_compound_evidence_disables_history_without_clearing_quarantine(
        case, orphan):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = replace(_result(
        case, 0, before=snapshot, after=snapshot,
        policy_digest="22" * 32, checkout=checkout), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    result, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="8" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        baseline_run_id=baseline.run_id,
    )
    assert H.publish_shadow_outcome(
        domain, checkout, result, comparison).committed
    quarantine = H.read_history(domain, checkout).selection_quarantine
    assert quarantine is not None

    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        if orphan == "attempt":
            connection.execute(
                "INSERT INTO attempt_evidence "
                "SELECT ?, attempt_id, result, inventory, terminal_complete, "
                "parallel_identity, runtime_identity FROM attempt_evidence "
                "WHERE run_id = ? AND attempt_id = 'a001'",
                ("f" * 32, result.run_id),
            )
        else:
            connection.execute(
                "INSERT INTO comparison_receipts "
                "SELECT ?, verdict, expected_quarantine "
                "FROM comparison_receipts WHERE run_id = ?",
                ("f" * 32, result.run_id),
            )
        connection.commit()

    view = H.read_history(domain, checkout)
    assert view.baseline is None
    assert view.selection_quarantine is None
    assert view.limitations[0].code == "coordinator-corrupt"
    marker = _store_path(domain, checkout).parent / "history-disabled.json"
    assert json.loads(marker.read_bytes())["code"] == "coordinator-corrupt"
    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert connection.execute(
            "SELECT run_id FROM selection_quarantine WHERE singleton = 1"
        ).fetchone() == (quarantine.run_id,)


@pytest.mark.parametrize("failure", ["interrupted", "disk-full"])
def test_failed_compound_commit_never_acknowledges_partial_publication(
        case, monkeypatch, failure):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = replace(_result(
        case, 0, before=snapshot, after=snapshot,
        policy_digest="22" * 32, checkout=checkout), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed"),
    ).baseline_published
    result, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="8" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        baseline_run_id=baseline.run_id,
    )

    def fail_store(*_args):
        if failure == "disk-full":
            raise _full_error()
        raise TypeError("interrupted compound publication")

    monkeypatch.setattr(H, "_store_attempt_evidence", fail_store)
    if failure == "interrupted":
        with pytest.raises(TypeError, match="interrupted compound publication"):
            H.publish_shadow_outcome(domain, checkout, result, comparison)
    else:
        published = H.publish_shadow_outcome(
            domain, checkout, result, comparison)
        assert published.committed is False
        assert published.reasons[0].code == "capacity-exceeded"

    with sqlite3.connect(_store_path(domain, checkout)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempt_evidence WHERE run_id = ?",
            (result.run_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM comparison_receipts WHERE run_id = ?",
            (result.run_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM selection_quarantine"
        ).fetchone() == (0,)
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert view.limitations[0].code == (
        "selection-disabled" if failure == "interrupted"
        else "capacity-exceeded")


def test_shadow_failure_from_first_attempt_survives_later_full_pass(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = replace(_result(
        case, 0, before=snapshot, after=snapshot,
        policy_digest="22" * 32, checkout=checkout), run_id="0" * 32)
    assert H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py", "tests/test_b.py"),
                       outcome="passed")).baseline_published
    result, comparison = _shadow_outcome(
        case, checkout, sequence=1, run_id="6" * 32,
        snapshot=snapshot, policy_digest="22" * 32,
        full_outcome="passed", baseline_run_id="0" * 32)
    failed_result = replace(
        comparison.selected.result, status=C.Status.FAILED,
        raw_exit_code=17, final_exit_code=17)
    failed_selected = replace(
        comparison.selected, result=failed_result,
        inventory=case.inventory(("tests/test_a.py",), outcome="failed"))
    result = replace(
        result, status=C.Status.FAILED, runner_exit_code=17, exit_code=17,
        attempts=(failed_result, comparison.full.result))
    comparison = replace(
        comparison, selected=failed_selected,
        verdict="unclassified-divergence")

    assert H.publish_shadow_outcome(
        domain, checkout, result, comparison).committed
    obligations = H.read_history(domain, checkout).obligations
    assert any(item.test_id == "tests/test_a.py::test_x"
               and item.sequence == 1 for item in obligations)


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


def test_committed_delta_full_pass_publishes_new_head_baseline(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    old = _snapshot(case)
    first = H.publish_outcome(
        domain, checkout, _result(case, 1, before=old, checkout=checkout), inventory,
    )
    assert first.baseline_published is True
    failed = _result(
        case, 2, status="failed", before=old, checkout=checkout,
        runner_exit_code=1, exit_code=1,
    )
    assert H.publish_outcome(domain, checkout, failed, inventory).committed is True
    assert any(item.file is None for item in H.read_history(domain, checkout).obligations)
    current = replace(
        old, head="b" * 40, digest="88" * 32, baseline_head=old.head,
        changes=(C.Change(old="src/app.py", new="src/app.py", kind="modified"),),
    )
    result = _result(case, 3, before=current, checkout=checkout)

    published = H.publish_outcome(domain, checkout, result, inventory)

    assert published.committed is True
    assert published.baseline_published is True
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is False
    assert view.baseline.run_id == result.run_id
    assert view.baseline.head == "b" * 40
    assert view.baseline.input_digest == "88" * 32
    assert view.baseline.inventory == inventory
    assert view.obligations == ()


@pytest.mark.parametrize("invalid", [
    "dirty-before", "dirty-after", "limited-before", "limited-after",
    "changed-digest", "changed-head", "changed-compatibility", "untrusted-source",
    "incomplete-inventory", "unknown-outcome",
])
def test_unverified_committed_delta_cannot_replace_baseline(case, invalid):
    domain = case.domain()
    checkout = case.checkout(domain)
    old = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    original = _result(case, 1, before=old, checkout=checkout)
    assert H.publish_outcome(domain, checkout, original, inventory).baseline_published is True
    current = replace(
        old, head="b" * 40, digest="88" * 32, baseline_head=old.head,
        changes=(C.Change(old="src/app.py", new="src/app.py", kind="modified"),),
    )
    before = after = current
    if invalid == "dirty-before":
        before = replace(current, clean=False)
    elif invalid == "dirty-after":
        after = replace(current, clean=False)
    elif invalid == "limited-before":
        before = replace(current, limitations=(C.Reason(code="unknown-input", message="unknown"),))
    elif invalid == "limited-after":
        after = replace(current, limitations=(C.Reason(code="unknown-input", message="unknown"),))
    elif invalid == "changed-digest":
        after = replace(current, digest="99" * 32)
    elif invalid == "changed-head":
        after = replace(current, head="c" * 40)
    elif invalid == "changed-compatibility":
        after = replace(current, compatibility="other-compatibility")
    elif invalid == "incomplete-inventory":
        inventory = replace(inventory, complete=False)
    elif invalid == "unknown-outcome":
        inventory = case.inventory(("tests/test_a.py",), outcome="unknown")
    candidate = _result(
        case, 2, before=before, after=after, checkout=checkout,
        source_valid=invalid != "untrusted-source",
    )

    published = H.publish_outcome(domain, checkout, candidate, inventory)

    assert published.committed is True
    assert published.baseline_published is False
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is False
    assert view.baseline.run_id == original.run_id
    assert view.baseline.head == old.head


@pytest.mark.parametrize("baseline_head", [None, "c" * 40])
def test_private_snapshot_codec_preserves_baseline_head(case, baseline_head):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = replace(_snapshot(case), baseline_head=baseline_head)
    result = _result(case, 1, before=snapshot, checkout=checkout)
    published = H.publish_outcome(
        domain, checkout, result, case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.baseline_published is True
    connection = sqlite3.connect(_store_path(domain, checkout))
    try:
        stored = connection.execute(
            "SELECT input_before, input_after FROM runs WHERE run_id = ?", (result.run_id,),
        ).fetchone()
    finally:
        connection.close()
    for text in stored:
        private = json.loads(text)
        assert "baseline_head" in private
        assert private["baseline_head"] == baseline_head
        assert H._snapshot_from_dict(private) == snapshot
    assert H.read_history(domain, checkout).selection_disabled is False
    summary = H.read_history_summaries(domain, checkout)[0]
    assert summary == C.serialize_run_result(result)
    assert "baseline_head" not in json.dumps(summary)
    if baseline_head is not None:
        assert baseline_head not in json.dumps(summary)


@pytest.mark.parametrize("column", ["input_before", "input_after"])
@pytest.mark.parametrize("corruption", ["missing", "unknown-field", "integer", "boolean", "empty"])
def test_invalid_private_baseline_head_disables_selection(case, column, corruption):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = replace(_snapshot(case), baseline_head="c" * 40)
    result = _result(case, 1, before=snapshot, checkout=checkout)
    assert H.publish_outcome(
        domain, checkout, result, case.inventory(("tests/test_a.py",), outcome="passed"),
    ).baseline_published is True
    path = _store_path(domain, checkout)
    connection = sqlite3.connect(path)
    try:
        # Only the two literal parameterized column names reach SQL here.
        private = json.loads(connection.execute(
            f"SELECT {column} FROM runs WHERE run_id = ?", (result.run_id,),
        ).fetchone()[0])
        if corruption == "missing":
            private.pop("baseline_head", None)
        elif corruption == "unknown-field":
            private["unexpected_provenance"] = "c" * 40
        else:
            private["baseline_head"] = {"integer": 7, "boolean": True, "empty": ""}[corruption]
        damaged = json.dumps(private)
        connection.execute(
            f"UPDATE runs SET {column} = ? WHERE run_id = ?", (damaged, result.run_id),
        )
        connection.commit()
    finally:
        connection.close()

    view = H.read_history(domain, checkout)

    assert view.selection_disabled is True
    assert view.baseline is None
    assert any(reason.code == "coordinator-corrupt" for reason in view.limitations)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            f"SELECT {column} FROM runs WHERE run_id = ?", (result.run_id,),
        ).fetchone()[0] == damaged
    finally:
        connection.close()


def test_deleted_id_reconciles_only_after_a_complete_clean_full_inventory(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    snapshot = _snapshot(case)
    full = _result(case, 2, before=snapshot, after=snapshot, checkout=checkout)
    H.publish_outcome(
        domain, checkout, full,
        case.inventory(("tests/test_deleted.py",), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations == ()


def test_empty_complete_full_inventory_preserves_obligations(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    snapshot = _snapshot(case)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=snapshot, after=snapshot, checkout=checkout),
        case.inventory((), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations


def test_clean_full_pass_recovers_compatibility_none_obligations(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    failed = _result(
        case, 1, status="failed", before=None, after=None,
        runner_exit_code=1, exit_code=1, checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, failed,
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    snapshot = _snapshot(case)
    recovered = H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=snapshot, after=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert recovered.baseline_published is True
    assert H.read_history(domain, checkout).obligations == ()


def test_clean_full_pass_recovers_compatibility_none_whole_gate(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    failed = _result(
        case, 1, status="failed", before=None, after=None,
        runner_exit_code=1, exit_code=1, checkout=checkout,
    )
    H.publish_outcome(domain, checkout, failed, None)
    snapshot = _snapshot(case)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=snapshot, after=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations == ()


@pytest.mark.parametrize(
    ("before_digest", "after_digest", "before_compat", "after_compat"),
    (
        ("aa" * 32, "bb" * 32, "compat-v1", "compat-v1"),
        ("aa" * 32, "aa" * 32, "compat-v1", "compat-v2"),
    ),
)
def test_mixed_input_identity_records_unknown_and_clean_full_recovers_it(
    case, before_digest, after_digest, before_compat, after_compat,
):
    domain = case.domain()
    checkout = case.checkout(domain)
    before = _snapshot(case, digest=before_digest, compatibility=before_compat)
    after = _snapshot(case, digest=after_digest, compatibility=after_compat)
    failed = _result(
        case, 1, status="failed", before=before, after=after,
        runner_exit_code=1, exit_code=1, checkout=checkout,
    )
    H.publish_outcome(
        domain, checkout, failed,
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    obligation = H.read_history(domain, checkout).obligations[0]
    assert obligation.source_digest is None
    assert obligation.compatibility is None
    clean = _snapshot(case, digest="cc" * 32, compatibility="compat-v1")
    recovered = H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=clean, after=clean, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert recovered.baseline_published is True
    assert H.read_history(domain, checkout).obligations == ()


@pytest.mark.parametrize(
    ("mode", "clean"),
    ((C.Mode.SCOPED, True), (C.Mode.FULL, False)),
)
def test_unknown_identity_obligation_survives_scoped_or_dirty_pass(case, mode, clean):
    domain = case.domain()
    checkout = case.checkout(domain)
    before = _snapshot(case, digest="aa" * 32, compatibility="compat-v1")
    after = _snapshot(case, digest="bb" * 32, compatibility="compat-v2")
    H.publish_outcome(
        domain, checkout,
        _result(case, 1, status="failed", before=before, after=after,
                runner_exit_code=1, exit_code=1, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    snapshot = _snapshot(case, compatibility="compat-v1", clean=clean)
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, mode=mode, before=snapshot, after=snapshot,
                checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert H.read_history(domain, checkout).obligations


def test_newer_clean_full_baseline_clears_older_whole_gate_across_compatibility(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    old = _snapshot(case, compatibility="compat-v1")
    H.publish_outcome(
        domain, checkout,
        _result(case, 1, status="failed", before=old, after=old,
                runner_exit_code=1, exit_code=1, checkout=checkout),
        None,
    )
    new = _snapshot(case, compatibility="compat-v2")
    H.publish_outcome(
        domain, checkout,
        _result(case, 2, before=new, after=new, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    view = H.read_history(domain, checkout)
    assert view.baseline is not None
    assert view.obligations == ()
    path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize("sequence,mode", [(8, C.Mode.FULL), (9, C.Mode.FULL), (10, C.Mode.SCOPED)])
def test_older_or_scoped_pass_cannot_clear_whole_gate_across_compatibility(case, sequence, mode):
    domain = case.domain()
    checkout = case.checkout(domain)
    old = _snapshot(case, compatibility="compat-v1")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 9, status="failed", before=old, checkout=checkout),
                             None).committed
    new = _snapshot(case, compatibility="compat-v2")
    assert H.publish_outcome(domain, checkout,
                             _result(case, sequence, mode=mode, before=new, checkout=checkout),
                             case.inventory(("tests/test_a.py",), outcome="passed")).committed
    obligations = H.read_history(domain, checkout).obligations
    assert len(obligations) == 1
    assert obligations[0].reason == "full-gate-obligation"
    assert obligations[0].sequence == 9


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


def test_read_history_with_existing_checkout_directory_does_not_create_database(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    checkouts = F.ensure_private_dir(domain.root, "checkouts")
    history_dir = F.ensure_private_dir(checkouts, checkout.checkout_id)
    assert H.read_history(domain, checkout).baseline is None
    assert not (history_dir / "history.sqlite3").exists()
    assert not (history_dir / "history-disabled.json").exists()
    assert H.read_history_summaries(domain, checkout) == ()
    assert H.read_history_payload(domain, checkout) == {
        "summaries": [], "obligations": [],
    }
    assert not (history_dir / "history.sqlite3").exists()


def test_interrupted_empty_store_is_absent_to_all_reads_and_reinitializable(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    original_open = H.open_database

    def deny_index(*args, **kwargs):
        connection = original_open(*args, **kwargs)
        connection.set_authorizer(
            lambda action, *_rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_INDEX
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(H, "open_database", deny_index)
    with pytest.raises(sqlite3.Error):
        H._open_store(domain, checkout, create=True)
    monkeypatch.setattr(H, "open_database", original_open)
    history_dir = domain.root / "checkouts" / checkout.checkout_id
    history_path = history_dir / "history.sqlite3"
    assert history_path.exists()
    assert H.read_history(domain, checkout).baseline is None
    assert H.read_history(domain, checkout).selection_disabled is False
    assert H.read_history_summaries(domain, checkout) == ()
    assert H.read_history_payload(domain, checkout) == {
        "summaries": [], "obligations": [],
    }
    assert not (history_dir / "history-disabled.json").exists()
    published = H.publish_outcome(
        domain, checkout,
        _result(case, 1, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.committed is True


@pytest.mark.parametrize(
    "code,message",
    ((sqlite3.SQLITE_BUSY, "database is locked"),
     (776, "attempt to write a readonly database"),
     (sqlite3.SQLITE_BUSY_SNAPSHOT, "database is locked")),
)
def test_transient_read_errors_are_per_call_and_never_mark_history(case, monkeypatch, code, message):
    domain = case.domain()
    checkout = case.checkout(domain)
    H.publish_outcome(
        domain, checkout, _result(case, 1, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    original_validate = H._validate_schema

    def fail_read(*args, **kwargs):
        failure = sqlite3.OperationalError(message)
        failure.sqlite_errorcode = code
        raise failure

    monkeypatch.setattr(H, "_validate_schema", fail_read)
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert view.limitations[0].code == "coordinator-unavailable"
    with pytest.raises(C.Problem, match="coordinator-unavailable"):
        H.read_history_summaries(domain, checkout)
    with pytest.raises(C.Problem, match="coordinator-unavailable"):
        H.read_history_payload(domain, checkout)
    assert not (domain.root / "checkouts" / checkout.checkout_id / "history-disabled.json").exists()
    monkeypatch.setattr(H, "_validate_schema", original_validate)
    assert H.read_history(domain, checkout).selection_disabled is False


def test_opener_corrupt_problem_is_unavailable_without_positive_corruption(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    original_open = H.open_database

    def unavailable(*_args, **_kwargs):
        raise C.Problem(code="coordinator-corrupt", message="opaque opener failure",
                        phase="storage", retryable=False)

    monkeypatch.setattr(H, "open_database", unavailable)
    result = _result(case, 1, checkout=checkout)
    inventory = case.inventory(("tests/test_a.py",), outcome="failed")
    published = H.publish_outcome(domain, checkout, result, inventory)
    assert not published.committed
    assert published.selection_disabled
    assert published.reasons[0].code == "coordinator-unavailable"
    assert not (_store_path(domain, checkout).parent / "history-disabled.json").exists()
    monkeypatch.setattr(H, "open_database", original_open)
    assert H.publish_outcome(domain, checkout, result, inventory).committed


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
    normal_home = _normal_home(case, monkeypatch)
    before = _tree_snapshot(normal_home)
    monkeypatch.setattr(H, "HISTORY_MAX_BYTES", 1)
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is True
    assert any(reason.code == "capacity-exceeded" for reason in view.limitations)
    assert _tree_snapshot(normal_home) == before


def test_normal_domain_tree_and_checkout_metadata_survive_history_operations(case, monkeypatch):
    normal_home = _normal_home(case, monkeypatch)
    assert Path.home() == normal_home
    assert Path(pwd.getpwuid(os.getuid()).pw_dir) == normal_home
    normal_before = _tree_snapshot(normal_home)
    domain = case.domain()
    checkout = case.checkout(domain)
    H.read_history(domain, checkout)
    H.read_history_summaries(domain, checkout)
    H.read_history_payload(domain, checkout)
    assert _tree_snapshot(normal_home) == normal_before

    H.publish_outcome(
        domain, checkout, _result(case, 1, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    history_dir = domain.root / "checkouts" / checkout.checkout_id
    checkout_before = _tree_snapshot(history_dir)
    H.read_history(domain, checkout)
    H.read_history_summaries(domain, checkout)
    H.read_history_payload(domain, checkout)
    assert _tree_snapshot(history_dir) == checkout_before

    monkeypatch.setattr(H, "HISTORY_MAX_BYTES", 1)
    view = H.read_history(domain, checkout)
    assert view.limitations[0].code == "capacity-exceeded"
    with pytest.raises(C.Problem, match="capacity-exceeded"):
        H.read_history_summaries(domain, checkout)
    with pytest.raises(C.Problem, match="capacity-exceeded"):
        H.read_history_payload(domain, checkout)
    assert _tree_snapshot(normal_home) == normal_before
    assert _tree_snapshot(history_dir) == checkout_before

    quota_publish = H.publish_outcome(
        domain, checkout, _result(case, 2, checkout=checkout), None,
    )
    assert not quota_publish.committed
    assert quota_publish.reasons[0].code == "capacity-exceeded"
    assert H.read_history(domain, checkout).selection_disabled
    for read in (H.read_history_summaries, H.read_history_payload):
        with pytest.raises(C.Problem, match="capacity-exceeded"):
            read(domain, checkout)
    assert _tree_snapshot(normal_home) == normal_before

    monkeypatch.setattr(H, "HISTORY_MAX_BYTES", C.HISTORY_MAX_BYTES)
    path = history_dir / "history.sqlite3"
    path.write_bytes(b"not-a-sqlite-database")
    assert H.read_history(domain, checkout).limitations[0].code == "coordinator-corrupt"
    for read in (H.read_history_summaries, H.read_history_payload):
        with pytest.raises(C.Problem, match="coordinator-corrupt"):
            read(domain, checkout)
    corrupt_publish = H.publish_outcome(
        domain, checkout, _result(case, 3, checkout=checkout), None,
    )
    assert not corrupt_publish.committed
    assert corrupt_publish.reasons[0].code == "coordinator-corrupt"
    assert path.read_bytes() == b"not-a-sqlite-database"
    assert _tree_snapshot(normal_home) == normal_before


def test_history_imports_no_platform_or_domain_resolver():
    tree = ast.parse(Path(H.__file__).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
            imports.extend(alias.name for alias in node.names)
    assert not {"platform", "config", "pwd", "domain", "domain_paths"}.intersection(
        part for name in imports for part in name.split(".")
    )


@pytest.mark.parametrize("roomy", [False, True])
def test_byte_pruning_preserves_baseline_and_failure_evidence(case, monkeypatch, roomy):
    monkeypatch.setattr(H, "HISTORY_MAX_BYTES", 128 * 1024)
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 200)
    original_open = H.open_database

    def roomy_open(directory, name, *, max_bytes):
        return original_open(directory, name, max_bytes=8 * 1024 * 1024)

    if roomy:
        monkeypatch.setattr(H, "open_database", roomy_open)
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = _result(case, 1, before=snapshot, checkout=checkout)
    assert H.publish_outcome(
        domain, checkout, baseline, case.inventory(("tests/test_base.py",), outcome="passed"),
    ).baseline_published
    _publish_failure(case, domain, checkout, sequence=2)
    assert H.publish_outcome(
        domain, checkout, _result(case, 3, status="failed", checkout=checkout), None,
    ).committed
    obligations = H.read_history(domain, checkout).obligations
    assert {item.reason for item in obligations} == {"prior-failure", "full-gate-obligation"}
    ordinary_ids = []
    total_incoming = 0
    for sequence in range(4, 28):
        result = _result(
            case, sequence, checkout=checkout, mode=C.Mode.SCOPED,
            limitations=(C.Reason(code="prior-failure", message="x" * 12000),),
        )
        inventory = case.inventory((f"tests/test_{sequence}.py",), outcome="passed")
        total_incoming += len(json.dumps(C.serialize_run_result(result)))
        ordinary_ids.append(result.run_id)
        published = H.publish_outcome(domain, checkout, result, inventory)
        assert published.committed is True
        assert published.selection_disabled is False
    assert total_incoming > 2 * H.HISTORY_MAX_BYTES
    retained = {item["run_id"] for item in H.read_history_summaries(domain, checkout)}
    assert baseline.run_id in retained
    assert not set(ordinary_ids[:5]) & retained
    assert ordinary_ids[-1] in retained
    assert len(set(ordinary_ids) - retained) >= 5
    assert _store_path(domain, checkout).stat().st_size <= H.HISTORY_MAX_BYTES
    view = H.read_history(domain, checkout)
    assert not view.selection_disabled
    assert view.baseline.run_id == baseline.run_id
    assert view.obligations == obligations


def test_sqlite_full_retries_after_committed_pruning_without_marker(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout, sequence=1)
    original_insert = H._insert_summary
    calls = 0

    def full_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(H, "_insert_summary", full_once)
    published = H.publish_outcome(
        domain, checkout, _result(case, 2, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert published.committed is True
    assert published.selection_disabled is False
    assert calls == 2
    assert not (domain.root / "checkouts" / checkout.checkout_id / "history-disabled.json").exists()


def test_protected_data_only_capacity_keeps_clearable_uncertainty(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = _result(case, 1, before=snapshot, after=snapshot, checkout=checkout)
    H.publish_outcome(
        domain, checkout, baseline,
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    # Only the baseline summary exists before the attempted failed inventory.
    assert len(H.read_history_summaries(domain, checkout)) == 1
    original_insert = H._insert_summary

    def always_full(*args, **kwargs):
        raise _full_error()

    monkeypatch.setattr(H, "_insert_summary", always_full)
    published = H.publish_outcome(
        domain, checkout, _result(case, 3, status="failed", checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    assert published.committed is False
    assert published.selection_disabled is True
    assert published.reasons[0].code == "capacity-exceeded"
    assert not (domain.root / "checkouts" / checkout.checkout_id / "history-disabled.json").exists()
    view = H.read_history(domain, checkout)
    assert view.baseline is not None
    assert view.baseline.run_id == baseline.run_id
    assert view.selection_disabled
    assert view.limitations[0].code == "capacity-exceeded"
    marker = _store_path(domain, checkout).parent / "history-capacity.json"
    assert json.loads(marker.read_bytes()) == {
        "version": 1, "code": "capacity-exceeded", "sequence": 3,
    }
    monkeypatch.setattr(H, "_insert_summary", original_insert)
    # A scoped success or late older full cannot account for a lost failure.
    for sequence, mode, outcome in ((4, C.Mode.SCOPED, "passed"), (2, C.Mode.FULL, "passed"),
                                     (5, C.Mode.FULL, "skipped")):
        result = _result(case, sequence, before=snapshot, checkout=checkout, mode=mode)
        assert H.publish_outcome(domain, checkout, result,
                                 case.inventory(("tests/test_a.py",), outcome=outcome)).committed
        assert H.read_history(domain, checkout).selection_disabled
        assert marker.exists()
    recovered = H.publish_outcome(
        domain, checkout, _result(case, 6, before=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="passed"),
    )
    assert recovered.committed and recovered.baseline_published
    assert not recovered.selection_disabled
    assert not H.read_history(domain, checkout).selection_disabled
    assert not marker.exists()


def test_sqlite_full_persists_until_oldest_ordinary_run_is_pruned(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    baseline = _result(case, 1, before=snapshot, checkout=checkout)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published
    ordinary = _result(case, 2, checkout=checkout)
    assert H.publish_outcome(domain, checkout, ordinary, inventory).committed
    original_insert = H._insert_summary
    calls = []

    def full_until_pruned(connection, *args):
        present = connection.execute("SELECT run_id FROM runs ORDER BY sequence").fetchall()
        calls.append(present)
        if (ordinary.run_id,) in present:
            raise _full_error()
        return original_insert(connection, *args)

    monkeypatch.setattr(H, "_insert_summary", full_until_pruned)
    published = H.publish_outcome(domain, checkout, _result(case, 3, checkout=checkout), inventory)
    assert published.committed and not published.selection_disabled
    assert len(calls) == 2
    assert (ordinary.run_id,) in calls[0] and (ordinary.run_id,) not in calls[1]
    assert H.read_history(domain, checkout).baseline.run_id == baseline.run_id


def test_real_sqlite_full_recovers_after_ordinary_eviction(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    baseline = _result(case, 1, before=_snapshot(case), checkout=checkout)
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published
    ordinary = _result(case, 2, checkout=checkout,
                       limitations=(C.Reason(code="prior-failure", message="x" * 80000),))
    assert H.publish_outcome(domain, checkout, ordinary, inventory).committed
    original_open = H.open_database
    original_insert = H._insert_summary
    full_codes = []

    def capped_open(*args, **kwargs):
        connection = original_open(*args, **kwargs)
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={pages}")
        return connection

    def insert(connection, *args):
        try:
            return original_insert(connection, *args)
        except sqlite3.Error as exc:
            full_codes.append(exc.sqlite_errorcode)
            raise

    monkeypatch.setattr(H, "open_database", capped_open)
    monkeypatch.setattr(H, "_insert_summary", insert)
    incoming = _result(case, 3, checkout=checkout,
                       limitations=(C.Reason(code="prior-failure", message="y" * 80000),))
    published = H.publish_outcome(domain, checkout, incoming, inventory)
    assert published.committed and not published.selection_disabled
    assert full_codes and set(full_codes) == {sqlite3.SQLITE_FULL}
    retained = {item["run_id"] for item in H.read_history_summaries(domain, checkout)}
    assert retained == {baseline.run_id, incoming.run_id}


def test_full_recovery_exhausts_ordinary_rows_then_reconciliations(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    _publish_failure(case, domain, checkout)
    inventory = case.inventory(("tests/test_other.py",), outcome="passed")
    baseline = _result(case, 2, before=_snapshot(case), checkout=checkout)
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published
    _publish_failure(case, domain, checkout, sequence=3, file="tests/test_keep.py")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 4, status="failed", checkout=checkout), None).committed
    protected = H.read_history(domain, checkout).obligations
    assert len(protected) == 2
    observations = []

    def full(connection, *_args):
        ordinary = connection.execute(
            "SELECT sequence FROM runs WHERE run_id != ? ORDER BY sequence", (baseline.run_id,),
        ).fetchall()
        reconciled = connection.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0]
        observations.append((ordinary, reconciled))
        raise _full_error()

    monkeypatch.setattr(H, "_insert_summary", full)
    published = H.publish_outcome(domain, checkout, _result(case, 5, checkout=checkout), inventory)
    assert not published.committed and published.reasons[0].code == "capacity-exceeded"
    assert observations[0] == ([(1,), (3,), (4,)], 1)
    assert observations[1] == ([(3,), (4,)], 1)
    assert ([], 1) in observations
    assert observations[-1] == ([], 0)
    assert len(observations) <= 6
    view = H.read_history(domain, checkout)
    assert view.baseline.run_id == baseline.run_id
    assert view.obligations == protected and view.selection_disabled


def test_capacity_marker_cannot_hide_invalid_store_during_recovery(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, _result(case, 1, checkout=checkout), inventory).committed
    original_insert = H._insert_summary
    monkeypatch.setattr(H, "_insert_summary", lambda *_args: (_ for _ in ()).throw(_full_error()))
    assert not H.publish_outcome(domain, checkout, _result(case, 2, checkout=checkout), inventory).committed
    monkeypatch.setattr(H, "_insert_summary", original_insert)
    connection = sqlite3.connect(_store_path(domain, checkout))
    connection.execute("UPDATE metadata SET value = 'broken' WHERE key = 'selection_disabled'")
    connection.commit()
    connection.close()
    recovered = H.publish_outcome(domain, checkout,
                                  _result(case, 3, before=_snapshot(case), checkout=checkout), inventory)
    assert not recovered.committed
    assert recovered.reasons[0].code == "coordinator-corrupt"
    assert (_store_path(domain, checkout).parent / "history-capacity.json").exists()


def test_full_has_reserved_uncertainty_even_if_later_marker_writes_fail(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 1, before=_snapshot(case), checkout=checkout), inventory).committed

    def marker_unavailable(*_args, **_kwargs):
        raise OSError("no space left on device")

    def full(*_args):
        monkeypatch.setattr(H, "publish_atomic", marker_unavailable)
        raise _full_error()

    monkeypatch.setattr(H, "_insert_summary", full)
    result = H.publish_outcome(domain, checkout,
                               _result(case, 2, status="failed", checkout=checkout),
                               case.inventory(("tests/test_a.py",), outcome="failed"))
    assert not result.committed and result.reasons[0].code == "capacity-exceeded"
    view = H.read_history(domain, checkout)
    assert view.selection_disabled and view.limitations[0].code == "capacity-exceeded"


def test_transient_publish_failure_leaves_no_permanent_capacity_or_corruption(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 1, before=_snapshot(case), checkout=checkout), inventory).committed

    def locked(*_args):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(H, "_insert_summary", locked)
    result = H.publish_outcome(domain, checkout, _result(case, 2, checkout=checkout), inventory)
    assert not result.committed and result.reasons[0].code == "coordinator-unavailable"
    assert not H.read_history(domain, checkout).selection_disabled
    directory = _store_path(domain, checkout).parent
    assert not (directory / "history-disabled.json").exists()
    assert not (directory / "history-capacity.json").exists()


def test_read_during_healthy_publish_stays_usable_without_touching_history(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    baseline = _result(case, 1, before=snapshot, checkout=checkout)
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published
    publishing = threading.Event()
    release = threading.Event()
    original_insert = H._insert_summary
    result = {}

    def blocked_insert(connection, incoming, incoming_inventory):
        if incoming.sequence == 2:
            publishing.set()
            assert release.wait(timeout=3)
        return original_insert(connection, incoming, incoming_inventory)

    def publish():
        result["value"] = H.publish_outcome(
            domain, checkout, _result(case, 2, checkout=checkout), inventory,
        )

    monkeypatch.setattr(H, "_insert_summary", blocked_insert)
    writer = threading.Thread(name="healthy-history-writer", target=publish)
    writer.start()
    assert publishing.wait(timeout=3)
    directory = _store_path(domain, checkout).parent
    before = _tree_snapshot(directory)
    try:
        view = H.read_history(domain, checkout)
        assert view.baseline is not None and view.baseline.run_id == baseline.run_id
        assert view.selection_disabled is False
        assert _tree_snapshot(directory) == before
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert result["value"].committed and not result["value"].selection_disabled


def test_read_during_healthy_publish_cleanup_stays_usable(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    baseline = _result(case, 1, before=snapshot, checkout=checkout)
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published
    cleanup_boundary = threading.Event()
    release = threading.Event()
    original_remove = H._remove_marker
    active_removals = 0
    result = {}

    def paused_remove(directory, name):
        nonlocal active_removals
        original_remove(directory, name)
        if (threading.current_thread().name == "healthy-cleanup-writer"
                and name == H._ACTIVE_PUBLICATION_NAME):
            active_removals += 1
            if active_removals == 2:
                cleanup_boundary.set()
                assert release.wait(timeout=3)

    def publish():
        result["value"] = H.publish_outcome(
            domain, checkout, _result(case, 2, checkout=checkout), inventory,
        )

    monkeypatch.setattr(H, "_remove_marker", paused_remove)
    writer = threading.Thread(name="healthy-cleanup-writer", target=publish)
    writer.start()
    assert cleanup_boundary.wait(timeout=3)
    try:
        view = H.read_history(domain, checkout)
        assert view.baseline is not None and view.baseline.run_id == baseline.run_id
        assert not view.selection_disabled
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert result["value"].committed and not result["value"].selection_disabled


@pytest.mark.parametrize("failure", [KeyboardInterrupt, TypeError])
def test_interrupted_publish_is_uncertain_without_false_capacity(case, monkeypatch, failure):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    baseline = _result(case, 1, before=snapshot, checkout=checkout)
    assert H.publish_outcome(domain, checkout, baseline, inventory).baseline_published

    def interrupted(*_args):
        raise failure("interrupted publication")

    monkeypatch.setattr(H, "_insert_summary", interrupted)
    with pytest.raises(failure, match="interrupted publication"):
        H.publish_outcome(
            domain, checkout, _result(case, 2, checkout=checkout), inventory,
        )
    view = H.read_history(domain, checkout)
    assert view.baseline is not None and view.baseline.run_id == baseline.run_id
    assert view.selection_disabled
    assert view.limitations[0].code == "selection-disabled"
    assert not (_store_path(domain, checkout).parent / "history-capacity.json").exists()


def test_interrupted_publication_allows_ineligible_commits_and_retains_failure(
    case, monkeypatch,
):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), passed,
    ).baseline_published
    original_insert = H._insert_summary

    def interrupted(*_args):
        raise TypeError("interrupted publication")

    monkeypatch.setattr(H, "_insert_summary", interrupted)
    with pytest.raises(TypeError, match="interrupted publication"):
        H.publish_outcome(
            domain, checkout, _result(case, 10, checkout=checkout), passed,
        )
    monkeypatch.setattr(H, "_insert_summary", original_insert)
    directory = _store_path(domain, checkout).parent
    marker = directory / "history-publication.json"
    assert json.loads(marker.read_bytes())["sequence"] == 10

    dirty = _snapshot(case, clean=False)
    candidates = (
        (_result(case, 11, before=snapshot, checkout=checkout, mode=C.Mode.SCOPED), passed),
        (_result(case, 12, before=dirty, checkout=checkout), passed),
        (_result(case, 13, before=snapshot, checkout=checkout),
         case.inventory(("tests/test_a.py",), outcome="skipped")),
        (_result(case, 14, before=snapshot, checkout=checkout),
         case.inventory(("tests/test_a.py",), outcome="xfail")),
        (_result(case, 15, status="failed", before=snapshot, checkout=checkout),
         case.inventory(("tests/test_new_failure.py",), outcome="failed")),
        (_result(case, 9, before=snapshot, checkout=checkout), passed),
    )
    committed_ids = []
    expected_sequence = 10
    for candidate, inventory in candidates:
        published = H.publish_outcome(domain, checkout, candidate, inventory)
        assert published.committed and published.selection_disabled
        assert published.reasons[0].code == "selection-disabled"
        expected_sequence = max(expected_sequence, candidate.sequence)
        assert json.loads(marker.read_bytes())["sequence"] == expected_sequence
        committed_ids.append(candidate.run_id)

    view = H.read_history(domain, checkout)
    assert view.selection_disabled
    assert view.limitations[0].code == "selection-disabled"
    assert any(
        item.file == "tests/test_new_failure.py" and item.sequence == 15
        for item in view.obligations
    )
    connection = sqlite3.connect(_store_path(domain, checkout))
    stored_ids = {row[0] for row in connection.execute("SELECT run_id FROM runs")}
    connection.close()
    assert set(committed_ids).issubset(stored_ids)
    assert not (directory / "history-capacity.json").exists()


def test_strictly_newer_clean_full_clears_publication_only_after_commit(
    case, monkeypatch,
):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), passed,
    ).baseline_published
    original_insert = H._insert_summary

    def interrupted(*_args):
        raise TypeError("interrupted publication")

    monkeypatch.setattr(H, "_insert_summary", interrupted)
    with pytest.raises(TypeError, match="interrupted publication"):
        H.publish_outcome(
            domain, checkout, _result(case, 10, checkout=checkout), passed,
        )
    monkeypatch.setattr(H, "_insert_summary", original_insert)
    failed = H.publish_outcome(
        domain, checkout,
        _result(case, 11, status="failed", before=snapshot, checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    assert failed.committed and failed.selection_disabled

    committed = threading.Event()
    release = threading.Event()
    original_remove = H._remove_marker
    result = {}
    errors = []
    recovery_result = _result(case, 12, before=snapshot, checkout=checkout)

    def remove_after_commit(directory, name):
        if (threading.current_thread().name == "publication-recovery"
                and name == H._PUBLICATION_MARKER_NAME):
            committed.set()
            assert release.wait(timeout=3)
        return original_remove(directory, name)

    def recover():
        try:
            result["value"] = H.publish_outcome(
                domain, checkout, recovery_result, passed,
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    monkeypatch.setattr(H, "_remove_marker", remove_after_commit)
    writer = threading.Thread(name="publication-recovery", target=recover)
    writer.start()
    assert committed.wait(timeout=3)
    try:
        connection = sqlite3.connect(_store_path(domain, checkout))
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE sequence = 12"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM obligations").fetchone() == (0,)
        connection.close()
        view = H.read_history(domain, checkout)
        assert view.baseline is not None and view.baseline.run_id == recovery_result.run_id
        assert view.obligations == ()
        assert view.selection_disabled
        assert view.limitations[0].code == "selection-disabled"
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive() and not errors
    assert result["value"].committed and result["value"].baseline_published
    assert not result["value"].selection_disabled
    assert not H.read_history(domain, checkout).selection_disabled
    assert not (_store_path(domain, checkout).parent / "history-publication.json").exists()


def test_publication_uncertainty_tracks_highest_loss_before_recovery(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), passed,
    ).baseline_published
    original_insert = H._insert_summary

    def interrupted(*_args):
        raise TypeError("interrupted publication")

    monkeypatch.setattr(H, "_insert_summary", interrupted)
    for sequence in (10, 30):
        with pytest.raises(TypeError, match="interrupted publication"):
            H.publish_outcome(
                domain, checkout,
                _result(case, sequence, before=snapshot, checkout=checkout),
                passed,
            )
    marker = _store_path(domain, checkout).parent / "history-publication.json"
    assert json.loads(marker.read_bytes())["sequence"] == 30

    monkeypatch.setattr(H, "_insert_summary", original_insert)
    late = H.publish_outcome(
        domain, checkout,
        _result(case, 20, before=snapshot, checkout=checkout), passed,
    )
    assert late.committed and late.baseline_published and late.selection_disabled
    assert late.reasons[0].code == "selection-disabled"
    assert json.loads(marker.read_bytes())["sequence"] == 30
    assert H.read_history(domain, checkout).selection_disabled

    recovery = H.publish_outcome(
        domain, checkout,
        _result(case, 31, before=snapshot, checkout=checkout), passed,
    )
    assert recovery.committed and recovery.baseline_published
    assert not recovery.selection_disabled
    assert not marker.exists()
    assert not H.read_history(domain, checkout).selection_disabled


def test_live_writer_inheriting_uncertainty_does_not_mask_it(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    passed = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), passed,
    ).baseline_published
    original_insert = H._insert_summary

    def interrupted(*_args):
        raise TypeError("interrupted publication")

    monkeypatch.setattr(H, "_insert_summary", interrupted)
    with pytest.raises(TypeError, match="interrupted publication"):
        H.publish_outcome(
            domain, checkout,
            _result(case, 10, before=snapshot, checkout=checkout), passed,
        )

    reserved = threading.Event()
    release = threading.Event()
    result = {}
    errors = []

    def blocked_insert(connection, incoming, incoming_inventory):
        reserved.set()
        assert release.wait(timeout=3)
        return original_insert(connection, incoming, incoming_inventory)

    def publish():
        try:
            result["value"] = H.publish_outcome(
                domain, checkout,
                _result(case, 30, before=snapshot, checkout=checkout), passed,
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    monkeypatch.setattr(H, "_insert_summary", blocked_insert)
    writer = threading.Thread(name="inherited-publication-writer", target=publish)
    writer.start()
    assert reserved.wait(timeout=3)
    marker = _store_path(domain, checkout).parent / "history-publication.json"
    try:
        assert json.loads(marker.read_bytes())["sequence"] == 30
        view = H.read_history(domain, checkout)
        assert view.selection_disabled
        assert view.limitations[0].code == "selection-disabled"
    finally:
        release.set()
        writer.join(timeout=5)
    assert not writer.is_alive() and not errors
    assert result["value"].committed and result["value"].baseline_published
    assert not result["value"].selection_disabled
    assert not marker.exists()


def test_reader_classification_at_every_healthy_publication_boundary(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), inventory,
    ).baseline_published
    original_publish = H.publish_atomic
    original_remove = H._remove_marker
    tracked = False
    observations = []
    names = {
        H._ACTIVE_PUBLICATION_NAME,
        H._PUBLICATION_MARKER_NAME,
        H._CAPACITY_RESERVE_NAME,
    }

    def observed_publish(directory, name, payload):
        nonlocal tracked
        value = original_publish(directory, name, payload)
        if name == H._ACTIVE_PUBLICATION_NAME:
            tracked = True
        if tracked and name in names:
            observations.append(("create", name, H._selection_marker(domain, checkout)))
        return value

    def observed_remove(directory, name):
        value = original_remove(directory, name)
        if tracked and name in names:
            observations.append(("remove", name, H._selection_marker(domain, checkout)))
        return value

    monkeypatch.setattr(H, "publish_atomic", observed_publish)
    monkeypatch.setattr(H, "_remove_marker", observed_remove)
    published = H.publish_outcome(
        domain, checkout, _result(case, 2, checkout=checkout), inventory,
    )
    assert published.committed and not published.selection_disabled
    assert observations == [
        ("create", H._ACTIVE_PUBLICATION_NAME, None),
        ("create", H._PUBLICATION_MARKER_NAME, None),
        ("create", H._CAPACITY_RESERVE_NAME, None),
        ("remove", H._CAPACITY_RESERVE_NAME, None),
        ("remove", H._PUBLICATION_MARKER_NAME, None),
        ("remove", H._ACTIVE_PUBLICATION_NAME, None),
    ]


def test_capacity_is_promoted_before_claim_cleanup_and_reserve_is_never_capacity(
    case, monkeypatch,
):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), inventory,
    ).baseline_published
    original_insert = H._insert_summary
    original_remove = H._remove_marker
    original_replace = H.os.replace
    claiming = False
    observations = []

    def full(*_args):
        nonlocal claiming
        claiming = True
        raise _full_error()

    def observed_remove(directory, name):
        value = original_remove(directory, name)
        if claiming and name in {H._ACTIVE_PUBLICATION_NAME, H._PUBLICATION_MARKER_NAME}:
            observations.append(("remove", name, H._selection_marker(domain, checkout)))
        return value

    def observed_replace(source, destination):
        value = original_replace(source, destination)
        if (claiming and Path(source).name == H._CAPACITY_RESERVE_NAME
                and Path(destination).name == H._CAPACITY_MARKER_NAME):
            observations.append(("promote", H._CAPACITY_MARKER_NAME,
                                 H._selection_marker(domain, checkout)))
        return value

    monkeypatch.setattr(H, "_insert_summary", full)
    monkeypatch.setattr(H, "_remove_marker", observed_remove)
    monkeypatch.setattr(H.os, "replace", observed_replace)
    published = H.publish_outcome(
        domain, checkout,
        _result(case, 2, status="failed", checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    assert not published.committed and published.reasons[0].code == "capacity-exceeded"
    assert observations == [
        ("promote", H._CAPACITY_MARKER_NAME, "capacity-exceeded"),
        ("remove", H._ACTIVE_PUBLICATION_NAME, "capacity-exceeded"),
        ("remove", H._PUBLICATION_MARKER_NAME, "capacity-exceeded"),
    ]
    monkeypatch.setattr(H, "_insert_summary", original_insert)


def test_stale_publisher_is_selection_disabled_and_does_not_turn_reserve_into_capacity(
    case, monkeypatch,
):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout,
        _result(case, 1, before=snapshot, checkout=checkout), inventory,
    ).baseline_published
    directory = _store_path(domain, checkout).parent
    H._store_capacity_reserve(directory, 10)
    F.publish_atomic(directory, H._PUBLICATION_MARKER_NAME, json.dumps({
        "version": 1, "code": "selection-disabled", "sequence": 10,
    }).encode())
    F.publish_atomic(directory, H._ACTIVE_PUBLICATION_NAME, json.dumps({
        "version": 1, "pid": os.getpid(),
        "birth": H.psutil.Process(os.getpid()).create_time() + 100,
        "sequence": 10,
    }).encode())

    view = H.read_history(domain, checkout)
    assert view.selection_disabled
    assert view.limitations[0].code == "selection-disabled"
    assert not (directory / "history-capacity.json").exists()
    failed = H.publish_outcome(
        domain, checkout,
        _result(case, 11, status="failed", before=snapshot, checkout=checkout),
        case.inventory(("tests/test_stale.py",), outcome="failed"),
    )
    assert failed.committed and failed.selection_disabled
    assert failed.reasons[0].code == "selection-disabled"
    view = H.read_history(domain, checkout)
    assert any(item.file == "tests/test_stale.py" for item in view.obligations)
    assert view.limitations[0].code == "selection-disabled"
    assert not (directory / "history-capacity.json").exists()


def test_ineligible_commits_preserve_original_capacity_sequence(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout, _result(case, 1, before=snapshot, checkout=checkout), inventory,
    ).baseline_published
    original_insert = H._insert_summary
    monkeypatch.setattr(H, "_insert_summary", lambda *_args: (_ for _ in ()).throw(_full_error()))
    failed = H.publish_outcome(
        domain, checkout, _result(case, 10, status="failed", checkout=checkout),
        case.inventory(("tests/test_a.py",), outcome="failed"),
    )
    assert not failed.committed and failed.reasons[0].code == "capacity-exceeded"
    monkeypatch.setattr(H, "_insert_summary", original_insert)
    marker = _store_path(domain, checkout).parent / "history-capacity.json"
    assert json.loads(marker.read_bytes())["sequence"] == 10

    candidates = (
        (_result(case, 20, before=snapshot, checkout=checkout, mode=C.Mode.SCOPED), inventory),
        (_result(case, 21, before=snapshot, checkout=checkout),
         case.inventory(("tests/test_a.py",), outcome="skipped")),
        (_result(case, 22, before=_snapshot(case, clean=False), checkout=checkout), inventory),
        (_result(case, 9, before=snapshot, checkout=checkout), inventory),
    )
    for candidate, candidate_inventory in candidates:
        published = H.publish_outcome(domain, checkout, candidate, candidate_inventory)
        assert published.committed and published.selection_disabled
        assert json.loads(marker.read_bytes())["sequence"] == 10


def test_old_capacity_failure_cannot_resurrect_after_newer_full_recovery(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(
        domain, checkout, _result(case, 1, before=snapshot, checkout=checkout), inventory,
    ).baseline_published
    old_released_lock = threading.Event()
    let_old_return = threading.Event()
    original_insert = H._insert_summary
    original_lock = H._writer_lock
    results = {}
    old_lock_entries = []

    def insert(connection, incoming, incoming_inventory):
        if incoming.sequence == 10:
            raise _full_error()
        return original_insert(connection, incoming, incoming_inventory)

    @contextmanager
    def interleaved_lock(*args):
        old_entry = threading.current_thread().name == "old-capacity-failure"
        if old_entry:
            old_lock_entries.append(True)
        try:
            with original_lock(*args) as directory:
                yield directory
        except BaseException:
            if old_entry and len(old_lock_entries) == 1:
                old_released_lock.set()
                assert let_old_return.wait(timeout=3)
            raise

    def publish(sequence):
        results[sequence] = H.publish_outcome(
            domain, checkout,
            _result(case, sequence, before=snapshot, checkout=checkout), inventory,
        )

    monkeypatch.setattr(H, "_insert_summary", insert)
    monkeypatch.setattr(H, "_writer_lock", interleaved_lock)
    old = threading.Thread(name="old-capacity-failure", target=publish, args=(10,))
    old.start()
    assert old_released_lock.wait(timeout=3)
    newer = threading.Thread(name="newer-full-recovery", target=publish, args=(20,))
    newer.start()
    newer.join(timeout=5)
    assert not newer.is_alive()
    assert results[20].committed and not results[20].selection_disabled
    let_old_return.set()
    old.join(timeout=5)
    assert not old.is_alive()
    assert not results[10].committed and results[10].selection_disabled
    assert len(old_lock_entries) == 1
    assert not H.read_history(domain, checkout).selection_disabled
    assert not (_store_path(domain, checkout).parent / "history-capacity.json").exists()


@pytest.mark.parametrize("failure", ["database is locked", "database or disk is full"])
def test_interrupted_capacity_recovery_retains_uncertainty_without_claiming_exhaustion(case, monkeypatch, failure):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 1, before=_snapshot(case), checkout=checkout), inventory).committed
    assert H.publish_outcome(domain, checkout, _result(case, 2, checkout=checkout), inventory).committed

    def full(*_args):
        raise _full_error()

    def prune_unavailable(*_args):
        raise sqlite3.OperationalError(failure)

    monkeypatch.setattr(H, "_insert_summary", full)
    monkeypatch.setattr(H, "_capacity_prune", prune_unavailable)
    result = H.publish_outcome(domain, checkout,
                               _result(case, 3, status="failed", checkout=checkout), inventory)
    assert not result.committed
    assert result.reasons[0].code == "coordinator-unavailable"
    view = H.read_history(domain, checkout)
    assert view.selection_disabled and view.limitations[0].code == "capacity-exceeded"
    connection = sqlite3.connect(_store_path(domain, checkout))
    assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    connection.close()


def test_late_peer_cannot_clear_newer_capacity_uncertainty(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 1, before=snapshot, checkout=checkout), inventory).committed
    compacting = threading.Event()
    peer_attempting = threading.Event()
    original_insert, original_lock = H._insert_summary, H._writer_lock
    original_compact = H._compact
    results, errors = {}, []

    def insert(connection, result, inventory):
        if result.sequence == 20:
            raise _full_error()
        return original_insert(connection, result, inventory)

    def compact(connection):
        if threading.current_thread().name == "capacity-writer":
            compacting.set()
            assert peer_attempting.wait(timeout=3)
        original_compact(connection)

    def lock(*args):
        if threading.current_thread().name == "late-peer":
            peer_attempting.set()
        return original_lock(*args)

    def publish(sequence):
        try:
            results[sequence] = H.publish_outcome(domain, checkout,
                _result(case, sequence, before=snapshot, checkout=checkout), inventory)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(H, "_insert_summary", insert)
    monkeypatch.setattr(H, "_compact", compact)
    monkeypatch.setattr(H, "_writer_lock", lock)
    failure = threading.Thread(name="capacity-writer", target=publish, args=(20,))
    peer = threading.Thread(name="late-peer", target=publish, args=(10,))
    failure.start()
    assert compacting.wait(timeout=3)
    peer.start()
    failure.join(timeout=5)
    peer.join(timeout=5)
    assert not failure.is_alive() and not peer.is_alive()
    assert not errors
    assert not results[20].committed and results[20].selection_disabled
    assert results[10].committed and results[10].selection_disabled
    assert H.read_history(domain, checkout).selection_disabled
    marker = _store_path(domain, checkout).parent / "history-capacity.json"
    assert json.loads(marker.read_bytes())["sequence"] == 20


@pytest.mark.parametrize("read", [H.read_history, H.read_history_summaries, H.read_history_payload])
def test_every_read_helper_keeps_one_snapshot_during_peer_writes(case, monkeypatch, read):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout,
                             _result(case, 1, before=_snapshot(case), checkout=checkout), inventory).committed
    original_validate = H._validate_private_runs
    blocked_commits = []

    def validate(connection):
        peer = sqlite3.connect(_store_path(domain, checkout), timeout=0)
        try:
            peer.execute("UPDATE metadata SET value = '1' WHERE key = 'selection_disabled'")
            try:
                peer.commit()
            except sqlite3.OperationalError as exc:
                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
                blocked_commits.append(True)
        finally:
            peer.rollback()
            peer.close()
        original_validate(connection)

    monkeypatch.setattr(H, "_validate_private_runs", validate)
    read(domain, checkout)
    assert blocked_commits == [True]
    assert not (_store_path(domain, checkout).parent / "history-disabled.json").exists()


@pytest.mark.parametrize("failure", ["database or disk is full", "SQL statements in progress"])
def test_post_commit_compaction_failure_cannot_retry_publication(case, monkeypatch, failure):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 1)
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, _result(case, 1, checkout=checkout), inventory).committed
    calls = []
    original_insert = H._insert_summary

    def insert(connection, *args):
        calls.append("insert")
        return original_insert(connection, *args)

    def compact(connection):
        calls.append("compact")
        assert not connection.in_transaction
        assert connection.execute("SELECT sequence FROM baselines").fetchone() == (2,)
        raise sqlite3.OperationalError(failure)

    monkeypatch.setattr(H, "_insert_summary", insert)
    monkeypatch.setattr(H, "_needs_compaction", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(H, "_compact", compact)
    snapshot = _snapshot(case)
    result = _result(case, 2, before=snapshot, checkout=checkout)
    published = H.publish_outcome(domain, checkout, result, inventory)
    assert published.committed and published.baseline_published
    assert not published.selection_disabled
    assert calls == ["insert", "compact"]
    assert H.read_history(domain, checkout).baseline.run_id == result.run_id
    assert not (_store_path(domain, checkout).parent / "history-disabled.json").exists()


def test_small_prune_does_not_trigger_vacuum(case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 1)
    compacted = []
    original_compact = H._compact
    monkeypatch.setattr(H, "_compact", lambda connection: compacted.append(True))
    domain = case.domain()
    checkout = case.checkout(domain)
    for sequence in (1, 2):
        H.publish_outcome(
            domain, checkout, _result(case, sequence, checkout=checkout),
            case.inventory((f"tests/test_{sequence}.py",), outcome="passed"),
        )
    assert compacted == []
    monkeypatch.setattr(H, "_compact", original_compact)


def test_realistic_steady_state_inventory_reuses_free_pages_without_vacuum(case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 8)
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(tuple(f"tests/test_{index}.py" for index in range(500)), outcome="passed")
    assert len(json.dumps(H._inventory_dict(inventory))) > 32768
    compacted = []
    original_compact = H._compact

    def compact(connection):
        compacted.append(True)
        original_compact(connection)

    monkeypatch.setattr(H, "_compact", compact)
    for sequence in range(1, 13):
        assert H.publish_outcome(domain, checkout,
                                 _result(case, sequence, checkout=checkout), inventory).committed
    assert len(H.read_history_summaries(domain, checkout)) == 8
    assert compacted == []


def test_substantial_freelist_triggers_compaction(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    for sequence in range(1, 4):
        assert H.publish_outcome(domain, checkout, _result(
            case, sequence, checkout=checkout,
            limitations=(C.Reason(code="prior-failure", message="x" * (1100 * 1024)),),
        ), inventory).committed
    before_size = _store_path(domain, checkout).stat().st_size
    compacted = []
    original_compact = H._compact

    def compact(connection):
        compacted.append(connection.execute("PRAGMA freelist_count").fetchone()[0])
        original_compact(connection)

    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 1)
    monkeypatch.setattr(H, "_compact", compact)
    assert H.publish_outcome(domain, checkout, _result(case, 4, checkout=checkout), inventory).committed
    assert compacted and all(pages > 8 for pages in compacted)
    assert _store_path(domain, checkout).stat().st_size < before_size / 2


def test_retention_age_uses_candidate_timestamp_not_host_clock(case, monkeypatch):
    class CandidateClock(datetime):
        @classmethod
        def now(cls, *_args, **_kwargs):
            pytest.fail("retention consulted the host clock")

    monkeypatch.setattr(H, "datetime", CandidateClock)
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 20)
    domain = case.domain()
    checkout = case.checkout(domain)
    old = _result(
        case, 1, checkout=checkout,
        finished_at="2099-01-01T00:00:01+00:00",
    )
    H.publish_outcome(
        domain, checkout, old,
        case.inventory(("tests/test_old.py",), outcome="passed"),
    )
    recent = _result(case, 2, checkout=checkout, finished_at="2099-02-01T00:00:01+00:00")
    H.publish_outcome(domain, checkout, recent,
                      case.inventory(("tests/test_recent.py",), outcome="passed"))
    current = _result(
        case, 3, checkout=checkout,
        finished_at="2099-02-15T00:00:01+00:00",
    )
    H.publish_outcome(
        domain, checkout, current,
        case.inventory(("tests/test_current.py",), outcome="passed"),
    )
    assert [item["run_id"] for item in H.read_history_summaries(domain, checkout)] == [
        current.run_id, recent.run_id,
    ]


@pytest.mark.parametrize("local_timezone", ["UTC+12", "UTC-12"])
def test_naive_retention_timestamps_are_utc_not_host_local(case, monkeypatch, local_timezone):
    domain = case.domain()
    checkout = case.checkout(domain)
    inventory = case.inventory(("tests/test_a.py",), outcome="passed")
    old = _result(case, 1, checkout=checkout, finished_at="2099-01-01T12:00:00")
    current = _result(case, 2, checkout=checkout, finished_at="2099-01-31T12:00:01+00:00")
    with monkeypatch.context() as local:
        local.setenv("TZ", local_timezone)
        time.tzset()
        try:
            assert H.publish_outcome(domain, checkout, old, inventory).committed
            assert H.publish_outcome(domain, checkout, current, inventory).committed
            assert [item["run_id"] for item in H.read_history_summaries(domain, checkout)] == [current.run_id]
        finally:
            local.undo()
            time.tzset()


def test_reconciliations_are_bounded_and_do_not_clear_whole_gate(case, monkeypatch):
    monkeypatch.setattr(H, "HISTORY_MAX_SUMMARIES", 2)
    domain = case.domain()
    checkout = case.checkout(domain)
    for sequence in range(1, 7):
        _publish_failure(
            case, domain, checkout, sequence=sequence,
            file=f"tests/test_deleted_{sequence}.py",
        )
        snapshot = _snapshot(case)
        H.publish_outcome(
            domain, checkout,
            _result(case, sequence + 10, before=snapshot, after=snapshot, checkout=checkout),
            case.inventory((f"tests/test_present_{sequence}.py",), outcome="passed"),
        )
    connection = sqlite3.connect(
        domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    )
    count = connection.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0]
    whole_gate = connection.execute(
        "SELECT COUNT(*) FROM reconciliations WHERE file IS NULL AND test_id IS NULL"
    ).fetchone()[0]
    connection.close()
    assert count <= H.HISTORY_MAX_SUMMARIES
    assert whole_gate == 0


def test_partial_schema_is_never_published_as_normal_state(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    original_open = H.open_database

    def deny_index(*args, **kwargs):
        connection = original_open(*args, **kwargs)
        connection.set_authorizer(
            lambda action, *_rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_INDEX
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(H, "open_database", deny_index)
    with pytest.raises(sqlite3.Error):
        H._open_store(domain, checkout, create=True)
    path = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    connection = sqlite3.connect(path)
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()
    assert tables == set()


def test_partial_nonempty_schema_read_persists_corruption(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    directory = F.ensure_private_dir(F.ensure_private_dir(domain.root, "checkouts"), checkout.checkout_id)
    path = F.create_exclusive(directory, "history.sqlite3", b"")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    view = H.read_history(domain, checkout)
    assert view.selection_disabled
    assert view.limitations[0].code == "coordinator-corrupt"
    for read in (H.read_history_summaries, H.read_history_payload):
        with pytest.raises(C.Problem, match="coordinator-corrupt"):
            read(domain, checkout)
    assert path.read_bytes() == before
    assert json.loads((directory / "history-disabled.json").read_bytes())["code"] == "coordinator-corrupt"


def test_pre_t12a_store_reads_without_mutation_then_writer_upgrades(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    initial = _result(
        case, 1, before=snapshot, after=snapshot, checkout=checkout,
    )
    inventory = case.inventory(("tests/test_old.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, initial, inventory).committed
    path = _store_path(domain, checkout)
    _downgrade_store_to_pre_t12a(path)
    before = path.read_bytes()

    view = H.read_history(domain, checkout)

    assert view.selection_disabled is False
    assert view.selection_quarantine is None
    assert view.baseline is not None
    assert view.baseline.runtime_identity is None
    assert path.read_bytes() == before
    assert not path.with_name("history-disabled.json").exists()

    newer = replace(_result(
        case, 2, before=snapshot, after=snapshot, checkout=checkout,
    ), runtime_identity="44" * 32)
    assert H.publish_outcome(domain, checkout, newer, inventory).committed
    connection = sqlite3.connect(path)
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    baseline_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(baselines)")
    }
    connection.close()
    assert H._REQUIRED_TABLES.issubset(tables)
    assert "runtime_identity" in baseline_columns
    assert H.read_history(domain, checkout).baseline.runtime_identity == "44" * 32


def test_writer_never_upgrades_a_partial_compound_schema(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    snapshot = _snapshot(case)
    initial = _result(
        case, 1, before=snapshot, after=snapshot, checkout=checkout,
    )
    inventory = case.inventory(("tests/test_old.py",), outcome="passed")
    assert H.publish_outcome(domain, checkout, initial, inventory).committed
    path = _store_path(domain, checkout)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE comparison_receipts")
        connection.commit()

    published = H.publish_outcome(
        domain, checkout,
        replace(initial, run_id="f" * 32, sequence=2), inventory,
    )

    assert published.committed is False
    assert published.reasons[0].code == "coordinator-corrupt"
    with sqlite3.connect(path) as connection:
        assert "comparison_receipts" not in {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", ("f" * 32,)
        ).fetchone() == (0,)
    assert json.loads(
        path.with_name("history-disabled.json").read_bytes()
    )["code"] == "coordinator-corrupt"


def test_concurrent_first_initializers_publish_complete_schema(case, monkeypatch):
    domain = case.domain()
    checkout = case.checkout(domain)
    original_ensure = H._ensure_schema
    lock = threading.Lock()
    first_inspection = threading.Event()
    peer_attempting = threading.Event()
    peer_entered_schema = threading.Event()
    release_first = threading.Event()
    observations = []
    first_thread = None
    original_writer_lock = H._writer_lock

    @contextmanager
    def observed_writer_lock(*args):
        if threading.current_thread().name == "initializer-b":
            peer_attempting.set()
        with original_writer_lock(*args) as directory:
            yield directory

    def synchronized_ensure(connection, *args, **kwargs):
        nonlocal first_thread
        with lock:
            if first_thread is None:
                first_thread = threading.current_thread().name
            elif threading.current_thread().name != first_thread:
                peer_entered_schema.set()
        return original_ensure(connection, *args, **kwargs)

    original_tables = H._schema_tables

    def observed_tables(connection):
        tables = original_tables(connection)
        observations.append((threading.current_thread().name, tables))
        if threading.current_thread().name == first_thread and not first_inspection.is_set():
            first_inspection.set()
            assert peer_attempting.wait(timeout=5)
            release_first.wait(timeout=5)
        return tables

    monkeypatch.setattr(H, "_ensure_schema", synchronized_ensure)
    monkeypatch.setattr(H, "_schema_tables", observed_tables)
    monkeypatch.setattr(H, "_writer_lock", observed_writer_lock)
    results = []
    errors = []

    def publish(sequence):
        try:
            results.append(
                H.publish_outcome(
                    domain, checkout,
                    _result(case, sequence, checkout=checkout),
                    case.inventory((f"tests/test_{sequence}.py",), outcome="passed"),
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    first = threading.Thread(name="initializer-a", target=publish, args=(1,))
    peer = threading.Thread(name="initializer-b", target=publish, args=(2,))
    first.start()
    assert first_inspection.wait(timeout=5)
    peer.start()
    assert peer_attempting.wait(timeout=5)
    assert not peer_entered_schema.is_set()
    release_first.set()
    first.join(timeout=10)
    peer.join(timeout=10)
    assert not errors
    assert len(results) == 2
    assert all(item.committed and not item.selection_disabled for item in results)
    assert any(name == "initializer-a" and not tables for name, tables in observations)
    peer_observations = [tables for name, tables in observations if name == "initializer-b"]
    assert peer_observations
    assert all(H._REQUIRED_TABLES.issubset(tables) for tables in peer_observations)
    view = H.read_history(domain, checkout)
    assert view.selection_disabled is False
    assert not (domain.root / "checkouts" / checkout.checkout_id / "history-disabled.json").exists()
