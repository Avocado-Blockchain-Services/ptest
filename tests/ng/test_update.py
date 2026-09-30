"""`ptest update` and the startup update check: abuse twins plus positives.

Tests use an injected fake Transport and temporary install roots; the real
network and the real install are never touched. The session guard in
conftest denies real network access for any test that forgets a fake.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import update as update_api
from ptest.cli import main

RUNNING = C.PTEST_VERSION
NEWER = "9.9.9"
OLDER = "0.0.1"
SUFFIX = "linux-x86_64"


def _asset(version: str) -> str:
    return f"ptest-{version}-{SUFFIX}.tar.gz"


def _release_base(version: str) -> str:
    return (f"https://github.com/Avocado-Blockchain-Services/ptest"
            f"/releases/download/v{version}")


class FakeTransport:
    """Serve bytes from a dict keyed by URL; record every call."""

    def __init__(self, files: dict | None = None, location: str | None = None):
        self.files = dict(files or {})
        self.location = location
        self.latest_calls: list[float] = []
        self.download_calls: list[str] = []

    def to_transport(self) -> update_api.Transport:
        return update_api.Transport(
            latest_location=self._latest_location,
            download=self._download,
        )

    def _latest_location(self, timeout_s: float) -> str:
        self.latest_calls.append(timeout_s)
        if self.location is None:
            raise C.Problem(code="update-unavailable",
                            message="could not reach the ptest releases on GitHub",
                            phase="update", retryable=True)
        return self.location

    def _download(self, url: str, sink, max_bytes: int, timeout_s: float) -> None:
        self.download_calls.append(url)
        if url not in self.files:
            raise C.Problem(code="update-failed",
                            message=f"no ptest {NEWER} release for {SUFFIX}",
                            phase="update")
        body = self.files[url]
        if len(body) > max_bytes:
            raise C.Problem(code="update-failed",
                            message="download exceeds size bound",
                            phase="update")
        sink.write(body)


def _tag_url(version: str) -> str:
    return (f"https://github.com/Avocado-Blockchain-Services/ptest"
            f"/releases/tag/v{version}")


def _make_bundle_tar(version: str, *, install_sh: str | None = None,
                     extra_members: list | None = None) -> bytes:
    """Build a well-formed bundle tarball in memory."""
    if install_sh is None:
        install_sh = "#!/bin/sh\nexit 0\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        top = tarfile.TarInfo(f"ptest-{version}")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        payload = install_sh.encode()
        info = tarfile.TarInfo(f"ptest-{version}/install.sh")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
        for member in (extra_members or []):
            tar.addfile(member[0], member[1])
    return buf.getvalue()


def _sha_line(body: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(body).hexdigest()}  {name}\n".encode()


def _fake_root(tmp_path: Path, version: str = RUNNING) -> update_api.Layout:
    """A valid install layout rooted under tmp_path."""
    root = tmp_path / "install-root"
    bundle = root / ".ptest-bundles" / f"{version}-test"
    bindir = bundle / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "ptest").write_text("#!/bin/sh\n", encoding="utf-8")
    marker = {"version": 1, "bundle_id": bundle.name,
              "ptest_version": version}
    (bundle / "complete.json").write_text(json.dumps(marker),
                                          encoding="utf-8")
    link = root / "ptest"
    link.symlink_to(bundle / "venv" / "bin" / "ptest")
    return update_api.Layout(root=root, bundle_id=bundle.name)


def _fake_install_sh(recorder: Path, dest_var: Path, new_version: str,
                     *, fail: bool = False, switch: bool = True) -> str:
    """An install.sh body that records argv/env and optionally switches."""
    return (
        "#!/bin/sh\n"
        "{\n"
        '  echo "argv:$*";\n'
        '  echo "PTEST_PYTHON=${PTEST_PYTHON-unset}";\n'
        '  echo "PTEST_NO_UPDATE_CHECK=${PTEST_NO_UPDATE_CHECK-unset}";\n'
        '  echo "PTEST_INSTALL_FAULT=${PTEST_INSTALL_FAULT-set}";\n'
        "} > \"$RECORDER_PATH\"\n".replace("$RECORDER_PATH", str(recorder))
        + (f"exit 1\n" if fail else
           f"DEST=\"$2\"; V=\"{new_version}\";\n"
           f"mkdir -p \"$DEST/.ptest-bundles/$V-test/venv/bin\"\n"
           + (f"printf '{{\"version\":1,\"bundle_id\":\"{new_version}-test\","
               f"\"ptest_version\":\"{new_version}\"}}' "
               f"> \"$DEST/.ptest-bundles/$V-test/complete.json\"\n"
               f": > \"$DEST/.ptest-bundles/$V-test/venv/bin/ptest\"\n"
               f"ln -sfn \"$DEST/.ptest-bundles/$V-test/venv/bin/ptest\" \"$DEST/ptest\"\n"
               if switch else "")
           + "exit 0\n")
    )


def _uv_shim(bin_dir: Path, python_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "uv"
    shim.write_text(f"#!/bin/sh\necho {python_path}\n", encoding="utf-8")
    shim.chmod(0o755)


def _update_files(version: str, tarball: bytes) -> dict:
    base = _release_base(version)
    name = _asset(version)
    return {f"{base}/{name}": tarball,
            f"{base}/{name}.sha256": _sha_line(tarball, name)}


def _run_update_cli(*args: str, domain_root: Path) -> int:
    return main(("--fixture-domain", str(domain_root), "update", *args))


# -- version parsing -----------------------------------------------------------

def test_valid_version_accepts_plain_release_numbers():
    assert update_api.valid_version("0.3.7") == "0.3.7"
    assert update_api.valid_version("10.20.30") == "10.20.30"


@pytest.mark.parametrize("hostile", [
    "1.2", "1.2.3.4", "../1.2.3", "1.2.3/x", "v1.2.3", "1.2.3\n",
    " 1.2.3", "1.2.3 ", "0.3.7;rm", "１.２.３", "x" * 33, "", "v",
    "1.2.3" + "9" * 28,
])
def test_hostile_version_rejected_before_network(hostile):
    assert update_api.valid_version(hostile) is None
    assert update_api.valid_version(123) is None  # type: ignore[arg-type]
    assert update_api.valid_version(None) is None


def test_is_newer_compares_integer_tuples():
    assert update_api.is_newer("0.3.8", "0.3.7")
    assert update_api.is_newer("0.10.0", "0.9.9")
    assert not update_api.is_newer("0.3.7", "0.3.7")
    assert not update_api.is_newer("0.3.6", "0.3.7")


def test_platform_suffix_matches_get_sh_table(monkeypatch):
    import os as _os

    class _Uname:
        def __init__(self, sysname, machine):
            self.sysname = sysname
            self.machine = machine

    monkeypatch.setattr(_os, "uname",
                        lambda: _Uname("Linux", "x86_64"))
    assert update_api.platform_suffix() == "linux-x86_64"
    monkeypatch.setattr(_os, "uname",
                        lambda: _Uname("Darwin", "arm64"))
    assert update_api.platform_suffix() == "macos-arm64"
    monkeypatch.setattr(_os, "uname",
                        lambda: _Uname("Linux", "aarch64"))
    assert update_api.platform_suffix() == "linux-aarch64"
    monkeypatch.setattr(_os, "uname",
                        lambda: _Uname("Windows", "x86_64"))
    with pytest.raises(C.Problem) as excinfo:
        update_api.platform_suffix()
    assert excinfo.value.code == "update-failed"


# -- latest resolution ----------------------------------------------------------

def test_latest_location_must_be_exact_tag_url():
    good = update_api.Transport(
        latest_location=lambda timeout: _tag_url("0.3.8"),
        download=lambda *a: (_ for _ in ()).throw(AssertionError()),
    )
    assert update_api.resolve_latest(good, timeout_s=5.0) == "0.3.8"
    for bad in (
        "https://github.com/Avocado-Blockchain-Services/other/releases/tag/v0.3.8",
        "https://example.com/ptest/releases/tag/v0.3.8",
        "http://github.com/Avocado-Blockchain-Services/ptest/releases/tag/v0.3.8",
        "https://github.com/Avocado-Blockchain-Services/ptest/releases/tag/0.3.8",
        "https://github.com/Avocado-Blockchain-Services/ptest/releases/tag/v../0.3.8",
        "https://github.com/Avocado-Blockchain-Services/ptest/releases/tag/v1.2",
        "not a url",
        "",
    ):
        transport = update_api.Transport(
            latest_location=lambda timeout, location=bad: location,
            download=lambda *a: (_ for _ in ()).throw(AssertionError()),
        )
        with pytest.raises(C.Problem) as excinfo:
            update_api.resolve_latest(transport, timeout_s=5.0)
        assert excinfo.value.code == "update-unavailable"


def test_redirect_outside_allowlist_refused():
    assert update_api._url_allowed("https://github.com/x") is True
    assert update_api._url_allowed(
        "https://objects.githubusercontent.com/x") is True
    assert update_api._url_allowed(
        "https://release-assets.githubusercontent.com/x") is True
    assert update_api._url_allowed("http://github.com/x") is False
    assert update_api._url_allowed("https://example.com/x") is False
    assert update_api._url_allowed("https://user@github.com/x") is False
    assert update_api._url_allowed("https://github.com:8443/x") is False
    assert update_api._url_allowed("ftp://github.com/x") is False


def test_env_cannot_redirect_download_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("PTEST_BASE_URL", "https://example.com/evil")
    monkeypatch.setenv("PTEST_UPDATE_URL", "https://example.com/evil")
    layout = _fake_root(tmp_path)
    tarball = _make_bundle_tar(NEWER)
    fake = FakeTransport(_update_files(NEWER, tarball),
                         location=_tag_url(NEWER))
    transport = fake.to_transport()
    before = list(fake.download_calls)
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested="0.0.0", check_only=False,
                              transport=transport, layout=layout)
    assert excinfo.value.code == "update-failed"
    assert fake.download_calls[len(before):] != []
    for url in fake.download_calls[len(before):]:
        assert url.startswith("https://github.com/")


def test_download_over_cap_fails_and_cleans_temp(tmp_path):
    target = tmp_path / "out.bin"
    with pytest.raises(C.Problem):
        update_api._bounded_copy(io.BytesIO(b"x" * 64), target, max_bytes=8)
    assert not target.exists()


# -- checksum / extraction / install --------------------------------------------

def _hostile_member(name: str, *, linkto: str = "") -> tuple:
    info = tarfile.TarInfo(name)
    if linkto:
        info.type = tarfile.SYMTYPE
        info.linkname = linkto
    else:
        info.type = tarfile.REGTYPE
        info.size = 1
    return (info, io.BytesIO(b"x"))


def _run_install(version: str, layout: update_api.Layout,
                 fake: FakeTransport, tmp_path: Path,
                 monkeypatch, **kwargs):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return update_api.run_update(requested=version, check_only=False,
                                 transport=fake.to_transport(),
                                 layout=layout, **kwargs)


def _install_success_setup(tmp_path, monkeypatch, version=NEWER,
                           *, fail=False, switch=True):
    recorder = tmp_path / "recorder.txt"
    dest_probe = tmp_path / "probe"
    body = _fake_install_sh(recorder, dest_probe, version,
                            fail=fail, switch=switch)
    tarball = _make_bundle_tar(version, install_sh=body)
    fake = FakeTransport(_update_files(version, tarball),
                         location=_tag_url(version))
    layout = _fake_root(tmp_path)
    python = tmp_path / "fake-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    bindir = tmp_path / "bin"
    _uv_shim(bindir, python)
    monkeypatch.setenv("PATH",
                       str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return layout, fake, recorder


def test_checksum_mismatch_never_runs_installer_and_keeps_launcher(
        tmp_path, monkeypatch):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch)
    base = _release_base(NEWER)
    name = _asset(NEWER)
    fake.files[f"{base}/{name}.sha256"] = b"0" * 64 + f"  {name}\n".encode()
    before = (layout.root / "ptest").readlink()
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"
    assert "checksum mismatch" in excinfo.value.message
    assert (layout.root / "ptest").readlink() == before
    assert not (layout.root / ".ptest-bundles" / f"{NEWER}-test").exists()
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.parametrize("member_name,linkto", [
    ("/absolute/path", ""),
    ("ptest-9.9.9/../../evil", ""),
    ("ptest-9.9.9/link", "/etc/passwd"),
    ("other-1.2.3/file", ""),
])
def test_unsafe_archive_member_rejected(tmp_path, monkeypatch,
                                       member_name, linkto):
    layout, _, _ = _install_success_setup(tmp_path, monkeypatch)
    if linkto:
        member = _hostile_member(member_name, linkto=linkto)
    else:
        member = _hostile_member(member_name)
    tarball = _make_bundle_tar(NEWER, extra_members=[member])
    fake = FakeTransport(_update_files(NEWER, tarball),
                         location=_tag_url(NEWER))
    before = (layout.root / "ptest").readlink()
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"
    assert (layout.root / "ptest").readlink() == before
    assert list((tmp_path / "tmp").iterdir()) == []


def test_hardlink_and_device_members_rejected(tmp_path, monkeypatch):
    layout, _, _ = _install_success_setup(tmp_path, monkeypatch)
    hard = tarfile.TarInfo(f"ptest-{NEWER}/hard")
    hard.type = tarfile.LNKTYPE
    hard.linkname = f"ptest-{NEWER}/install.sh"
    dev = tarfile.TarInfo(f"ptest-{NEWER}/dev")
    dev.type = tarfile.CHRTYPE
    tarball = _make_bundle_tar(NEWER, extra_members=[(hard, io.BytesIO()),
                                                    (dev, io.BytesIO())])
    fake = FakeTransport(_update_files(NEWER, tarball),
                         location=_tag_url(NEWER))
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_failed_install_keeps_old_bundle_and_launcher(tmp_path, monkeypatch):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch, fail=True)
    old_bundle = layout.root / ".ptest-bundles" / layout.bundle_id
    before = (layout.root / "ptest").readlink()
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"
    assert old_bundle.is_dir()
    assert (layout.root / "ptest").readlink() == before
    assert list((tmp_path / "tmp").iterdir()) == []


def test_post_verify_rejects_unswitched_launcher(tmp_path, monkeypatch):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch,
                                             switch=False)
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"
    assert "did not switch" in excinfo.value.message
    assert list((tmp_path / "tmp").iterdir()) == []


def test_success_installs_side_by_side_and_switches(tmp_path, monkeypatch, capsys):
    layout, fake, recorder = _install_success_setup(tmp_path, monkeypatch)
    old_bundle = layout.root / ".ptest-bundles" / layout.bundle_id
    result = _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert result.action == "updated"
    assert result.target_version == NEWER
    assert result.running_version == RUNNING
    # Old bundle still exists; launcher points at the new bundle.
    assert old_bundle.is_dir()
    assert f"{NEWER}-test" in os.readlink(layout.root / "ptest")
    recorded = recorder.read_text(encoding="utf-8")
    assert f"--dest {layout.root}" in recorded
    assert f"PTEST_PYTHON={tmp_path / 'fake-python'}" in recorded
    assert "PTEST_NO_UPDATE_CHECK=1" in recorded
    assert "PTEST_INSTALL_FAULT=set" in recorded
    assert (f"ptest: downloading ptest {NEWER} for "
            f"{update_api.platform_suffix()}") in capsys.readouterr().err
    assert list((tmp_path / "tmp").iterdir()) == []


def test_source_layout_refuses_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("PTEST_NO_UPDATE_CHECK", raising=False)
    calls: list[str] = []

    def deny_latest(timeout_s: float) -> str:
        calls.append("latest")
        raise AssertionError("network must not be reached")

    transport = update_api.Transport(
        latest_location=deny_latest,
        download=lambda *a: (_ for _ in ()).throw(
            AssertionError("network must not be reached")),
    )
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested=None, check_only=False,
                              transport=transport, layout=None)
    assert excinfo.value.code == "not-install-layout"
    assert excinfo.value.message == update_api.SOURCE_HINT
    assert calls == []


def test_detect_layout_real_source_returns_none():
    assert update_api.detect_layout() is None


# -- run_update decisions -------------------------------------------------------

def test_up_to_date_when_latest_is_running(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(RUNNING))
    result = update_api.run_update(requested=None, check_only=False,
                                   transport=fake.to_transport(),
                                   layout=layout)
    assert result.action == "up-to-date"
    assert result.target_version == RUNNING
    assert fake.download_calls == []
    assert update_api.render_text(result) == f"ptest is up to date ({RUNNING})\n"


def test_check_only_reports_without_downloading(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    result = update_api.run_update(requested=None, check_only=True,
                                   transport=fake.to_transport(),
                                   layout=layout)
    assert result.action == "available"
    assert result.check_only is True
    assert fake.download_calls == []
    assert update_api.render_text(result) == (
        f"ptest {NEWER} is available (installed {RUNNING})"
        f" \u2014 run: ptest update\n")


def test_explicit_older_version_installs(tmp_path, monkeypatch):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch,
                                             version=OLDER)
    result = _run_install(OLDER, layout, fake, tmp_path, monkeypatch)
    assert result.action == "updated"
    assert result.target_version == OLDER


def test_latest_lower_than_running_is_up_to_date(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(OLDER))
    result = update_api.run_update(requested=None, check_only=False,
                                   transport=fake.to_transport(),
                                   layout=layout)
    assert result.action == "up-to-date"
    assert fake.download_calls == []


def test_offline_update_is_retryable_75(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=None)
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested=None, check_only=False,
                              transport=fake.to_transport(), layout=layout)
    assert excinfo.value.code == "update-unavailable"
    assert excinfo.value.retryable is True


def test_missing_release_asset_reports_suffix(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested=NEWER, check_only=False,
                              transport=fake.to_transport(), layout=layout)
    assert excinfo.value.code == "update-failed"
    assert SUFFIX in excinfo.value.message


def test_updated_renders_s7_and_document_data(tmp_path):
    layout = _fake_root(tmp_path)
    result = update_api.UpdateResult(running_version=RUNNING,
                                     target_version=NEWER, action="updated",
                                     check_only=False,
                                     install_root=layout.root)
    assert update_api.render_text(result) == (
        f"ptest updated to {NEWER} (was {RUNNING})\n")
    data = update_api.document_data(result)
    assert data == {"running_version": RUNNING, "target_version": NEWER,
                    "action": "updated", "check_only": False,
                    "install_root": str(layout.root)}


# -- CLI wiring ------------------------------------------------------------------

def _cli_update(case, monkeypatch, *, transport=None, layout=None):
    from ptest import cli as cli_api

    domain = case.domain()
    _check_env(monkeypatch, case.base, transport=transport)
    if layout is not None:
        monkeypatch.setattr(update_api, "detect_layout",
                            lambda module_file=None: layout)
    return cli_api, domain


def test_cli_update_check_reports_s6(case, monkeypatch, capsys):
    layout = _fake_root(case.base)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    cli_api, domain = _cli_update(
        case, monkeypatch, transport=fake.to_transport(), layout=layout)
    code = cli_api.main(("--fixture-domain", str(domain.root),
                         "update", "--check"))
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == (f"ptest {NEWER} is available (installed {RUNNING})"
                            f" \u2014 run: ptest update\n")
    assert captured.err == ""


def test_cli_update_json_round_trips(case, monkeypatch, capsys):
    layout = _fake_root(case.base)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    cli_api, domain = _cli_update(
        case, monkeypatch, transport=fake.to_transport(), layout=layout)
    code = cli_api.main(("--fixture-domain", str(domain.root),
                         "update", "--check", "--json"))
    assert code == 0
    captured = capsys.readouterr()
    doc = C.decode_public_document(captured.out.encode())
    assert doc.kind == "update"
    assert set(doc.data) == {"running_version", "target_version", "action",
                             "check_only", "install_root"}
    assert doc.data["action"] == "available"
    assert doc.data["check_only"] is True
    assert captured.err == ""


def test_cli_update_source_refuses_exit_2(case, monkeypatch, capsys):
    def deny_latest(timeout_s: float) -> str:
        raise AssertionError("network must not be reached")

    transport = update_api.Transport(
        latest_location=deny_latest,
        download=lambda *a: (_ for _ in ()).throw(
            AssertionError("network must not be reached")),
    )
    cli_api, domain = _cli_update(
        case, monkeypatch, transport=transport, layout=None)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: None)
    code = cli_api.main(("--fixture-domain", str(domain.root), "update"))
    assert code == 2
    captured = capsys.readouterr()
    assert update_api.SOURCE_HINT in captured.err


def test_cli_update_offline_exits_75(case, monkeypatch, capsys):
    layout = _fake_root(case.base)
    fake = FakeTransport({}, location=None)
    cli_api, domain = _cli_update(
        case, monkeypatch, transport=fake.to_transport(), layout=layout)
    code = cli_api.main(("--fixture-domain", str(domain.root), "update"))
    assert code == 75
    captured = capsys.readouterr()
    assert "could not reach the ptest releases on GitHub" in captured.err


def test_cli_update_help_shows_topic(tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api

    monkeypatch.chdir(tmp_path)
    assert cli_api.main(("update", "--help")) == 0
    assert "ptest update [--check] [--version X.Y.Z] [--json]" in (
        capsys.readouterr().out)


def test_cli_update_check_and_version_rejected(tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api

    monkeypatch.chdir(tmp_path)
    code = cli_api.main(("update", "--check", "--version", "0.3.8"))
    assert code == 2
    assert "--check and --version cannot be combined" in (
        capsys.readouterr().err)


def test_cli_update_hostile_version_rejected(tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api

    monkeypatch.chdir(tmp_path)
    code = cli_api.main(("update", "--version", "../0.3.8"))
    assert code == 2
    assert "--version must be a release number like 0.3.7" in (
        capsys.readouterr().err)


# -- startup check -----------------------------------------------------------------

def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _check_env(monkeypatch, tmp_path, *, transport=None):
    """Prepare env for a direct startup_check call; return the cache dir."""
    monkeypatch.delenv("PTEST_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    state = _private_dir(tmp_path / "state")
    coord = _private_dir(state / "coordination")
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    if transport is not None:
        monkeypatch.setattr(update_api, "default_transport",
                            lambda: transport)
    return coord


@pytest.mark.parametrize("setup", [
    "no-check-env", "ci-true", "fixture", "quiet",
])
def test_opt_outs_never_touch_transport(tmp_path, monkeypatch, capsys,
                                        setup):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    kwargs = {"quiet": False, "json_output": False, "fixture": False}
    if setup == "no-check-env":
        monkeypatch.setenv("PTEST_NO_UPDATE_CHECK", "1")
    elif setup == "ci-true":
        monkeypatch.setenv("CI", "true")
    elif setup == "fixture":
        kwargs["fixture"] = True
    elif setup == "quiet":
        kwargs["quiet"] = True
    update_api.startup_check(["status"], transport=fake.to_transport(),
                             **kwargs)
    assert fake.latest_calls == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("ci_value", ["0", "false", "off", "no", ""])
def test_falsy_ci_still_checks(tmp_path, monkeypatch, capsys, ci_value):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    monkeypatch.setenv("CI", ci_value)
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport())
    assert fake.latest_calls != []
    assert "run: ptest update" in capsys.readouterr().err


def test_exempt_commands_skip_startup_check(tmp_path, monkeypatch):
    import ptest.cli as cli_api

    calls: list = []
    monkeypatch.setattr(update_api, "startup_check",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("PTEST_NO_UPDATE_CHECK", raising=False)
    monkeypatch.chdir(tmp_path)
    for command in ("help", "version", "update"):
        if command == "help":
            assert cli_api.main(("help",)) == 0
        elif command == "version":
            assert cli_api.main(("--version",)) == 0
        else:
            layout = _fake_root(tmp_path)
            fake = FakeTransport({}, location=_tag_url(RUNNING))
            monkeypatch.setattr(update_api, "default_transport",
                                lambda: fake.to_transport())
            monkeypatch.setattr(update_api, "detect_layout",
                                lambda module_file=None: layout)
            cli_api.main(("update", "--check"))
    assert calls == []


def test_non_tty_notice_is_s2_exact(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"ptest: update available: {NEWER} (installed {RUNNING})"
        f" \u2014 run: ptest update\n")


def test_source_notice_is_s3_exact(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: None)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"ptest: update available: {NEWER} (installed {RUNNING})"
        f" \u2014 installed from source; update it with git pull\n")


def test_notice_is_stderr_only_and_json_stdout_unchanged(
        tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api

    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.chdir(tmp_path)
    code = cli_api.main(("where", "--json"))
    captured = capsys.readouterr()
    assert code == 0
    doc = C.decode_public_document(captured.out.encode())
    assert doc.kind == "where"
    assert captured.err == (
        f"ptest: update available: {NEWER} (installed {RUNNING})"
        f" \u2014 run: ptest update\n")


def test_startup_check_never_changes_exit_status(
        tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api

    def boom(timeout_s: float) -> str:
        raise RuntimeError("transport exploded")

    transport = update_api.Transport(
        latest_location=boom,
        download=lambda *a: (_ for _ in ()).throw(AssertionError()),
    )
    _check_env(monkeypatch, tmp_path, transport=transport)
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.chdir(tmp_path)
    exploding = cli_api.main(("where", "--json"))
    capsys.readouterr()
    monkeypatch.setenv("PTEST_NO_UPDATE_CHECK", "1")
    baseline = cli_api.main(("where", "--json"))
    capsys.readouterr()
    assert exploding == baseline


def test_offline_startup_means_no_notice(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=None)
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport())
    assert capsys.readouterr().err == ""


def test_failed_fetch_backs_off_via_claim(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=None)
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=1_000_000.0)
    assert len(fake.latest_calls) == 1
    cache = json.loads((domain / "update-check.json").read_text(
        encoding="utf-8"))
    first_checked = cache["checked_at"]
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=1_000_000.0 + 60)
    assert len(fake.latest_calls) == 1
    assert capsys.readouterr().err == ""


def test_cache_hit_makes_no_network_call(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    now = 2_000_000.0
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": now - 60, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=now)
    assert fake.latest_calls == []
    assert "run: ptest update" in capsys.readouterr().err


def test_startup_fetch_is_wall_clock_bounded(tmp_path, monkeypatch, capsys):
    import threading

    monkeypatch.setattr(update_api, "CHECK_TIMEOUT_S", 0.2)
    released = threading.Event()

    def hanging(timeout_s: float) -> str:
        assert released.wait(timeout=10)
        return _tag_url(NEWER)

    transport = update_api.Transport(
        latest_location=hanging,
        download=lambda *a: (_ for _ in ()).throw(AssertionError()),
    )
    domain = _check_env(monkeypatch, tmp_path, transport=transport)
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    started = time.monotonic()
    try:
        update_api.startup_check(["status"], quiet=False, json_output=False,
                                 fixture=False, transport=transport,
                                 now=3_000_000.0)
    finally:
        released.set()
    assert time.monotonic() - started < 5
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("payload", [
    "not json",
    '{"schema": 2, "checked_at": 1.0}',
    '{"schema": 1, "checked_at": 9999999999.0, "latest": "9.9.9",'
    ' "declined": null, "declined_at": null}',
    '{"schema": 1, "checked_at": 1.0, "latest": "bogus",'
    ' "declined": null, "declined_at": null}',
    '{"schema": 1, "checked_at": true, "latest": null,'
    ' "declined": null, "declined_at": null}',
    '{"schema": 1, "checked_at": 1.0, "latest": "9.9.9",'
    ' "declined": "9.9.9", "declined_at": true}',
    "x" * 5000,
])
def test_corrupt_cache_is_a_miss_never_followed(
        tmp_path, monkeypatch, capsys, payload):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    cache = domain / "update-check.json"
    if payload == "not json":
        cache.write_text("not json", encoding="utf-8")
    else:
        cache.write_text(payload, encoding="utf-8")
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=4_000_000.0)
    assert fake.latest_calls != []


def test_symlink_cache_never_followed(tmp_path, monkeypatch, capsys):
    # A symlinked cache is never read and its claim write is refused, so
    # the check stays silent without fetching: no crash, no follow.
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"schema": 1, "checked_at": 0.0,
                                   "latest": None, "declined": None,
                                   "declined_at": None}), encoding="utf-8")
    (domain / "update-check.json").symlink_to(outside)
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=4_000_100.0)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert fake.latest_calls == []
    assert (domain / "update-check.json").is_symlink()


def test_prompt_eof_declines(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "")
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=5_000_000.0)
    captured = capsys.readouterr()
    assert f"ptest {NEWER} is available (you have {RUNNING})" in captured.err
    cache = json.loads((domain / "update-check.json").read_text(
        encoding="utf-8"))
    assert cache["declined"] == NEWER


def test_decline_remembered_for_24h_then_asks_again(
        tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "n\n")
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": 6_000_000.0, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=6_000_100.0)
    assert "Update now?" in capsys.readouterr().err
    # Second run within 24h stays silent.
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=6_000_200.0)
    assert capsys.readouterr().err == ""
    # After 24h it asks again.
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=6_000_100.0 + 86400 + 1)
    assert "Update now?" in capsys.readouterr().err


def test_json_tty_gets_notice_never_prompt(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    update_api.startup_check(["status"], quiet=False, json_output=True,
                             fixture=False, transport=fake.to_transport())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run: ptest update" in captured.err
    assert "Update now?" not in captured.err


def test_accept_updates_then_reexecs_with_literal_argv(
        tmp_path, monkeypatch, capsys):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch)
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "\n")
    exec_calls: list = []
    monkeypatch.setattr(update_api, "_execv",
                        lambda path, argv: exec_calls.append((path, argv)))
    argv = ["status", "--workers", "2"]
    update_api.startup_check(argv, quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=7_000_000.0)
    assert exec_calls != []
    path, exec_argv = exec_calls[0]
    assert path == str(layout.root / "ptest")
    assert exec_argv == [str(layout.root / "ptest"), *argv]
    captured = capsys.readouterr()
    assert f"ptest: updated to {NEWER} (was {RUNNING})" in captured.err


def test_reexec_failure_continues_with_next_run_notice(
        tmp_path, monkeypatch, capsys):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch)
    _check_env(monkeypatch, tmp_path, transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    def failing_execv(path, argv):
        raise OSError("no exec")

    monkeypatch.setattr(update_api, "_execv", failing_execv)
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=7_000_100.0)
    assert "takes effect on the next run" in capsys.readouterr().err


def test_prompted_update_failure_continues_and_records_decline(
        tmp_path, monkeypatch, capsys):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=None)
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": 8_000_000.0, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=8_000_100.0)
    captured = capsys.readouterr()
    assert f"continuing with {RUNNING}" in captured.err
    cache = json.loads((domain / "update-check.json").read_text(
        encoding="utf-8"))
    assert cache["declined"] == NEWER


def test_parse_sha256_rejects_malformed():
    name = _asset(NEWER)
    good = _sha_line(b"body", name)
    assert update_api._parse_sha256(good, name) == good.decode().split()[0]
    star = f"{good.decode().split()[0]}  *{name}\n".encode()
    assert update_api._parse_sha256(star, name) == good.decode().split()[0]
    for bad in (b"\xff\xfe binary", b"nothex  " + name.encode(),
                b"ab12  " + name.encode(),
                f"{'a' * 64}  other-file.tar.gz\n".encode(),
                b""):
        with pytest.raises(C.Problem) as excinfo:
            update_api._parse_sha256(bad, name)
        assert excinfo.value.code == "update-failed"
        assert "the current install is unchanged" in excinfo.value.message


@pytest.mark.parametrize("name", [
    "", "/д", "ptest-9.9.9/\x00evil",
])
def test_check_member_rejects_bad_names(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = 1
    with pytest.raises(C.Problem):
        update_api._check_member(f"ptest-{NEWER}", info)


@pytest.mark.parametrize("name", [
    "/absolute", "ptest-9.9.9/../evil", "ptest-9.9.9/./x",
    "other-1.0.0/file",
])
def test_check_member_rejects_escape_and_wrong_top(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = 1
    with pytest.raises(C.Problem):
        update_api._check_member(f"ptest-{NEWER}", info)


def test_check_member_rejects_non_regular():
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                 tarfile.FIFOTYPE):
        info = tarfile.TarInfo(f"ptest-{NEWER}/node")
        info.type = kind
        with pytest.raises(C.Problem):
            update_api._check_member(f"ptest-{NEWER}", info)


def test_missing_release_message_parses_release_url():
    url = (_release_base(NEWER) + "/" + _asset(NEWER))
    assert update_api._missing_release_message(url) == (
        f"no ptest {NEWER} release for {SUFFIX}; "
        "the current install is unchanged")
    fallback = update_api._missing_release_message("https://github.com/x")
    assert fallback.endswith("; the current install is unchanged")


def test_resolve_latest_rejects_non_string_and_unexpected_throw(tmp_path):
    weird = update_api.Transport(
        latest_location=lambda timeout: 12345,
        download=lambda *a: (_ for _ in ()).throw(AssertionError()),
    )
    with pytest.raises(C.Problem) as excinfo:
        update_api.resolve_latest(weird, timeout_s=5.0)
    assert excinfo.value.code == "update-unavailable"

    def boom(timeout_s: float) -> str:
        raise RuntimeError("socket exploded")

    with pytest.raises(C.Problem) as excinfo:
        update_api.resolve_latest(
            update_api.Transport(latest_location=boom,
                                 download=lambda *a: None),
            timeout_s=5.0)
    assert excinfo.value.code == "update-unavailable"


def test_fetch_thread_failure_is_silent_none():
    def boom(timeout_s: float) -> str:
        raise RuntimeError("dns exploded")

    transport = update_api.Transport(
        latest_location=boom, download=lambda *a: None)
    assert update_api._fetch_latest_bounded(transport, 5.0) is None


def test_run_update_rejects_hostile_requested_before_network(tmp_path):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested="../9.9.9", check_only=True,
                              transport=fake.to_transport(), layout=layout)
    assert excinfo.value.code == "invalid-config"
    assert fake.latest_calls == [] and fake.download_calls == []


def test_find_python_requires_uv_and_absolute_regular(monkeypatch, tmp_path):
    monkeypatch.setattr(update_api.shutil, "which", lambda name: None)
    with pytest.raises(C.Problem) as excinfo:
        update_api._find_python()
    assert excinfo.value.code == "update-failed"
    monkeypatch.setattr(update_api.shutil, "which",
                        lambda name: str(tmp_path / "uv"))
    monkeypatch.setattr(update_api.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1,
                                                       "stdout": ""})())
    with pytest.raises(C.Problem):
        update_api._find_python()


def test_detect_layout_bundle_and_mismatch(tmp_path):
    layout = _fake_root(tmp_path)
    probe = layout.root / ".ptest-bundles" / layout.bundle_id / "probe.py"
    probe.write_text("x", encoding="utf-8")
    found = update_api.detect_layout(str(probe))
    assert found is not None
    assert found.root == layout.root
    assert found.bundle_id == layout.bundle_id
    (layout.root / "ptest").unlink()
    assert update_api.detect_layout(str(probe)) is None


def test_document_data_allows_null_install_root():
    result = update_api.UpdateResult(running_version=RUNNING,
                                     target_version=RUNNING,
                                     action="up-to-date", check_only=False,
                                     install_root=None)
    assert update_api.document_data(result)["install_root"] is None
    assert update_api.render_text(result) == (
        f"ptest is up to date ({RUNNING})\n")


def test_prompt_ctrl_c_exits_130(tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    layout = _fake_root(tmp_path)
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    def cancel():
        raise KeyboardInterrupt

    monkeypatch.setattr(sys.stdin, "readline", cancel)
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": 9_000_000.0, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        update_api.startup_check(["status"], quiet=False, json_output=False,
                                 fixture=False,
                                 transport=fake.to_transport(),
                                 now=9_000_100.0)
    assert excinfo.value.code == 130


def test_url_allowed_rejects_garbage_port():
    assert update_api._url_allowed("https://github.com:bad/x") is False
    assert update_api._url_allowed("https://github.com:443/x") is True


def _fixture_server(monkeypatch, routes):
    """Local fixture server; the allowlist check alone is stubbed to it."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _serve(self):
            body, code, headers = routes[self.path]
            if callable(body):
                body, code, headers = body()
            payload = body if isinstance(body, bytes) else b""
            self.send_response(code)
            names = {name.lower() for name, _ in headers}
            for name, value in headers:
                self.send_header(name, value)
            if payload and "content-length" not in names:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)

        do_GET = _serve
        do_HEAD = _serve

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        update_api, "_url_allowed",
        lambda url: url.startswith(
            f"http://127.0.0.1:{server.server_port}/"))
    return server


