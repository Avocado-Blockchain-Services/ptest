"""Behavioral contracts for durable local history and selection evidence."""
from __future__ import annotations

import ast
import builtins
import importlib.util
import io
import json
import os
import pwd
import sqlite3
import threading
import time
from contextlib import contextmanager
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
