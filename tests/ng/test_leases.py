"""Unit contracts for leases.py and the new files.py helpers.

Kernel-real files, in-process: identity format and fail-closed unknowns,
the is_foreign truth table, exact sidecar bytes and 0600 modes, lock
verdicts (HELD while held, ACQUIRABLE after release, INDETERMINATE for
anything replaced/missing/corrupt/symlinked), probe-never-pins, ordered
removal, rollback, adoption, bounded listing, and the files.py contracts.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ptest import files
from ptest import leases
from ptest.contracts import Problem


def _root(tmp_path: Path) -> Path:
    path = tmp_path / "domain"
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _run_id(seed: str) -> str:
    return (seed.encode().hex() + "c" * 32)[:32]


def _leases_dir(root: Path) -> Path:
    return root / leases.LEASES_DIRNAME


# -- namespace identity -----------------------------------------------------

def test_namespace_identity_format_matches_documented_shape():
    identity = leases.namespace_identity()
    assert identity == "host" or identity == "unknown" or (
        identity.startswith("pid:[") and "@" in identity)
    if identity not in ("host", "unknown"):
        link, _, start = identity.partition("@")
        assert link.startswith("pid:[") and link.endswith("]")
        assert link[5:-1].isdigit()
        assert start.isdigit()


def test_namespace_identity_non_linux_is_host(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert leases.namespace_identity() == "host"


def test_namespace_identity_inconsistent_readlink_is_unknown(monkeypatch):
    real = os.readlink
    calls = {"n": 0}

    def flapping(path):
        if path == "/proc/self/ns/pid":
            calls["n"] += 1
            return f"pid:[{400000 + calls['n']}]"
        return real(path)

    monkeypatch.setattr(os, "readlink", flapping)
    assert leases.namespace_identity() == "unknown"


def test_namespace_identity_self_mismatch_is_unknown(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda path: "pid:[1]" if path != "/proc/self" else "1")
    assert leases.namespace_identity() == "unknown"


def test_namespace_identity_unreadable_is_unknown(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda path: (_ for _ in ()).throw(OSError(2, "no proc")))
    assert leases.namespace_identity() == "unknown"


def test_namespace_identity_bad_stat_is_unknown(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda path: "pid:[4026531836]" if path != "/proc/self" else str(os.getpid()))
    monkeypatch.setattr(leases, "_read_proc_stat_starttime", lambda: (_ for _ in ()).throw(OSError(13, "denied")))
    assert leases.namespace_identity() == "unknown"


def test_namespace_identity_malformed_link_is_unknown(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda path: "not-a-namespace" if path != "/proc/self" else str(os.getpid()))
    assert leases.namespace_identity() == "unknown"


# -- is_foreign --------------------------------------------------------------

@pytest.mark.parametrize(("recorded", "observer", "expected"), [
    ("unknown", "unknown", True),
    ("unknown", "host", True),
    ("host", "unknown", True),
    ("host", "host", False),
    ("pid:[1]@2", "pid:[1]@2", False),
    ("pid:[1]@2", "pid:[1]@3", True),
    ("pid:[1]@2", "host", True),
])
def test_is_foreign_truth_table(recorded, observer, expected):
    assert leases.is_foreign(recorded, observer) is expected


# -- create -------------------------------------------------------------------

def test_create_writes_exact_sidecar_bytes_private_and_non_inheritable(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("exact")
    fd = leases.create(root, run_id)
    try:
        assert leases.held_fd(root, run_id) == fd
        assert os.get_inheritable(fd) is False
        lock_path = _leases_dir(root) / f"{run_id}.lock"
        sidecar_path = _leases_dir(root) / f"{run_id}.json"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(_leases_dir(root).stat().st_mode) == 0o700
        stamp = os.fstat(fd)
        expected = json.dumps(
            {"version": 1, "run_id": run_id,
             "pid_namespace": leases.namespace_identity(),
             "lease_device": stamp.st_dev, "lease_inode": stamp.st_ino},
            sort_keys=True, separators=(",", ":")).encode("ascii")
        assert sidecar_path.read_bytes() == expected
        sidecar = leases.read_sidecar(root, run_id)
        assert sidecar.status == "valid"
        assert (sidecar.lease_device, sidecar.lease_inode) == (stamp.st_dev, stamp.st_ino)
    finally:
        leases.release_held(root, run_id)


def test_duplicate_create_is_already_exists(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("dup")
    leases.create(root, run_id)
    with pytest.raises(Problem) as caught:
        leases.create(root, run_id)
    assert caught.value.code == "already-exists"
    leases.release_held(root, run_id)


def test_sidecar_failure_removes_lock_and_raises_state_unavailable(tmp_path, monkeypatch):
    root = _root(tmp_path)
    run_id = _run_id("sidecar-fail")

    def broken(*args, **kwargs):
        raise Problem(code="state-unavailable", message="no space", phase="files")

    monkeypatch.setattr(files, "create_exclusive", broken)
    with pytest.raises(Problem) as caught:
        leases.create(root, run_id)
    assert caught.value.code == "state-unavailable"
    assert caught.value.message == "run lease cannot be created"
    assert not (_leases_dir(root) / f"{run_id}.lock").exists()
    assert leases.held_fd(root, run_id) is None


# -- probe ---------------------------------------------------------------------

def test_probe_held_while_held_acquirable_after_release(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("held")
    leases.create(root, run_id)
    sidecar = leases.read_sidecar(root, run_id)
    assert leases.probe(root, run_id, sidecar) == leases.HELD
    leases.release_held(root, run_id)
    assert leases.probe(root, run_id, sidecar) == leases.ACQUIRABLE


def test_probe_never_pins_and_leaves_no_fd_behind(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("nopin")

    def fd_count():
        return len(os.listdir("/proc/self/fd"))

    leases.create(root, run_id)
    sidecar = leases.read_sidecar(root, run_id)
    before = fd_count()
    assert leases.probe(root, run_id, sidecar) == leases.HELD
    assert leases.probe(root, run_id, sidecar) == leases.HELD
    assert fd_count() == before
    leases.release_held(root, run_id)
    # Releasing drops the held fd itself; the probes above held nothing,
    # so the lock is acquirable again with no new fd retained.
    released_baseline = fd_count()
    assert released_baseline == before - 1
    assert leases.probe(root, run_id, sidecar) == leases.ACQUIRABLE
    assert fd_count() == released_baseline


def test_replaced_lease_file_never_probes_acquirable_and_refuses_removal(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("replaced")
    leases.create(root, run_id)
    sidecar = leases.read_sidecar(root, run_id)
    assert sidecar.status == "valid"
    lock_path = _leases_dir(root) / f"{run_id}.lock"
    os.unlink(lock_path)
    # A fresh file at the same path is a different inode: not our lease.
    replacement = files.create_locked(_leases_dir(root), f"{run_id}.lock")
    try:
        assert leases.probe(root, run_id, sidecar) == leases.INDETERMINATE
        assert leases.remove_if_released(root, run_id) is False
        assert lock_path.exists()
    finally:
        os.close(replacement)
        leases.release_held(root, run_id)


def test_missing_lock_probes_indeterminate(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("missing")
    leases.create(root, run_id)
    sidecar = leases.read_sidecar(root, run_id)
    os.unlink(_leases_dir(root) / f"{run_id}.lock")
    try:
        assert leases.probe(root, run_id, sidecar) == leases.INDETERMINATE
    finally:
        leases.release_held(root, run_id)


@pytest.mark.parametrize("status", ["absent", "invalid"])
def test_missing_or_invalid_sidecar_probes_indeterminate(tmp_path, status):
    root = _root(tmp_path)
    run_id = _run_id(f"bad-{status}")
    if status == "absent":
        sidecar = leases.read_sidecar(root, run_id)
        assert sidecar.status == "absent"
    else:
        leases.create(root, run_id)
        try:
            with open(_leases_dir(root) / f"{run_id}.json", "wb") as stream:
                stream.write(b"{not json")
            sidecar = leases.read_sidecar(root, run_id)
            assert sidecar.status == "invalid"
        finally:
            leases.release_held(root, run_id)
    assert leases.probe(root, run_id, sidecar) == leases.INDETERMINATE


def test_oversized_sidecar_is_invalid(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("big")
    leases.create(root, run_id)
    try:
        with open(_leases_dir(root) / f"{run_id}.json", "wb") as stream:
            stream.write(b"1" * (leases.SIDECAR_MAX_BYTES + 1))
        assert leases.read_sidecar(root, run_id).status == "invalid"
    finally:
        leases.release_held(root, run_id)


def test_symlinked_lock_and_sidecar_are_never_followed(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("links")
    leases.create(root, run_id)
    sidecar = leases.read_sidecar(root, run_id)
    assert sidecar.status == "valid"
    outside = tmp_path / "outside"
    outside.write_bytes(b"decoy")
    lock_path = _leases_dir(root) / f"{run_id}.lock"
    sidecar_path = _leases_dir(root) / f"{run_id}.json"
    os.unlink(lock_path)
    os.unlink(sidecar_path)
    os.symlink(outside, lock_path)
    os.symlink(outside, sidecar_path)
    try:
        assert leases.read_sidecar(root, run_id).status == "invalid"
        assert leases.probe(root, run_id, sidecar) == leases.INDETERMINATE
        assert outside.read_bytes() == b"decoy"
    finally:
        os.unlink(lock_path)
        os.unlink(sidecar_path)
        leases.release_held(root, run_id)


# -- removal / rollback / adoption ----------------------------------------------

def test_remove_if_released_removes_both_and_refuses_while_held(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("remove")
    leases.create(root, run_id)
    assert leases.remove_if_released(root, run_id) is False
    assert (_leases_dir(root) / f"{run_id}.lock").exists()
    leases.release_held(root, run_id)
    assert leases.remove_if_released(root, run_id) is True
    assert not (_leases_dir(root) / f"{run_id}.lock").exists()
    assert not (_leases_dir(root) / f"{run_id}.json").exists()
    assert leases.remove_if_released(root, run_id) is False


def test_remove_if_released_leaves_lone_sidecar_without_lock(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("lone")
    leases.create(root, run_id)
    sidecar_path = _leases_dir(root) / f"{run_id}.json"
    saved = sidecar_path.read_bytes()
    os.unlink(_leases_dir(root) / f"{run_id}.lock")
    leases.release_held(root, run_id)
    # No lock to take: the sidecar is left in place, never deleted blind.
    assert leases.remove_if_released(root, run_id) is False
    assert sidecar_path.read_bytes() == saved


def test_discard_created_removes_files_and_drops_fd(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("discard")
    leases.create(root, run_id)
    leases.discard_created(root, run_id)
    assert leases.held_fd(root, run_id) is None
    assert not (_leases_dir(root) / f"{run_id}.lock").exists()
    assert not (_leases_dir(root) / f"{run_id}.json").exists()


def test_adopt_inherited_accepts_matching_fd_and_rejects_mismatch(tmp_path):
    root = _root(tmp_path)
    run_id = _run_id("adopt")
    fd = leases.create(root, run_id)
    try:
        leases.adopt_inherited(root, run_id, fd)
        assert leases.held_fd(root, run_id) == fd
        assert os.get_inheritable(fd) is False
    finally:
        leases.release_held(root, run_id)
    other = os.open(tmp_path / "other", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(Problem) as caught:
            leases.adopt_inherited(root, _run_id("adopt-missing"), other)
        assert caught.value.code == "ownership-uncertain"
        with pytest.raises(Problem):
            leases.adopt_inherited(root, run_id, -1)
    finally:
        os.close(other)


# -- run_ids ---------------------------------------------------------------------

def test_run_ids_sorted_bounded_and_never_creates(tmp_path):
    root = _root(tmp_path)
    assert leases.run_ids(root) == ()
    assert not _leases_dir(root).exists()
    second = _run_id("b-second")
    first = _run_id("a-first")
    third = "9" * 32
    leases.create(root, second)
    leases.create(root, first)
    try:
        (_leases_dir(root) / (third + ".lock")).touch()
        (_leases_dir(root) / "not-hex.lock").touch()
        (_leases_dir(root) / (first + ".lock.tmp.1.deadbeef")).touch()
        got = leases.run_ids(root)
        assert got == tuple(sorted([first, second, third]))
        assert leases.run_ids(root, limit=2) == tuple(sorted([first, second, third])[:2])
    finally:
        leases.release_all_held()


# -- files.py helpers ---------------------------------------------------------------

def test_create_locked_duplicate_is_already_exists(tmp_path):
    root = _root(tmp_path)
    sub = files.ensure_private_dir(root, "w")
    fd = files.create_locked(sub, "one.lock")
    try:
        with pytest.raises(Problem) as caught:
            files.create_locked(sub, "one.lock")
        assert caught.value.code == "already-exists"
    finally:
        os.close(fd)


def test_create_locked_fd_is_non_inheritable_and_private(tmp_path):
    root = _root(tmp_path)
    sub = files.ensure_private_dir(root, "w")
    fd = files.create_locked(sub, "n.lock")
    try:
        assert os.get_inheritable(fd) is False
        stamp = os.fstat(fd)
        assert stat.S_ISREG(stamp.st_mode)
        assert stat.S_IMODE(stamp.st_mode) == 0o600
        assert stamp.st_nlink == 1
    finally:
        os.close(fd)


def test_open_regular_fd_refuses_symlink_fifo_and_missing(tmp_path):
    root = _root(tmp_path)
    sub = files.ensure_private_dir(root, "w")
    (sub / "real").write_bytes(b"x")
    os.chmod(sub / "real", 0o600)
    os.symlink(sub / "real", sub / "link")
    with pytest.raises(Problem) as caught:
        files.open_regular_fd(sub, "link")
    assert caught.value.code == "unsafe-path"
    os.mkfifo(sub / "pipe")
    with pytest.raises(Problem) as caught:
        files.open_regular_fd(sub, "pipe")
    assert caught.value.code == "unsafe-path"
    with pytest.raises(Problem) as caught:
        files.open_regular_fd(sub, "absent")
    assert caught.value.code == "state-unavailable"


def test_unlink_if_same_identity_checked(tmp_path):
    root = _root(tmp_path)
    sub = files.ensure_private_dir(root, "w")
    fd = files.create_locked(sub, "v.lock")
    try:
        stamp = os.fstat(fd)
        assert files.unlink_if_same(sub, "missing", stamp.st_dev, stamp.st_ino) is False
        assert files.unlink_if_same(sub, "v.lock", stamp.st_dev, stamp.st_ino + 1) is False
        assert (sub / "v.lock").exists()
        assert files.unlink_if_same(sub, "v.lock", stamp.st_dev, stamp.st_ino) is True
        assert not (sub / "v.lock").exists()
    finally:
        os.close(fd)