def test_read_location_returns_redirect_target_without_following(
        monkeypatch):
    tag = _tag_url("0.3.8")
    server = _fixture_server(monkeypatch, {
        "/latest": (b"", 302, [("Location", tag)]),
        "/gone": (b"", 404, [("Location", tag)]),
    })
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert update_api._read_location(f"{base}/latest", 5.0) == tag
        # An error status carrying Location still yields it, never a fetch.
        assert update_api._read_location(f"{base}/gone", 5.0) == tag
    finally:
        server.shutdown()
        server.server_close()


def test_default_transport_resolves_latest_end_to_end(tmp_path, monkeypatch):
    # 0.4.2 shipped with the real transport miswired (it called
    # _read_location without its URL), so every real check was "offline"
    # while every fake-transport test passed. Exercise the real transport:
    # only the host is swapped for a local fixture server.
    server = _fixture_server(monkeypatch, {
        "/latest": (b"", 302, [("Location", _tag_url(NEWER))]),
    })
    try:
        monkeypatch.setattr(
            update_api, "LATEST_URL",
            f"http://127.0.0.1:{server.server_port}/latest")
        transport = update_api.default_transport()
        assert update_api.resolve_latest(transport, timeout_s=5.0) == NEWER
        result = update_api.run_update(requested=None, check_only=True,
                                       layout=_fake_root(tmp_path))
        assert (result.action, result.target_version) == ("available", NEWER)
    finally:
        server.shutdown()
        server.server_close()


