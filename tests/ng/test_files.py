"""Safe file primitive behavior and abuse tests (Task0 owned)."""
from __future__ import annotations

import os
import select
import stat
import subprocess
import time

import pytest

from ptest.contracts import Problem
from support import BOUND_OUTPUT_BYTES, CONTROL_VARS, _read_export_file
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
MINI_BIG_MAIN = """
import json
import sys
from pathlib import Path
def main():
    argv = sys.argv[1:]
    export = None
    for index, token in enumerate(argv):
        if token == "--result-json" and index + 1 < len(argv):
            export = argv[index + 1]
    assert export is not None, "mini target requires --result-json"
    sys.stdout.buffer.write(b"O" * 1572864)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(b"E" * 1572864)
    sys.stderr.buffer.flush()
    Path.cwd().joinpath(export).write_text(json.dumps({"ok": True}))
    return 0
raise SystemExit(main())
"""
MINI_HANG_MAIN = """
import subprocess
import sys
import time
def main():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(60)
    return 0
raise SystemExit(main())
"""
MINI_EXIT_RETAIN_MAIN = """
import json
import subprocess
import sys
from pathlib import Path
def main():
    argv = sys.argv[1:]
    export = None
    for index, token in enumerate(argv):
        if token == "--result-json" and index + 1 < len(argv):
            export = argv[index + 1]
    assert export is not None, "mini target requires --result-json"
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path.cwd().joinpath(export).write_text(json.dumps({"ok": True}))
    return 0
raise SystemExit(main())
"""
def _write_mini_target(tmp_path, body):
    stub = tmp_path / "mini-target" / "ptest"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("")
    (stub / "__main__.py").write_text(body)
    return str(stub.parent)
def test_invoke_missing_timeout_fails_before_launch(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    with pytest.raises(TypeError):
        case.invoke(domain, project)
    assert list(project.glob("ptest-result-*.json")) == []
@pytest.mark.parametrize("bad", [None, 0, -5, 0.0, float("nan"), float("inf"), True, False, "10", (10,)])
def test_invoke_invalid_timeout_fails_before_launch(case, tmp_path, bad):
    domain = case.domain()
    project = case.project(domain)
    with pytest.raises(ValueError, match="timeout"):
        case.invoke(domain, project, timeout=bad)
    assert list(project.glob("ptest-result-*.json")) == []
def test_invoke_output_past_cap_is_typed_failure(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_BIG_MAIN)
    with pytest.raises(ValueError, match="bound"):
        case.invoke(domain, project, env={"PYTHONPATH": pythonpath}, timeout=20.0)
def test_invoke_watchdog_bounded_with_retained_pipe(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_HANG_MAIN)
    sentinel = subprocess.Popen(["sleep", "60"])
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="timeout"):
            case.invoke(domain, project, env={"PYTHONPATH": pythonpath}, timeout=1.0)
        elapsed = time.monotonic() - start
    finally:
        sentinel_alive = sentinel.poll() is None
        sentinel.terminate()
        sentinel.wait(timeout=10)
    assert sentinel_alive, "watchdog cleanup must not kill unrelated jobs"
    assert 0.5 <= elapsed < 6.0, elapsed
def test_invoke_returns_when_child_exits_despite_retained_pipe(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_EXIT_RETAIN_MAIN)
    start = time.monotonic()
    completed = case.invoke(domain, project, env={"PYTHONPATH": pythonpath}, timeout=15.0)
    elapsed = time.monotonic() - start
    assert completed.code == 0
    assert completed.result == {"ok": True}
    assert elapsed < 5.0, elapsed
def test_invoke_parses_caller_result_json_with_existing_parent(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_MAIN)
    sub = project / "sub"
    sub.mkdir()
    completed = case.invoke(domain, project, "--result-json", "sub/result.json", env={"PYTHONPATH": pythonpath}, timeout=20.0)
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert (sub / "result.json").is_file()
    assert list(project.glob("ptest-result-*.json")) == []
