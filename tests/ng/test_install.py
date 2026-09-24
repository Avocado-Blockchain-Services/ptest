from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
import sys
import os
import subprocess
import urllib.request
import shutil
import textwrap
import tomllib

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

import pytest

import install
from install import validate_manifest, validate_wheel, install_bundle


def _wheel(path: Path, name: str, version: str = "0.1.0", tag: str = "py3-none-any") -> Path:
    filename = f"{name.replace('-', '_')}-{version}-{tag}.whl"
    target = path / filename
    dist = name.replace("-", "_")
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(f"{dist}/__init__.py", "__version__ = '" + version + "'\n")
        archive.writestr(f"{dist}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n")
        archive.writestr(f"{dist}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        archive.writestr(f"{dist}-{version}.dist-info/RECORD", "")
    return target


def _manifest(wheelhouse: Path, *wheels: Path) -> Path:
    records = []
    for wheel in wheels:
        package = "psutil" if wheel.name.startswith("psutil") else "ptest-ng"
        version = "7.2.2" if package == "psutil" else "0.1.0"
        records.append({"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "package": package, "version": version})
    manifest = wheelhouse / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "ptest_version": "0.1.0", "python_tag": "py3", "platform_tag": "any", "wheels": records}))
    return manifest


def _inventory(dest: Path) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """Return the exact published/temporary names and public target."""
    bundles = dest / ".ptest-bundles"
    bundle_names = tuple(sorted(p.name for p in bundles.iterdir())) if bundles.is_dir() else ()
    link_names = tuple(sorted(p.name for p in dest.glob(".ptest-link-*"))) if dest.is_dir() else ()
    public = dest / "ptest"
    target = str(public.resolve(strict=False)) if public.is_symlink() else None
    return bundle_names, link_names, target


def test_manifest_requires_exact_pinned_pair(tmp_path):
    with pytest.raises(ValueError, match="exactly one ptest-ng and one psutil"):
        validate_manifest({"version": 1, "ptest_version": "0.1.0", "python_tag": "py3", "platform_tag": "any", "wheels": []})


def test_release_install_wrapper_discovers_bundled_wheelhouse(tmp_path):
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    (release / "wheelhouse").mkdir()
    (release / "wheelhouse" / "manifest.json").write_text("{}", encoding="utf-8")
    marker = tmp_path / "argv.json"
    (release / "scripts" / "install.py").write_text(textwrap.dedent(f"""\
        import json
        import sys
        from pathlib import Path
        Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))
    """), encoding="utf-8")
    wrapper = release / "install.sh"
    wrapper.write_text((Path(__file__).parents[2] / "install.sh").read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)

    destination = tmp_path / "install"
    subprocess.run([str(wrapper), "--dest", str(destination)], check=True)

    assert json.loads(marker.read_text()) == [
        "--dest", str(destination), "--wheelhouse", str(release / "wheelhouse"),
        "--manifest", str(release / "wheelhouse" / "manifest.json"),
    ]


def test_wheel_rejects_sdist_and_path_escape(tmp_path):
    with pytest.raises(ValueError, match="wheel filename"):
        validate_wheel(Path("../evil.tar.gz"), "ptest-ng", "0.1.0", "py3", "any")


def test_manifest_rejects_extra_wheel_and_wrong_hash(tmp_path):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = json.loads(_manifest(wheelhouse, ptest, psutil).read_text())
    manifest["wheels"].append(dict(manifest["wheels"][0]))
    with pytest.raises(ValueError, match="exactly one"):
        validate_manifest(manifest)
    manifest["wheels"] = manifest["wheels"][:2]
    manifest["wheels"][0]["sha256"] = "0" * 64
    (wheelhouse / "manifest.json").write_text(json.dumps(manifest))
    dest = tmp_path / "dest"
    before = _inventory(dest)
    with pytest.raises(ValueError):
        install_bundle(dest, wheelhouse, wheelhouse / "manifest.json")
    assert _inventory(dest) == before


def test_manifest_wheel_symlink_is_rejected(tmp_path):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    outside = tmp_path / "outside.whl"; outside.write_bytes(psutil.read_bytes())
    psutil.unlink(); psutil.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        install_bundle(tmp_path / "dest", wheelhouse, manifest)