def test_stream_download_success_redirect_cap_and_404(monkeypatch):
    body = b"x" * 1024
    release_path = ("/Avocado-Blockchain-Services/ptest/releases/download/"
                    f"v{NEWER}/{_asset(NEWER)}")
    server = _fixture_server(monkeypatch, {
        "/file": (body, 200, []),
        "/redir": (b"", 302, [("Location", "/file")]),
        "/loop": (b"", 302, [("Location", "/loop")]),
        "/big": (b"y", 200, [("Content-Length", str(10**9))]),
        release_path: (b"", 404, []),
    })
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        sink = io.BytesIO()
        update_api._stream_download(f"{base}/file", sink, 65536, 5.0)
        assert sink.getvalue() == body
        sink = io.BytesIO()
        update_api._stream_download(f"{base}/redir", sink, 65536, 5.0)
        assert sink.getvalue() == body
        # Past five redirects the handler stops and the refused 302 fails.
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/loop", io.BytesIO(),
                                        65536, 5.0)
        assert excinfo.value.code == "update-unavailable"
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/big", io.BytesIO(), 16, 5.0)
        assert excinfo.value.code == "update-failed"
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}{release_path}", io.BytesIO(),
                                        65536, 5.0)
        assert excinfo.value.code == "update-failed"
        assert excinfo.value.message == (
            f"no ptest {NEWER} release for {SUFFIX}; "
            "the current install is unchanged")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_network_guard_records_hits_from_the_fetch_thread():
    # The startup check fetches in a worker thread, where pytest.fail only
    # kills the thread; the guard must still record the hit so the test
    # fails at teardown instead of passing silently.
    violations = update_api._read_location.violations
    assert update_api._fetch_latest_bounded(
        update_api.default_transport(), 2.0) is None
    assert violations == [update_api.LATEST_URL]
    violations.clear()
    with pytest.raises(pytest.fail.Exception):
        update_api._read_location("http://127.0.0.1:1@github.com/x", 1.0)
    assert violations == ["http://127.0.0.1:1@github.com/x"]
    violations.clear()


