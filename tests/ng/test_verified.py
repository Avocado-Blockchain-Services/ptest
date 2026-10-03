"""Project-wide ledger of green full gates, shared by a project's checkouts."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ptest import verified


def _record(n: int = 1, **overrides) -> verified.Record:
    values = dict(
        run_id=f"{n:032x}", head=f"{n:040x}", input_digest=f"{n:064x}",
        compatibility="cc" * 32, policy_digest="dd" * 32,
        runtime_identity="ee" * 32, scope="api",
        checkout_id=f"{n + 1000:032x}", root=f"/work/tree-{n}",
        created_at="2026-09-29T12:00:00+00:00")
    values.update(overrides)
    return verified.Record(**values)


def _green(domain, project, record):
    token = verified.begin_full(domain, project, record.input_digest)
    verified.record_green(domain, project, record, token=token)


def _ledger(domain, project_id: str) -> Path:
    return Path(domain.root) / "projects" / project_id / "verified.json"


def test_a_recorded_green_gate_is_found_by_every_checkout_of_the_project(case):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))

    assert verified.find(domain, "ab" * 16) == (_record(1),)
    assert verified.find(domain, "cd" * 16) == ()


def test_newest_first_and_bounded(case):
    domain = case.domain()
    for n in range(1, verified.MAX_RECORDS + 6):
        _green(domain, "ab" * 16, _record(n))

    found = verified.find(domain, "ab" * 16)

    assert len(found) == verified.MAX_RECORDS
    assert found[0] == _record(verified.MAX_RECORDS + 5)
    assert _record(1) not in found


def test_recording_the_same_tree_again_keeps_one_record(case):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    _green(domain, "ab" * 16, _record(1, run_id="99" * 16))

    assert [item.run_id for item in verified.find(domain, "ab" * 16)] == ["99" * 16]


def test_a_failed_full_run_on_the_same_tree_forgets_it(case):
    # A flaky pass must not be reused once the same inputs failed.
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    _green(domain, "ab" * 16, _record(2))

    verified.forget(domain, "ab" * 16, _record(1).input_digest)

    assert verified.find(domain, "ab" * 16) == (_record(2),)


def test_ledger_files_are_private(case):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    path = _ledger(domain, "ab" * 16)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_a_corrupt_ledger_reads_as_empty_and_is_replaced(case):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    path = _ledger(domain, "ab" * 16)
    path.write_text("{not json", encoding="utf-8")

    assert verified.find(domain, "ab" * 16) == ()
    _green(domain, "ab" * 16, _record(2))
    assert verified.find(domain, "ab" * 16) == (_record(2),)


def test_malformed_entries_are_skipped_not_trusted(case):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    path = _ledger(domain, "ab" * 16)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["records"].insert(0, {"run_id": "zz"})
    path.write_text(json.dumps(data), encoding="utf-8")

    assert verified.find(domain, "ab" * 16) == (_record(1),)


def test_invalid_project_ids_never_touch_the_filesystem(case):
    domain = case.domain()
    for bad in ("../escape", "", "AB" * 16, "ab" * 15):
        verified.record_green(domain, bad, _record(1))
        assert verified.find(domain, bad) == ()
    assert not (Path(domain.root) / "projects").exists()


def test_a_symlinked_ledger_is_never_followed(case, tmp_path):
    domain = case.domain()
    _green(domain, "ab" * 16, _record(1))
    path = _ledger(domain, "ab" * 16)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(elsewhere)

    assert verified.find(domain, "ab" * 16) == ()
    _green(domain, "ab" * 16, _record(2))
    assert elsewhere.read_text(encoding="utf-8").count("run_id") == 1


def test_failure_fences_delayed_green_and_later_run_can_restore(case):
    domain = case.domain()
    project = 'ab' * 16
    record = _record()
    begin = getattr(verified, 'begin_full', None)
    assert callable(begin), 'full publishers need a pre-execution withdrawal token'
    delayed = begin(domain, project, record.input_digest)
    verified.forget(domain, project, record.input_digest)
    verified.record_green(domain, project, record, token=delayed)
    assert verified.find(domain, project) == ()
    later = begin(domain, project, record.input_digest)
    verified.record_green(domain, project, record, token=later)
    assert verified.find(domain, project) == (record,)
    verified.forget(domain, project, record.input_digest)
    verified.record_green(domain, project, record, token=later)
    assert verified.find(domain, project) == ()


def test_withdrawal_fence_survives_more_than_record_limit_and_keeps_other_project(case):
    domain = case.domain()
    project = 'ab' * 16
    begin = getattr(verified, 'begin_full', None)
    assert callable(begin), 'bounded ledger must preserve withdrawal ordering'
    delayed = begin(domain, project, _record().input_digest)
    _green(domain, 'cd' * 16, _record())
    for n in range(1, verified.MAX_RECORDS * 2):
        verified.forget(domain, project, _record(n).input_digest)
    verified.record_green(domain, project, _record(), token=delayed)
    assert verified.find(domain, project) == ()
    assert verified.find(domain, 'cd' * 16) == (_record(),)


def test_missing_token_cannot_publish_even_on_first_run(case):
    domain = case.domain()
    verified.record_green(domain, 'ab' * 16, _record())
    assert verified.find(domain, 'ab' * 16) == ()


def test_cross_project_token_cannot_authorize_green(case):
    domain = case.domain()
    token = verified.begin_full(domain, 'ab' * 16, _record().input_digest)
    verified.record_green(domain, 'cd' * 16, _record(), token=token)
    assert verified.find(domain, 'cd' * 16) == ()


def test_old_writer_ledger_without_ordering_never_resurrects_failed_green(case):
    domain = case.domain()
    token = verified.begin_full(domain, 'ab' * 16, _record().input_digest)
    verified.forget(domain, 'ab' * 16, _record().input_digest)
    from dataclasses import asdict
    _ledger(domain, 'ab' * 16).write_text(json.dumps({
        'version': 1, 'records': [asdict(_record())]}))
    assert verified.find(domain, 'ab' * 16) == ()
    verified.record_green(domain, 'ab' * 16, _record(), token=token)
    assert verified.find(domain, 'ab' * 16) == ()


def test_corrupt_records_cannot_authorize_old_publication_token(case):
    domain = case.domain()
    project = 'ab' * 16
    record = _record()
    token = verified.begin_full(domain, project, record.input_digest)
    path = _ledger(domain, project)
    data = json.loads(path.read_text())
    data['records'] = 'corrupt'
    path.write_text(json.dumps(data))
    verified.record_green(domain, project, record, token=token)
    assert verified.find(domain, project) == ()