def test_network_failure_does_not_leave_private_bundle_or_temp_link(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    psutil.unlink()
    dest = tmp_path / "dest"
    before = _inventory(dest)

    def denied(*args, **kwargs):
        raise OSError("network denied")

    monkeypatch.setattr(install, "urlopen", denied)
    with pytest.raises(OSError, match="network denied"):
        install_bundle(dest, wheelhouse, manifest, allow_network=True)
    assert _inventory(dest) == before


def test_failed_upgrade_keeps_old_target(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    bundles = tmp_path / ".ptest-bundles"
    bundles.mkdir(mode=0o700)
    bundles.chmod(0o700)
    old = bundles / "old" / "venv" / "bin"; old.mkdir(parents=True)
    (bundles / "old").chmod(0o700)
    (old / "ptest").write_text("#!/bin/sh\nexit 0\n"); (old / "ptest").chmod(0o755)
    current = tmp_path / "ptest"
    current.symlink_to(old / "ptest")
    before = _inventory(tmp_path)
    def fake_run(argv, **kwargs):
        if argv[:2] == ["uv", "venv"]:
            venv = Path(argv[-1]) / "bin"; venv.mkdir(parents=True)
            for name in ("ptest", "python"):
                target = venv / name; target.write_text("#!/bin/sh\nexit 0\n"); target.chmod(0o755)
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="before-symlink-replace"):
        install_bundle(tmp_path, wheelhouse, manifest, fault="before-symlink-replace")
    assert current.resolve() == old / "ptest"
    assert _inventory(tmp_path) == before


def test_success_writes_complete_marker_before_public_symlink(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["uv", "venv"]:
            venv = Path(argv[-1]) / "bin"; venv.mkdir(parents=True)
            entry = venv / "ptest"; entry.write_text("#!/bin/sh\nexit 0\n"); entry.chmod(0o755)
            (venv / "python").write_text("")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    public = install_bundle(tmp_path / "dest", wheelhouse, manifest)
    assert public.is_symlink()
    assert (public.resolve().parents[2] / "complete.json").is_file()
    assert calls[0][0:2] == ["uv", "venv"]
    assert any("--no-index" in call for call in calls)


def test_missing_offline_psutil_never_invokes_uv(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    manifest = _manifest(wheelhouse, ptest, _wheel(wheelhouse, "psutil", "7.2.2"))
    (wheelhouse / "psutil-7.2.2-py3-none-any.whl").unlink()
    called = []
    dest = tmp_path / "dest"
    before = _inventory(dest)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: called.append(args))
    with pytest.raises(FileNotFoundError):
        install_bundle(dest, wheelhouse, manifest)
    assert called == []
    assert _inventory(dest) == before


def test_wheel_replacement_race_is_rejected_before_provisioning(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    original = psutil.read_bytes()
    replaced = original + b"replacement"
    real_open = install.os.open
    switched = False

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal switched
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if not switched and dir_fd is not None and Path(path).name == psutil.name:
            switched = True
            psutil.unlink()
            psutil.write_bytes(replaced)
        return fd

    monkeypatch.setattr(install.os, "open", race_open)
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: pytest.fail("uv must not run"))
    with pytest.raises(ValueError, match="replaced"):
        install_bundle(tmp_path / "dest", wheelhouse, manifest)
    assert not (tmp_path / "dest" / "ptest").exists()


@pytest.mark.parametrize("mode", [0o702, 0o707])
def test_installer_rejects_writable_destination(mode, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir(mode=mode)
    dest.chmod(mode)
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    with pytest.raises(ValueError, match="private"):
        install_bundle(dest, wheelhouse, manifest)


def test_installer_rejects_hostile_bundle_ancestor(tmp_path):
    dest = tmp_path / "dest"; dest.mkdir()
    dest.chmod(0o700)
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    bundles = dest / ".ptest-bundles"
    bundles.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="bundle root"):
        install_bundle(dest, wheelhouse, manifest)


def test_installer_rejects_writable_destination_ancestor(tmp_path):
    parent = tmp_path / "hostile"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    dest = parent / "dest"
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    with pytest.raises(ValueError, match="ancestor"):
        install_bundle(dest, wheelhouse, manifest)


def test_real_subprocess_bundle_seeds_network_then_runs_offline(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"; wheelhouse.mkdir()
    build = tmp_path / "build"; build.mkdir()
    source_copy = tmp_path / "source-copy"; source_copy.mkdir()
    repo = Path(__file__).parents[2]
    shutil.copy2(repo / "pyproject.toml", source_copy / "pyproject.toml")
    shutil.copy2(repo / "uv.lock", source_copy / "uv.lock")
    shutil.copytree(repo / "src", source_copy / "src")
    ptest_version = tomllib.loads(
        (source_copy / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    built = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(build)], cwd=source_copy, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    ptest_wheel = next(build.glob("ptest_ng-*.whl"))
    shutil.copy2(ptest_wheel, wheelhouse / ptest_wheel.name)
    import json as _json
    with urllib.request.urlopen("https://pypi.org/pypi/psutil/7.2.2/json", timeout=30) as response:
        metadata = _json.loads(response.read())
    psutil = next(item for item in metadata["urls"] if "cp36-abi3-manylinux2010_x86_64" in item["filename"])
    manifest = wheelhouse / "manifest.json"
    platform_tag = "manylinux2010_x86_64"
    manifest.write_text(_json.dumps({"version": 1, "ptest_version": ptest_version, "python_tag": "cp36", "platform_tag": platform_tag, "wheels": [
        {"filename": ptest_wheel.name, "sha256": hashlib.sha256(ptest_wheel.read_bytes()).hexdigest(), "package": "ptest-ng", "version": ptest_version},
        {"filename": psutil["filename"], "sha256": psutil["digests"]["sha256"], "package": "psutil", "version": "7.2.2"},
    ]}))
    dest = tmp_path / "install"
    installer = Path(__file__).parents[2] / "install.sh"
    env = {**os.environ, "PTEST_NETWORK_SENTINEL": "real-network-seed"}
    seeded = subprocess.run([str(installer), "--dest", str(dest), "--wheelhouse", str(wheelhouse), "--manifest", str(manifest), "--allow-network"], cwd=Path(__file__).parents[2], env=env, capture_output=True, text=True, timeout=300)
    assert seeded.returncode == 0, seeded.stderr
    assert (dest / "ptest").is_symlink()
    assert subprocess.run([str(dest / "ptest"), "--version"], capture_output=True, text=True).returncode == 0
    assert subprocess.run([str(dest / "ptest"), "guide"], capture_output=True, text=True).returncode == 0
    seeded_wheel = next((dest / ".ptest-bundles").glob("*/wheels/psutil-*.whl"))
    bundle = seeded_wheel.parents[1]
    assert (dest / ".ptest-bundles").stat().st_mode & 0o777 == 0o700
    assert bundle.stat().st_mode & 0o777 == 0o700
    assert (bundle / "wheels").stat().st_mode & 0o777 == 0o700
    assert (bundle / "complete.json").stat().st_mode & 0o777 == 0o600
    assert seeded_wheel.stat().st_mode & 0o777 == 0o600
    shutil.copy2(seeded_wheel, wheelhouse / seeded_wheel.name)
    offline_env = {**env, "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}
    offline = subprocess.run([str(installer), "--dest", str(dest), "--wheelhouse", str(wheelhouse), "--manifest", str(manifest)], cwd=Path(__file__).parents[2], env=offline_env, capture_output=True, text=True, timeout=300)
    assert offline.returncode == 0, offline.stderr
    python = dest / "ptest"; python = python.resolve().parent / "python"
    probe = subprocess.run([str(python), "-c", "import psutil, ptest; print(psutil.__version__)"], capture_output=True, text=True)
    assert probe.returncode == 0 and "7.2.2" in probe.stdout
    old_target = (dest / "ptest").resolve()
    old_bundle = old_target.parents[2]
    for fault in ("after-one-wheel", "after-venv", "before-complete", "before-symlink-replace"):
        before = _inventory(dest)
        failed = subprocess.run([str(installer), "--dest", str(dest), "--wheelhouse", str(wheelhouse), "--manifest", str(manifest)], cwd=Path(__file__).parents[2], env={**offline_env, "PTEST_INSTALL_FAULT": fault}, capture_output=True, text=True, timeout=300)
        assert failed.returncode != 0
        assert (dest / "ptest").resolve() == old_target
        assert subprocess.run([str(dest / "ptest"), "--version"], capture_output=True, text=True).returncode == 0
        assert _inventory(dest) == before
        assert not tuple(dest.glob(".ptest-link-*"))
    before_parent = _inventory(dest)
    parent_failed = subprocess.run([str(installer), "--dest", str(dest), "--wheelhouse", str(wheelhouse), "--manifest", str(manifest)], cwd=Path(__file__).parents[2], env={**offline_env, "PTEST_INSTALL_FAULT": "parent-fsync"}, capture_output=True, text=True, timeout=300)
    assert parent_failed.returncode != 0
    assert (dest / "ptest").resolve().is_file()
    assert subprocess.run([str(dest / "ptest"), "--version"], capture_output=True, text=True).returncode == 0
    after_parent = _inventory(dest)
    new_bundles = set(after_parent[0]) - set(before_parent[0])
    assert len(new_bundles) == 1
    new_bundle = next(iter(new_bundles))
    assert set(after_parent[0]) == set(before_parent[0]) | {new_bundle}
    assert (dest / "ptest").resolve().parents[2].name == new_bundle
    assert old_bundle.name in before_parent[0]
    assert (old_bundle / "complete.json").is_file()
    assert subprocess.run([str(old_target), "--version"], capture_output=True, text=True).returncode == 0
    assert not after_parent[1]
    assert all((dest / ".ptest-bundles" / name / "complete.json").is_file() for name in after_parent[0])


def test_installed_ptest_package_exposes_doctor_checklist_resources():
    """Guide, recipes, and catalog must survive packaging, not just the source tree."""
    from importlib.resources import files

    package = files("ptest")
    guide = package.joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")
    assert "assessment authority only" in guide
    for name in ("factories", "databases", "cache", "files-ports", "processes", "time-network"):
        content = package.joinpath("resources", "recipes", name + ".md").read_text(encoding="utf-8")
        assert len(content.encode("utf-8")) > 0

    from ptest import checklist

    assert [entry.id for entry in checklist.CATALOG] == [
        "FIX-001", "FIX-002", "DB-001", "DB-002", "CACHE-001",
        "RESOURCE-001", "NETWORK-001", "PROCESS-001", "TIME-001",
        "SELECT-001", "TIMING-001", "PARALLEL-001",
    ]