def test_stream_download_refuses_off_allowlist(monkeypatch):
    # The allowlist refuses before any connection is even built.
    def no_opener(*handlers):
        pytest.fail("off-allowlist URL reached the network layer")

    monkeypatch.setattr(update_api.urllib.request, "build_opener", no_opener)
    stream_download = update_api._stream_download.unguarded
    with pytest.raises(C.Problem) as excinfo:
        stream_download("https://example.com/file", io.BytesIO(), 65536, 5.0)
    assert excinfo.value.code == "update-unavailable"


def test_stream_download_conn_refused_is_unavailable(monkeypatch):
    monkeypatch.setattr(update_api, "_url_allowed", lambda url: True)
    with pytest.raises(C.Problem) as excinfo:
        update_api._stream_download("http://127.0.0.1:1/file", io.BytesIO(),
                                    65536, 2.0)
    assert excinfo.value.code == "update-unavailable"


def test_stream_download_foreign_redirect_and_500(monkeypatch):
    server = _fixture_server(monkeypatch, {
        "/evil": (b"", 302, [("Location", "https://example.com/x")]),
        "/broken": (b"", 500, []),
        "/weird-cl": (b"y", 200, [("Content-Length", "garbage")]),
    })
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/evil", io.BytesIO(),
                                        65536, 5.0)
        assert excinfo.value.code == "update-unavailable"
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/broken", io.BytesIO(),
                                        65536, 5.0)
        assert excinfo.value.code == "update-unavailable"
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/weird-cl", io.BytesIO(),
                                        65536, 5.0)
        assert excinfo.value.code == "update-failed"
    finally:
        server.shutdown()
        server.server_close()


