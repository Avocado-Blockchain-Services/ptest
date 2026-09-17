"""Platform-domain and process-identity contracts (Task 2)."""
from __future__ import annotations

import errno
import os
import stat
from types import SimpleNamespace

import psutil
import pytest

from ptest import platform as P
from ptest.contracts import Problem


def _account_home(monkeypatch, home):
    monkeypatch.setattr(P.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(home)))
    monkeypatch.setattr(P, "_filesystem_type", lambda path: "ext4")


def _make_account_home(tmp_path):
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    return home


def test_domain_ignores_home_and_xdg(monkeypatch):
    before = P.domain_paths(None)
    monkeypatch.setenv("HOME", "/unused/tui-home")
    monkeypatch.setenv("XDG_STATE_HOME", "/unused/tui-state")
    assert P.domain_paths(None).root == before.root


def test_normal_domain_resolves_account_paths_without_creating(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)

    result = P.domain_paths(None)

    assert result.root == home / ".local" / "state" / "ptest" / "coordination"
    assert result.machine_config == home / ".config" / "ptest" / "machine.toml"
    assert result.ledger == result.root / "coordinator.sqlite3"
    assert result.marker == result.root / "domain.json"
    assert result.fixture is False
    assert result.domain_id is None
    assert not (home / ".local").exists()
    assert not (home / ".config").exists()


