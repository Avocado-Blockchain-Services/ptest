"""Safe file primitive behavior and abuse tests (Task0 owned)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ptest.contracts import Problem
from support import CONTROL_VARS
from ptest.files import (
    create_exclusive,
    ensure_private_dir,
    ensure_shared_dir,
    publish_atomic,
    read_regular,
    validate_private_dir,
    validate_private_file,
    validate_single_name,
)


def test_read_regular_rejects_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "sentinel"
    outside.write_text("outside-secret")
    (root / "alias").symlink_to(outside)
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "alias", 1024)


def test_read_regular_roundtrip_bounded(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "note.txt").write_bytes(b"hello")
    assert read_regular(root, "sub/note.txt", 1024) == b"hello"
    assert read_regular(root, "sub/note.txt", 3) == b"hel"


def test_read_regular_rejects_dotdot_and_absolute(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "../sentinel", 1024)
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "/etc/hostname", 1024)


def test_read_regular_rejects_directory_swap(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "real").mkdir()
    (root / "real" / "data.txt").write_bytes(b"safe")
    link_dir = tmp_path / "evil"
    link_dir.mkdir()
    (link_dir / "data.txt").write_bytes(b"planted")
    (root / "swap").symlink_to(link_dir)
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "swap/data.txt", 1024)


def test_read_regular_rejects_fifo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "pipe", 1024)


def test_read_regular_missing_is_typed_absence(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(Problem, match="state-unavailable"):
        read_regular(root, "absent.txt", 1024)


def test_validate_private_dir_accepts_0700(tmp_path):
    target = tmp_path / "exclusive"
    target.mkdir(mode=0o700)
    validate_private_dir(target)


def test_validate_private_dir_rejects_group_writable(tmp_path):
    target = tmp_path / "shared"
    target.mkdir(mode=0o755)
    with pytest.raises(Problem, match="unsafe-path"):
        validate_private_dir(target)


def test_validate_private_file_rejects_hardlink(tmp_path):
    target = tmp_path / "state.bin"
    target.write_bytes(b"data")
    os.chmod(target, 0o600)
    os.link(target, tmp_path / "alias.bin")
    with pytest.raises(Problem, match="unsafe-path"):
        validate_private_file(target)


def test_validate_private_file_rejects_symlink(tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"data")
    os.chmod(target, 0o600)
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    with pytest.raises(Problem, match="unsafe-path"):
        validate_private_file(link)


def test_ensure_shared_dir_creates_0700_and_keeps_0755(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    created = ensure_shared_dir(parent, "fresh")
    assert stat.S_IMODE(os.stat(created).st_mode) == 0o700
    legacy = parent / "legacy"
    legacy.mkdir(mode=0o755)
    kept = ensure_shared_dir(parent, "legacy")
    assert kept == legacy
    assert stat.S_IMODE(os.stat(legacy).st_mode) == 0o755


def test_ensure_shared_dir_never_chmods_existing(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    existing = parent / "keep"
    existing.mkdir(mode=0o750)
    ensure_shared_dir(parent, "keep")
    assert stat.S_IMODE(os.stat(existing).st_mode) == 0o750


def test_ensure_shared_dir_rejects_recursive_creation(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    with pytest.raises(Problem, match="unsafe-path"):
        ensure_shared_dir(parent, "a/b")


def test_ensure_private_dir_requires_0700(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    created = ensure_private_dir(parent, "child")
    assert stat.S_IMODE(os.stat(created).st_mode) == 0o700
    lax = parent / "lax"
    lax.mkdir(mode=0o755)
    with pytest.raises(Problem, match="unsafe-path"):
        ensure_private_dir(parent, "lax")
    assert stat.S_IMODE(os.stat(lax).st_mode) == 0o755


def test_create_exclusive_roundtrip_modes(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = create_exclusive(root, "secret.txt", b"shh", private=True)
    assert private.read_bytes() == b"shh"
    assert stat.S_IMODE(os.stat(private).st_mode) == 0o600
    public = create_exclusive(root, "guide.txt", b"hi", private=False)
    assert stat.S_IMODE(os.stat(public).st_mode) == 0o644


def test_create_exclusive_never_overwrites(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    create_exclusive(root, "keep.txt", b"original")
    with pytest.raises(Problem, match="already-exists"):
        create_exclusive(root, "keep.txt", b"replacement")
    assert (root / "keep.txt").read_bytes() == b"original"


def test_create_exclusive_refuses_symlink_parent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "real").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "link").symlink_to(other)
    with pytest.raises(Problem, match="unsafe-path"):
        create_exclusive(root, "link/smuggled.txt", b"x")
    assert not (other / "smuggled.txt").exists()


def test_publish_atomic_replaces_and_cleans_temp(tmp_path):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    first = publish_atomic(root, "result.json", b'{"v":1}')
    assert first.read_bytes() == b'{"v":1}'
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o600
    publish_atomic(root, "result.json", b'{"v":2}')
    assert first.read_bytes() == b'{"v":2}'
    leftovers = [p for p in root.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_publish_atomic_rejects_symlink_destination(tmp_path):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    outside = tmp_path / "victim.txt"
    outside.write_bytes(b"victim")
    (root / "result.json").symlink_to(outside)
    with pytest.raises(Problem, match="unsafe-path"):
        publish_atomic(root, "result.json", b"overwrite")
    assert outside.read_bytes() == b"victim"
    leftovers = [p for p in root.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_publish_atomic_requires_private_root(tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    with pytest.raises(Problem, match="unsafe-path"):
        publish_atomic(root, "result.json", b"x")


def test_fixture_domain_helpers_use_private_primitives(case):
    domain = case.domain()
    assert stat.S_IMODE(os.stat(domain.root).st_mode) == 0o700
    assert domain.marker.read_bytes().find(b"fixture_id") != -1
    project = case.project(domain)
    assert (project / ".ptest.toml").exists()
    assert stat.S_IMODE(os.stat(project / ".ptest.toml").st_mode) == 0o644


def test_ensure_shared_dir_supports_application_support(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    created = ensure_shared_dir(parent, "Application Support")
    assert created == parent / "Application Support"
    assert stat.S_IMODE(os.stat(created).st_mode) == 0o700
    again = ensure_shared_dir(parent, "Application Support")
    assert again == created


def test_read_regular_supports_spaces_brackets_unicode(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test data").mkdir()
    name = "[slug].tést-文件.txt"
    (root / "test data" / name).write_bytes(b"payload")
    assert read_regular(root, f"test data/{name}", 1024) == b"payload"


def test_validate_single_name_structural_boundary():
    for good in ("Application Support", "test data", "[slug].test.ts",
                 "tést-文件.txt", "a b", ".hidden", "a.b-c_d", "a+b=c"):
        assert validate_single_name(good) == good
    for bad in ("", ".", "..", "a/b", "/abs", "a\x00b", "a\nb", "a\x1fb",
                "a\x7fb", "trailing/"):
        with pytest.raises(Problem, match="unsafe-path"):
            validate_single_name(bad)
    for bad in (None, 123, b"bytes", ["x"]):
        with pytest.raises(Problem, match="unsafe-path"):
            validate_single_name(bad)


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


needs_proc_fd = pytest.mark.skipif(
    not os.path.exists("/proc/self/fd"),
    reason="descriptor accounting needs /proc/self/fd",
)


@needs_proc_fd
def test_nested_read_success_keeps_descriptors_stable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "b").mkdir()
    (root / "a" / "b" / "note.txt").write_bytes(b"hello")
    assert read_regular(root, "a/b/note.txt", 1024) == b"hello"
    base = _fd_count()
    for _ in range(200):
        assert read_regular(root, "a/b/note.txt", 1024) == b"hello"
    assert _fd_count() - base <= 2


@needs_proc_fd
def test_nested_failure_keeps_descriptors_stable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "b").mkdir()
    base = _fd_count()
    for _ in range(100):
        with pytest.raises(Problem, match="state-unavailable"):
            read_regular(root, "a/b/absent.txt", 1024)
    for _ in range(100):
        with pytest.raises(Problem, match="state-unavailable"):
            create_exclusive(root, "a/b/c/missing.txt", b"x")
    assert _fd_count() - base <= 2


def test_create_exclusive_missing_parent_is_typed_absence(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(Problem, match="state-unavailable"):
        create_exclusive(root, "nodir/file.txt", b"x")


MINI_MAIN = """\
import json
import os
import sys
from pathlib import Path