def test_read_location_without_location_is_unavailable(monkeypatch):
    server = _fixture_server(monkeypatch, {
        "/plain": (b"hello", 200, []),
        "/missing": (b"", 404, []),
    })
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(C.Problem):
            update_api._read_location(f"{base}/plain", 5.0)
        with pytest.raises(C.Problem):
            update_api._read_location(f"{base}/missing", 5.0)
        with pytest.raises(C.Problem):
            update_api._read_location("http://127.0.0.1:1/latest", 1.0)
    finally:
        server.shutdown()
        server.server_close()


def test_urlsplit_value_error_is_not_allowed():
    assert update_api._url_allowed("https://\ud800/") is False


def test_post_verify_rejects_missing_and_mismatched_bundles(tmp_path):
    import shutil as _shutil

    layout = _fake_root(tmp_path / "gone")
    update_api._post_verify(layout.root, RUNNING)
    _shutil.rmtree(layout.root / ".ptest-bundles")
    with pytest.raises(C.Problem) as excinfo:
        update_api._post_verify(layout.root, RUNNING)
    assert excinfo.value.code == "update-failed"

    layout = _fake_root(tmp_path / "stale")
    bundle = layout.root / ".ptest-bundles" / layout.bundle_id
    (bundle / "complete.json").write_text(json.dumps(
        {"version": 1, "bundle_id": layout.bundle_id,
         "ptest_version": "0.0.0"}), encoding="utf-8")
    with pytest.raises(C.Problem):
        update_api._post_verify(layout.root, RUNNING)
    (bundle / "complete.json").write_text("not json", encoding="utf-8")
    with pytest.raises(C.Problem):
        update_api._post_verify(layout.root, RUNNING)