def test_normal_domain_ignores_legacy_siblings_and_preserves_modes(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    local = home / ".local"
    state = local / "state"
    state_ptest = state / "ptest"
    config = home / ".config"
    config_ptest = config / "ptest"
    for path in (local, state, state_ptest, config, config_ptest):
        path.mkdir(mode=0o755)
    legacy_active = state_ptest / "active"
    legacy_active.mkdir(mode=0o755)
    legacy_record = legacy_active / "123.json"
    legacy_record.write_bytes(b"legacy-run")
    os.chmod(legacy_record, 0o644)
    legacy_config = config_ptest / "config.toml"
    legacy_config.write_bytes(b"legacy = true\n")
    os.chmod(legacy_config, 0o644)
    before = {
        path: (stat.S_IMODE(os.stat(path).st_mode), path.read_bytes())
        for path in (legacy_record, legacy_config)
    }
    modes = {path: stat.S_IMODE(os.stat(path).st_mode)
             for path in (local, state, state_ptest, config, config_ptest)}
    _account_home(monkeypatch, home)

    result = P.domain_paths(None)

    assert result.root == state_ptest / "coordination"
    assert not result.root.exists()
    assert {path: (stat.S_IMODE(os.stat(path).st_mode), path.read_bytes())
            for path in (legacy_record, legacy_config)} == before
    assert {path: stat.S_IMODE(os.stat(path).st_mode)
            for path in modes} == modes


def test_normal_domain_accepts_existing_private_coordination(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    root = home / ".local" / "state" / "ptest" / "coordination"
    root.parent.mkdir(parents=True, mode=0o755)
    root.mkdir(mode=0o700)
    config_parent = home / ".config" / "ptest"
    config_parent.mkdir(parents=True, mode=0o755)
    machine = config_parent / "machine.toml"
    machine.write_bytes(b"version = 1\n")
    os.chmod(machine, 0o600)
    _account_home(monkeypatch, home)

    assert P.domain_paths(None).root == root


def test_macos_domain_uses_account_application_support(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P, "_filesystem_type", lambda path: "apfs")

    result = P.domain_paths(None)

    parent = home / "Library" / "Application Support" / "ptest"
    assert result.root == parent / "coordination"
    assert result.machine_config == parent / "machine.toml"
    assert not (home / "Library").exists()


@pytest.mark.parametrize("mode", [0o755, 0o770])
def test_normal_domain_rejects_wrong_coordination_mode(monkeypatch, tmp_path, mode):
    home = _make_account_home(tmp_path)
    root = home / ".local" / "state" / "ptest" / "coordination"
    root.parent.mkdir(parents=True, mode=0o755)
    root.mkdir(mode=mode)
    _account_home(monkeypatch, home)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


def test_normal_domain_rejects_existing_machine_file_wrong_mode(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    parent = home / ".config" / "ptest"
    parent.mkdir(parents=True, mode=0o755)
    machine = parent / "machine.toml"
    machine.write_bytes(b"version = 1\n")
    os.chmod(machine, 0o644)
    _account_home(monkeypatch, home)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


def test_normal_domain_rejects_symlink_component(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (home / ".local").symlink_to(outside, target_is_directory=True)
    _account_home(monkeypatch, home)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


def test_normal_domain_rejects_machine_config_symlink_component(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    outside = tmp_path / "outside-config"
    outside.mkdir(mode=0o700)
    (home / ".config").symlink_to(outside, target_is_directory=True)
    _account_home(monkeypatch, home)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


def test_normal_domain_rejects_group_writable_parent(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    local = home / ".local"
    local.mkdir(mode=0o770)
    os.chmod(local, 0o770)
    _account_home(monkeypatch, home)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


def test_normal_domain_rejects_foreign_account_home(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)
    real_uid = os.getuid()
    monkeypatch.setattr(P.os, "getuid", lambda: real_uid + 1)
    monkeypatch.setattr(P.os, "geteuid", lambda: real_uid + 1)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(None)


@pytest.mark.parametrize("filesystem", ["nfs", "overlay", None])
def test_normal_domain_rejects_unknown_or_network_filesystem(
        monkeypatch, tmp_path, filesystem):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)
    monkeypatch.setattr(P, "_filesystem_type", lambda path: filesystem)

    with pytest.raises(Problem, match="unsupported-platform"):
        P.domain_paths(None)


def test_normal_domain_rejects_mismatched_real_and_effective_uid(
        monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)
    monkeypatch.setattr(P.os, "geteuid", lambda: os.getuid() + 1)

    with pytest.raises(Problem, match="unsupported-platform"):
        P.domain_paths(None)


def test_normal_domain_rejects_unsupported_os(monkeypatch, tmp_path):
    home = _make_account_home(tmp_path)
    _account_home(monkeypatch, home)
    monkeypatch.setattr(P.sys, "platform", "win32")

    with pytest.raises(Problem, match="unsupported-platform"):
        P.domain_paths(None)


def test_fixture_domain_validates_marker_and_is_read_only(case):
    fixture = case.domain(slots=2, jobs=1)
    before = fixture.marker.read_bytes()
    marker_mode = stat.S_IMODE(os.stat(fixture.marker).st_mode)

    result = P.domain_paths(fixture.root)

    assert result == fixture
    assert fixture.marker.read_bytes() == before
    assert stat.S_IMODE(os.stat(fixture.marker).st_mode) == marker_mode


def test_fixture_domain_must_already_exist(monkeypatch, tmp_path):
    missing = tmp_path / "not-created"
    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(missing)
    assert not missing.exists()


def test_fixture_domain_rejects_root_symlink(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(link)


def test_fixture_domain_rejects_wrong_root_mode(case):
    fixture = case.domain()
    os.chmod(fixture.root, 0o755)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(fixture.root)


def test_fixture_domain_rejects_hardlinked_marker(case, tmp_path):
    fixture = case.domain()
    alias = tmp_path / "marker-alias"
    os.link(fixture.marker, alias)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(fixture.root)


def test_fixture_domain_rejects_stale_device_or_inode(case):
    fixture = case.domain()
    original = fixture.marker.read_text()
    fixture.marker.write_text(original.replace(
        f"directory_inode = {os.stat(fixture.root).st_ino}",
        "directory_inode = 1"))

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(fixture.root)


def test_fixture_domain_rejects_unknown_or_invalid_marker_fields(case):
    fixture = case.domain()
    original = fixture.marker.read_text()
    fixture.marker.write_text(original + "unexpected = true\n")

    with pytest.raises(Problem, match="invalid-config"):
        P.domain_paths(fixture.root)


def test_fixture_domain_rejects_oversized_marker(case):
    fixture = case.domain()
    fixture.marker.write_text(
        fixture.marker.read_text()
        + 'padding = "' + ("x" * 65536) + '"\n')

    with pytest.raises(Problem, match="invalid-config"):
        P.domain_paths(fixture.root)


def test_fixture_domain_rejects_marker_symlink(case, tmp_path):
    fixture = case.domain()
    outside = tmp_path / "outside-marker"
    outside.write_bytes(fixture.marker.read_bytes())
    os.chmod(outside, 0o600)
    fixture.marker.unlink()
    fixture.marker.symlink_to(outside)

    with pytest.raises(Problem, match="unsafe-path"):
        P.domain_paths(fixture.root)


def test_process_identity_observes_birth_uid_and_group():
    identity = P.process_identity(os.getpid())

    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.birth > 0
    assert identity.uid == os.getuid()
    assert identity.pgid == os.getpgrp()


@pytest.mark.parametrize("pid", [0, -1, True, "self"])
def test_process_identity_rejects_invalid_pid(pid):
    assert P.process_identity(pid) is None


def test_process_identity_returns_none_when_inaccessible(monkeypatch):
    def inaccessible(pid):
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(P.psutil, "Process", inaccessible)

    assert P.process_identity(123) is None


def test_process_identity_rejects_mismatched_real_effective_uid(monkeypatch):
    class FakeProcess:
        def create_time(self):
            return 10.0

        def uids(self):
            return SimpleNamespace(real=1000, effective=1001)

    monkeypatch.setattr(P.psutil, "Process", lambda pid: FakeProcess())

    assert P.process_identity(123) is None


def test_process_identity_never_reads_process_argv_or_environment(monkeypatch):
    class FakeProcess:
        def create_time(self):
            return 10.0

        def uids(self):
            return SimpleNamespace(real=os.getuid(), effective=os.getuid())

        def __getattr__(self, name):
            if name in {"cmdline", "environ"}:
                raise AssertionError(f"forbidden process field: {name}")
            raise AttributeError(name)

    monkeypatch.setattr(P.psutil, "Process", lambda pid: FakeProcess())
    monkeypatch.setattr(P.os, "getpgid", lambda pid: 456)

    identity = P.process_identity(123)

    assert identity is not None
    assert identity.pgid == 456


def test_process_identity_returns_none_for_reused_or_gone_pid(monkeypatch):
    def gone(pid):
        raise psutil.NoSuchProcess(pid=pid)

    monkeypatch.setattr(P.psutil, "Process", gone)

    assert P.process_identity(123) is None


def test_process_identity_rejects_pid_reuse_during_observation(monkeypatch):
    class ReusedProcess:
        def __init__(self):
            self._births = iter((10.0, 11.0))

        def create_time(self):
            return next(self._births)

        def uids(self):
            return SimpleNamespace(real=os.getuid(), effective=os.getuid())

    monkeypatch.setattr(P.psutil, "Process", lambda pid: ReusedProcess())
    monkeypatch.setattr(P.os, "getpgid", lambda pid: 456)

    assert P.process_identity(123) is None


def test_probe_group_uses_signal_zero_without_signalling(monkeypatch):
    calls = []

    def probe(pgid, signal_number):
        calls.append((pgid, signal_number))

    monkeypatch.setattr(P.os, "killpg", probe)
    monkeypatch.setattr(P.time, "monotonic", lambda: 12.5)

    observation = P.probe_group(456)

    assert observation.exists is True
    assert observation.permission is True
    assert observation.checked_at == 12.5
    assert calls == [(456, 0)]


def test_probe_group_only_reports_absent_for_esrch(monkeypatch):
    def absent(pgid, signal_number):
        raise OSError(errno.ESRCH, "gone")

    monkeypatch.setattr(P.os, "killpg", absent)

    observation = P.probe_group(456)

    assert observation.exists is False
    assert observation.permission is True


def test_probe_group_fails_closed_on_permission_error(monkeypatch):
    def denied(pgid, signal_number):
        raise PermissionError(errno.EPERM, "denied")

    monkeypatch.setattr(P.os, "killpg", denied)

    observation = P.probe_group(456)

    assert observation.exists is None
    assert observation.permission is False


def test_probe_group_rejects_invalid_group_without_syscall(monkeypatch):
    monkeypatch.setattr(P.os, "killpg", lambda *_: pytest.fail("syscall"))

    observation = P.probe_group(0)

    assert observation.exists is None
    assert observation.permission is False


def test_boot_identity_is_stable_and_does_not_use_user_environment():
    first = P.boot_identity()
    second = P.boot_identity()

    assert isinstance(first, str)
    assert first
    assert first == second


def test_boot_identity_changes_when_kernel_observation_changes(monkeypatch):
    monkeypatch.setattr(P.sys, "platform", "linux")
    observations = iter(("boot-a", "boot-b"))
    monkeypatch.setattr(P, "_read_linux_boot_id", lambda: next(observations))

    assert P.boot_identity() == "boot-a"
    assert P.boot_identity() == "boot-b"


def test_boot_identity_fails_closed_when_observation_is_inaccessible(monkeypatch):
    monkeypatch.setattr(P.sys, "platform", "linux")
    monkeypatch.setattr(P, "_read_linux_boot_id", lambda: None)

    with pytest.raises(Problem, match="state-unavailable"):
        P.boot_identity()