def main():
    argv = sys.argv[1:]
    export = None
    fixture = None
    for index, token in enumerate(argv):
        if token == "--result-json" and index + 1 < len(argv):
            export = argv[index + 1]
        if token == "--fixture-domain" and index + 1 < len(argv):
            fixture = argv[index + 1]
    assert export is not None, "mini target requires --result-json"
    payload = {
        "ok": True,
        "argv": argv,
        "fixture_domain": fixture,
        "leaked_control_vars": sorted(
            var for var in os.environ if var.startswith("PTEST_")),
    }
    Path.cwd().joinpath(export).write_text(json.dumps(payload))
    return 0


raise SystemExit(main())
"""


def test_invoke_default_export_roundtrip_with_miniature_target(
        case, tmp_path, monkeypatch):
    """The frozen invoke default export must succeed and round-trip."""
    domain = case.domain()
    project = case.project(domain)
    stub = tmp_path / "mini-target" / "ptest"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("")
    (stub / "__main__.py").write_text(MINI_MAIN)
    monkeypatch.setenv("PTEST_CONFIG", "bogus-override")
    completed = case.invoke(
        domain, project,
        env={"PYTHONPATH": str(stub.parent)}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["fixture_domain"] == str(domain.root)
    leaked = completed.result["leaked_control_vars"]
    for var in CONTROL_VARS:
        assert var not in leaked, var
    assert "--result-json" in completed.result["argv"]
    exported = list(project.glob("ptest-result-*.json"))
    assert len(exported) == 1