def test_find_python_rejects_relative_and_follows_symlinks(
        monkeypatch, tmp_path):
    monkeypatch.setattr(update_api.shutil, "which",
                        lambda name: str(tmp_path / "uv"))

    def run_relative(*args, **kwargs):
        return type("R", (), {"returncode": 0,
                              "stdout": "relative/python\n"})()

    monkeypatch.setattr(update_api.subprocess, "run", run_relative)
    with pytest.raises(C.Problem):
        update_api._find_python()

    target = tmp_path / "real-python"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "link-python"
    link.symlink_to(target)

    def run_link(*args, **kwargs):
        return type("R", (), {"returncode": 0,
                              "stdout": f"{link}\n"})()

    monkeypatch.setattr(update_api.subprocess, "run", run_link)
    # A link to a non-executable file is refused ...
    with pytest.raises(C.Problem):
        update_api._find_python()
    # ... but system and Homebrew pythons are symlinks to real interpreters
    # (/usr/bin/python3 -> python3.14) and must be accepted as get.sh does.
    target.chmod(0o755)
    assert update_api._find_python() == str(link)
    target.unlink()
    with pytest.raises(C.Problem):
        update_api._find_python()

    def run_boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(update_api.subprocess, "run", run_boom)
    with pytest.raises(C.Problem):
        update_api._find_python()