MINI_PREFIX_MAIN = """
import json
import sys
from pathlib import Path
WRAPPER_VALUE_OPTS = (
    "--fixture-domain", "--base", "--workers", "--queue-timeout",
    "--result-json",
)
WRAPPER_BOOL_OPTS = (
    "--changed", "--full", "--no-setup", "--fresh", "--local", "--shadow",
)
def main():
    argv = sys.argv[1:]
    wrapper = {}
    index = 0
    native = []
    while index < len(argv):
        token = argv[index]
        if token == "--":
            native = argv[index + 1:]
            break
        if token in WRAPPER_BOOL_OPTS:
            wrapper[token] = True
            index += 1
            continue
        if token not in WRAPPER_VALUE_OPTS:
            native = argv[index:]
            break
        if index + 1 >= len(argv):
            sys.stderr.write("missing wrapper value\\n")
            return 2
        wrapper[token] = argv[index + 1]
        index += 2
    export = wrapper.get("--result-json")
    if export is None:
        sys.stderr.write("mini target requires wrapper --result-json\\n")
        return 2
    Path.cwd().joinpath(export).write_text(json.dumps(
        {"ok": True, "wrapper": wrapper, "native": native}))
    return 0
raise SystemExit(main())
"""
def test_invoke_generated_export_precedes_native_tail(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    completed = case.invoke(
        domain, project, "tests/test_x.py",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["native"] == ["tests/test_x.py"]
    assert completed.result["wrapper"]["--result-json"].startswith(
        "ptest-result-")
    assert len(list(project.glob("ptest-result-*.json"))) == 1
def test_invoke_native_option_looking_tokens_are_not_overrides(
        case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    completed = case.invoke(
        domain, project, "-k", "--result-json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["native"] == ["-k", "--result-json"]
    assert len(list(project.glob("ptest-result-*.json"))) == 1
def test_invoke_delimiter_hides_native_result_token(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    completed = case.invoke(
        domain, project, "--", "--result-json", "evil.json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["native"] == ["--result-json", "evil.json"]
    assert not (project / "evil.json").exists()
    assert len(list(project.glob("ptest-result-*.json"))) == 1
def test_invoke_leading_explicit_result_path_read_back(case, tmp_path):
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    sub = project / "sub"
    sub.mkdir()
    completed = case.invoke(
        domain, project, "--result-json", "sub/result.json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["native"] == []
    assert (sub / "result.json").is_file()
    assert list(project.glob("ptest-result-*.json")) == []
def _stream_main(stream, size):
    """Test-only miniature target writing size bytes to one stream."""
    assert stream in ("stdout", "stderr")
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    byte = "O" if stream == "stdout" else "E"
    return (
        "import json\nimport sys\nfrom pathlib import Path\n"
        "def main():\n"
        "    argv = sys.argv[1:]\n"
        "    export = None\n"
        "    for index, token in enumerate(argv):\n"
        "        if token == \"--result-json\" and index + 1 < len(argv):\n"
        "            export = argv[index + 1]\n"
        "    assert export is not None, \"mini target requires --result-json\"\n"
        f"    {target}.write(b{byte!r} * {int(size)})\n"
        f"    {target}.flush()\n"
        "    Path.cwd().joinpath(export).write_text(json.dumps({\"ok\": True}))\n"
        "    return 0\n"
        "raise SystemExit(main())\n"
    )
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invoke_stream_exact_bound_succeeds(case, tmp_path, stream):
    """Exactly BOUND_OUTPUT_BYTES on either stream must succeed intact."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(
        tmp_path, _stream_main(stream, BOUND_OUTPUT_BYTES))
    completed = case.invoke(
        domain, project, env={"PYTHONPATH": pythonpath}, timeout=20.0)
    assert completed.code == 0
    assert len(getattr(completed, stream)) == BOUND_OUTPUT_BYTES
    assert completed.result == {"ok": True}
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invoke_stream_past_cap_reports_named_stream(case, tmp_path, stream):
    """BOUND+1 on either stream must name the offending stream."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(
        tmp_path, _stream_main(stream, BOUND_OUTPUT_BYTES + 1))
    with pytest.raises(ValueError, match=f"invoke {stream}.*bound"):
        case.invoke(
            domain, project, env={"PYTHONPATH": pythonpath}, timeout=20.0)
def test_export_read_malformed_scalar_list_are_absent(tmp_path):
    """Malformed/scalar/list exports are not dicts, so read as absent."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "bad.json").write_bytes(b"{not json")
    assert _read_export_file(root, "bad.json") is None
    (root / "scalar.json").write_bytes(b"42")
    assert _read_export_file(root, "scalar.json") is None
    (root / "lst.json").write_bytes(b"[1, 2]")
    assert _read_export_file(root, "lst.json") is None
    assert _read_export_file(root, "absent.json") is None
def test_export_read_oversize_raises_bounded_failure(tmp_path):
    """An oversize export is incomplete evidence, not a missing file."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "big.json").write_bytes(b"x" * (BOUND_OUTPUT_BYTES + 5))
    with pytest.raises(ValueError, match="bound"):
        _read_export_file(root, "big.json")
def test_export_read_rejects_absolute_path_without_opening(tmp_path):
    """Absolute export names are rejected; never opened as host paths."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"ok": true}')
    assert _read_export_file(root, str(outside)) is None
def test_export_read_absolute_fifo_never_blocks(tmp_path):
    """An absolute FIFO export name must not wedge the reader."""
    root = tmp_path / "project"
    root.mkdir()
    fifo = tmp_path / "stuck"
    os.mkfifo(fifo)
    start = time.monotonic()
    assert _read_export_file(root, str(fifo)) is None
    assert time.monotonic() - start < 5.0
def test_export_read_rejects_symlink_and_fifo(tmp_path):
    """Symlink and FIFO exports inside the root read as absent."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"ok": true}')
    (root / "link.json").symlink_to(outside)
    assert _read_export_file(root, "link.json") is None
    os.mkfifo(root / "pipe.json")
    start = time.monotonic()
    assert _read_export_file(root, "pipe.json") is None
    assert time.monotonic() - start < 5.0
def test_invoke_shadow_prefix_result_json_read_back(case, tmp_path):
    """A leading --shadow must not duplicate the caller export."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    sub = project / "sub"
    sub.mkdir()
    completed = case.invoke(
        domain, project, "--shadow", "--result-json", "sub/result.json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["wrapper"]["--result-json"] == "sub/result.json"
    assert completed.result["wrapper"].get("--shadow") is True
    assert completed.result["native"] == []
    assert (sub / "result.json").is_file()
    assert list(project.glob("ptest-result-*.json")) == []
def test_invoke_workers_prefix_result_json_read_back(case, tmp_path):
    """A leading --workers value must not duplicate the caller export."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    sub = project / "sub"
    sub.mkdir()
    completed = case.invoke(
        domain, project, "--workers", "1", "--result-json", "sub/result.json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["wrapper"]["--result-json"] == "sub/result.json"
    assert completed.result["wrapper"]["--workers"] == "1"
    assert completed.result["native"] == []
    assert (sub / "result.json").is_file()
    assert list(project.glob("ptest-result-*.json")) == []
def test_invoke_leading_wrapper_opts_keep_native_verbatim(case, tmp_path):
    """Wrapper prefix is consumed; the native tail stays literal."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_PREFIX_MAIN)
    completed = case.invoke(
        domain, project, "--shadow", "--workers", "1", "-k", "--result-json",
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert completed.result is not None
    assert completed.result["ok"] is True
    assert completed.result["wrapper"].get("--shadow") is True
    assert completed.result["wrapper"]["--workers"] == "1"
    assert completed.result["native"] == ["-k", "--result-json"]
    assert len(list(project.glob("ptest-result-*.json"))) == 1
def test_invoke_purges_inherited_control_vars(case, tmp_path, monkeypatch):
    """Inherited control vars must not leak into the fixture child."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_MAIN)
    monkeypatch.setenv("PTEST_RUN_ID", "inherited-contamination")
    completed = case.invoke(
        domain, project,
        env={"PYTHONPATH": pythonpath}, timeout=20.0,
    )
    assert completed.code == 0
    assert "PTEST_RUN_ID" not in completed.result["leaked_control_vars"]
def test_invoke_explicit_env_override_passes_through(case, tmp_path):
    """Explicit env= control vars reach the child for override tests."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_MAIN)
    completed = case.invoke(
        domain, project,
        env={"PYTHONPATH": pythonpath, "PTEST_RUN_ID": "explicit-override"},
        timeout=20.0,
    )
    assert completed.code == 0
    assert "PTEST_RUN_ID" in completed.result["leaked_control_vars"]
def test_invoke_stream_select_failure_raises_fixture_error(
        case, tmp_path, monkeypatch):
    """A select/read failure must not look like a normal empty result."""
    domain = case.domain()
    project = case.project(domain)
    pythonpath = _write_mini_target(tmp_path, MINI_MAIN)
    def _boom(*args, **kwargs):
        raise OSError("injected select failure")
    monkeypatch.setattr(select, "select", _boom)
    with pytest.raises(OSError, match="stream"):
        case.invoke(
            domain, project, env={"PYTHONPATH": pythonpath}, timeout=20.0)
