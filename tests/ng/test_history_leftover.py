"""An empty, non-private history store is a leftover, not history (0.6.5).

Bug report (0.6.4 reuse trial, 2026-10-12): muse-msp passed all 48 tests
under ``reuse = true`` and ended ``incomplete (exit 70)``: "advanced attempt
history could not be committed". Its checkout held a zero-byte
``history.sqlite3`` with mode 0644, made by another program on 2026-09-28,
as were 135 others. The normal mode never opens the store, so the leftover
only surfaced once the project opted in to the full mode.

Oracle: a zero-byte file holds no history, so treating it as absent loses
nothing; every store with content keeps the fail-closed privacy checks.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import files as F
from ptest import history as H


def _store_dir(root: Path, checkout_id: str) -> Path:
    checkouts = F.ensure_private_dir(root, H._CHECKOUTS_NAME)
    return F.ensure_private_dir(checkouts, checkout_id)


def _leftover(directory: Path, data: bytes = b"", mode: int = 0o644) -> Path:
    path = directory / H._STORE_NAME
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _baseline(case, checkout):
    snapshot = case.snapshot(compatibility="compat-v1")
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest, compatibility=snapshot.compatibility)
    return case.result(sequence=0, status="passed", mode=C.Mode.FULL, phase="complete",
                       plan=plan, input_before=snapshot, input_after=snapshot,
                       policy_digest="22" * 32, full_gate_eligible=True,
                       project_id=checkout.project_id, checkout_id=checkout.checkout_id)


def _publish(case, domain, checkout):
    return H.publish_outcome(domain, checkout, _baseline(case, checkout),
                             case.inventory(("tests/test_a.py",), outcome="passed"))


def test_a_write_sets_an_empty_leftover_aside_and_records_history(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    directory = _store_dir(domain.root, checkout.checkout_id)
    _leftover(directory)

    assert _publish(case, domain, checkout).baseline_published

    store = directory / H._STORE_NAME
    assert store.stat().st_size > 0
    assert store.stat().st_mode & 0o777 == 0o600
    aside = sorted(directory.glob(H._STORE_NAME + ".empty-0644.*"))
    assert len(aside) == 1 and aside[0].stat().st_size == 0
    assert H.read_history(domain, checkout).baseline is not None


def test_a_reader_treats_an_empty_leftover_as_no_history_and_moves_nothing(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    path = _leftover(_store_dir(domain.root, checkout.checkout_id))

    assert H.next_sequence(domain, checkout) == 0
    assert H.read_history(domain, checkout).baseline is None
    assert path.exists() and path.stat().st_mode & 0o777 == 0o644
    assert not list(path.parent.glob(H._STORE_NAME + ".empty-*"))


def _content(directory: Path, tmp_path: Path) -> Path:
    return _leftover(directory, b"not a database")


def _symlink(directory: Path, tmp_path: Path) -> Path:
    target = tmp_path / "elsewhere.sqlite3"
    target.write_bytes(b"")
    os.chmod(target, 0o644)
    path = directory / H._STORE_NAME
    path.symlink_to(target)
    return path


def _hard_link(directory: Path, tmp_path: Path) -> Path:
    path = _leftover(directory)
    os.link(path, tmp_path / "second-name")
    return path


@pytest.mark.parametrize("make", [_content, _symlink, _hard_link],
                         ids=["has-content", "symlink", "hard-link"])
def test_anything_but_an_empty_private_leftover_is_still_refused(case, tmp_path, make):
    domain = case.domain()
    checkout = case.checkout(domain)
    path = make(_store_dir(domain.root, checkout.checkout_id), tmp_path)
    before = os.lstat(path)

    with pytest.raises(C.Problem):
        H.next_sequence(domain, checkout)
    assert not _publish(case, domain, checkout).committed

    after = os.lstat(path)
    assert (after.st_ino, after.st_mode, after.st_size) == (
        before.st_ino, before.st_mode, before.st_size)
    assert not list(path.parent.glob(H._STORE_NAME + ".empty-*"))


# End to end: the opted-in project the trial found


def _project_store(domain, root: Path) -> Path:
    checkout_id = hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]
    return _store_dir(domain.root, checkout_id)


def test_an_opted_in_full_run_survives_an_empty_leftover(case):
    from test_reuse_opt_in import _full, _project, _verified

    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain)
    _leftover(_project_store(domain, root))

    first = _full(case, domain, root)
    assert first.code == 0, first.stderr.decode()
    assert first.result["data"]["baseline_published"] is True
    second = _full(case, domain, root)
    assert _verified(second), second.stderr.decode()


def test_a_refused_store_is_named_in_the_end_line(case):
    from test_reuse_opt_in import _full, _project

    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain)
    _leftover(_project_store(domain, root), b"not a database")

    completed = _full(case, domain, root)
    assert completed.code == 70
    assert b"history.sqlite3 must be mode 0600" in completed.stderr, (
        completed.stderr.decode())


def test_a_writer_refuses_a_real_store_that_is_not_private(case):
    """Before 0.6.5 only readers checked the mode; a writer appended new
    history to a world-readable store."""
    domain = case.domain()
    checkout = case.checkout(domain)
    assert _publish(case, domain, checkout).committed
    store = _store_dir(domain.root, checkout.checkout_id) / H._STORE_NAME
    os.chmod(store, 0o644)
    size = store.stat().st_size

    assert not _publish(case, domain, checkout).committed
    assert store.stat().st_mode & 0o777 == 0o644 and store.stat().st_size == size