def test_safe_extract_truncated_bundle_is_a_refusal(tmp_path):
    # A damaged archive whose checksum still matched must be an update
    # refusal (exit 2 / JSON error), never a traceback.
    payload = os.urandom(256 * 1024)
    member = tarfile.TarInfo(f"ptest-{NEWER}/big.bin")
    member.size = len(payload)
    body = _make_bundle_tar(NEWER, extra_members=[(member, io.BytesIO(payload))])
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(body[: len(body) // 2])
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(C.Problem) as excinfo:
        update_api._safe_extract(archive, dest, NEWER)
    assert excinfo.value.code == "update-failed"
    assert "cannot extract" in excinfo.value.message


def test_safe_extract_write_failure_is_a_refusal(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(_make_bundle_tar(NEWER))
    dest = tmp_path / "readonly"
    dest.mkdir()
    dest.chmod(0o500)
    try:
        with pytest.raises(C.Problem) as excinfo:
            update_api._safe_extract(archive, dest, NEWER)
    finally:
        dest.chmod(0o700)
    assert excinfo.value.code == "update-failed"
    assert "cannot extract" in excinfo.value.message
    assert "duplicate" not in excinfo.value.message


def test_staging_dir_failure_is_a_refusal(tmp_path, monkeypatch):
    layout, fake, _ = _install_success_setup(tmp_path, monkeypatch)

    def no_tmp(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update_api.tempfile, "mkdtemp", no_tmp)
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert excinfo.value.code == "update-failed"


def test_disk_full_download_is_not_reported_as_offline(tmp_path):
    import errno

    def full(url, sink, max_bytes, timeout_s):
        raise OSError(errno.ENOSPC, "No space left on device")

    transport = update_api.Transport(latest_location=lambda t: "",
                                     download=full)
    target = tmp_path / "bundle.tar.gz"
    with pytest.raises(C.Problem) as excinfo:
        update_api._download_to(transport, "https://github.com/x", target,
                                1024, 1.0)
    assert excinfo.value.code == "update-failed"
    assert "No space left" in excinfo.value.message
    assert not target.exists()


def test_disk_full_at_final_flush_is_a_refusal(tmp_path, monkeypatch):
    import errno

    class FullOnClose(io.BufferedWriter):
        def close(self):
            super().close()
            raise OSError(errno.ENOSPC, "No space left on device")

    real_fdopen = os.fdopen

    def fdopen(fd, mode="r", *args, **kwargs):
        if mode == "wb":
            return FullOnClose(io.FileIO(fd, "wb", closefd=True))
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(update_api.os, "fdopen", fdopen)
    transport = update_api.Transport(
        latest_location=lambda t: "",
        download=lambda url, sink, max_bytes, timeout_s: sink.write(b"x"))
    target = tmp_path / "bundle.tar.gz.sha256"
    with pytest.raises(C.Problem) as excinfo:
        update_api._download_to(transport, "https://github.com/x", target,
                                1024, 1.0)
    assert excinfo.value.code == "update-failed"
    assert "No space left" in excinfo.value.message
    assert not target.exists()


def test_bare_update_beside_same_named_path_refuses(tmp_path, monkeypatch,
                                                     capsys):
    from ptest import cli as cli_api

    fake = FakeTransport({}, location=_tag_url(NEWER))
    monkeypatch.setattr(update_api, "default_transport",
                        lambda: fake.to_transport())
    monkeypatch.chdir(tmp_path)
    for word in ("update", "upgrade"):
        (tmp_path / word).mkdir()
        assert cli_api.main((word,)) == 2
        err = capsys.readouterr().err
        assert f"`ptest ./{word}`" in err
        (tmp_path / word).rmdir()
    assert fake.latest_calls == [] and fake.download_calls == []


def _process_gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def test_installer_timeout_kills_the_whole_installer_group(tmp_path,
                                                           monkeypatch):
    # install.sh runs install.py as a child; a timeout must not leave that
    # child running to switch the launcher after we reported "unchanged".
    pid_file = tmp_path / "child.pid"
    body = ("#!/bin/sh\n"
            f"sh -c 'echo $$ > \"{pid_file}\"; exec sleep 60'\n")
    tarball = _make_bundle_tar(NEWER, install_sh=body)
    fake = FakeTransport(_update_files(NEWER, tarball),
                         location=_tag_url(NEWER))
    layout, _, _ = _install_success_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(update_api, "INSTALL_TIMEOUT_S", 1)
    with pytest.raises(C.Problem) as excinfo:
        _run_install(NEWER, layout, fake, tmp_path, monkeypatch)
    assert "did not finish" in excinfo.value.message
    child = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while not _process_gone(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _process_gone(child)


def test_uninstall_skips_startup_check():
    import ptest.cli as cli_api

    # Offering an update right before `uninstall --self` would download
    # and install a bundle only to delete it.
    assert "uninstall" in cli_api._UPDATE_CHECK_EXEMPT


def test_safe_extract_rejects_garbage(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"this is not a gzip tarball")
    with pytest.raises(C.Problem) as excinfo:
        update_api._safe_extract(archive, tmp_path, NEWER)
    assert excinfo.value.code == "update-failed"


def test_detect_layout_tolerates_os_errors(tmp_path, monkeypatch):
    assert update_api.detect_layout(
        str(tmp_path / "ghost" / ".ptest-bundles" / "x" / "mod.py")) is None
    monkeypatch.setattr(update_api.os.path, "realpath",
                        lambda path: (_ for _ in ()).throw(
                            OSError("no path")))
    assert update_api.detect_layout("anything") is None


def test_stage_paths_reject_existing_files(tmp_path):
    target = tmp_path / "staged.bin"
    target.write_bytes(b"old")
    fake = FakeTransport({}, location=_tag_url(NEWER))
    with pytest.raises(C.Problem):
        update_api._download_to(fake.to_transport(), "http://x/y", target,
                                65536, 5.0)
    with pytest.raises(C.Problem):
        update_api._bounded_copy(io.BytesIO(b"new"), target, 65536)
    assert target.read_bytes() == b"old"


def test_run_installer_reports_oserror(tmp_path, monkeypatch, capsys):
    python = tmp_path / "fake-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    bindir = tmp_path / "bin"
    _uv_shim(bindir, python)
    monkeypatch.setenv("PATH",
                       str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    def run_boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(update_api.subprocess, "run", run_boom)
    log = io.StringIO()
    with pytest.raises(C.Problem) as excinfo:
        update_api._run_installer(tmp_path / "bundle", tmp_path,
                                  tmp_path, log)
    assert excinfo.value.code == "update-failed"
    assert "the current install is unchanged" in excinfo.value.message


def test_run_update_rejects_loose_tmpdir(tmp_path, monkeypatch):
    import tempfile as _tempfile

    layout = _fake_root(tmp_path)
    tarball = _make_bundle_tar(NEWER)
    fake = FakeTransport(_update_files(NEWER, tarball),
                         location=_tag_url(NEWER))
    real_mkdtemp = _tempfile.mkdtemp

    def loose(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        os.chmod(path, 0o755)
        return path

    monkeypatch.setattr(_tempfile, "mkdtemp", loose)
    with pytest.raises(C.Problem) as excinfo:
        update_api.run_update(requested=NEWER, check_only=False,
                              transport=fake.to_transport(), layout=layout)
    assert excinfo.value.code == "update-failed"


def test_prompted_update_cancel_exits_130(tmp_path, monkeypatch, capsys):
    layout = _fake_root(tmp_path)
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())
    monkeypatch.setattr(update_api, "detect_layout",
                        lambda module_file=None: layout)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "y\n")

    def cancel(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(update_api, "run_update", cancel)
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": 9_100_000.0, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        update_api.startup_check(["status"], quiet=False, json_output=False,
                                 fixture=False,
                                 transport=fake.to_transport(),
                                 now=9_100_100.0)
    assert excinfo.value.code == 130


def test_startup_check_swallows_unexpected_layout_failure(
        tmp_path, monkeypatch, capsys):
    fake = FakeTransport({}, location=_tag_url(NEWER))
    domain = _check_env(monkeypatch, tmp_path,
                        transport=fake.to_transport())

    def boom(module_file=None):
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(update_api, "detect_layout", boom)
    (domain / "update-check.json").write_text(json.dumps({
        "schema": 1, "checked_at": 9_200_000.0, "latest": NEWER,
        "declined": None, "declined_at": None}), encoding="utf-8")
    update_api.startup_check(["status"], quiet=False, json_output=False,
                             fixture=False, transport=fake.to_transport(),
                             now=9_200_100.0)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_stream_download_deadline_is_bounded(monkeypatch):
    def slow():
        import time as _time
        _time.sleep(1.0)
        return (b"x", 200, [])

    server = _fixture_server(monkeypatch, {"/slow": slow})
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        started = time.monotonic()
        with pytest.raises(C.Problem) as excinfo:
            update_api._stream_download(f"{base}/slow", io.BytesIO(),
                                        65536, 0.2)
        assert excinfo.value.code == "update-unavailable"
        assert time.monotonic() - started < 3
    finally:
        server.shutdown()
        server.server_close()


def test_install_smoke_runs_disable_update_check():
    """The three post-install smoke runs must carry PTEST_NO_UPDATE_CHECK=1.

    Parsed from AST so removing the env from any smoke call fails: the
    new bundle's smoke `ptest guide` would otherwise prompt or hit the
    network mid-install.
    """
    import ast

    path = (Path(__file__).resolve().parents[2]
            / "scripts" / "install.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    smoke_runs = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"):
            continue
        func = node.func
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        first = node.args[0].elts
        if len(first) < 2 or not isinstance(first[1], ast.Constant):
            continue
        if first[1].value not in ("--version", "guide", "-c"):
            continue
        smoke_runs += 1
        env = next((kw for kw in node.keywords if kw.arg == "env"), None)
        assert env is not None, ast.dump(node)
        assert "'PTEST_NO_UPDATE_CHECK': '1'" in ast.unparse(env.value)
    assert smoke_runs == 3
