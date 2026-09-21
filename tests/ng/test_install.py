from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
import sys
import os
import subprocess
import urllib.request

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


def test_manifest_requires_exact_pinned_pair(tmp_path):
    with pytest.raises(ValueError, match="exactly one ptest-ng and one psutil"):
        validate_manifest({"version": 1, "ptest_version": "0.1.0", "python_tag": "py3", "platform_tag": "any", "wheels": []})


def test_wheel_rejects_sdist_and_path_escape(tmp_path):
    with pytest.raises(ValueError, match="wheel filename"):
        validate_wheel(Path("../evil.tar.gz"), "ptest-ng", "0.1.0", "py3", "any")


def test_failed_upgrade_keeps_old_target(tmp_path):
    wheelhouse = tmp_path / "wheels"; wheelhouse.mkdir()
    ptest = _wheel(wheelhouse, "ptest-ng")
    psutil = _wheel(wheelhouse, "psutil", "7.2.2")
    manifest = _manifest(wheelhouse, ptest, psutil)
    old = tmp_path / "old"; old.mkdir()
    current = tmp_path / "ptest"
    current.symlink_to(old, target_is_directory=True)
    with pytest.raises(RuntimeError, match="before-symlink-replace"):
        install_bundle(tmp_path, wheelhouse, manifest, fault="before-symlink-replace")
    assert current.resolve() == old.resolve()


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
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: called.append(args))
    with pytest.raises(FileNotFoundError):
        install_bundle(tmp_path / "dest", wheelhouse, manifest)
    assert called == []


def test_real_subprocess_bundle_seeds_network_then_runs_offline(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"; wheelhouse.mkdir()
    build = tmp_path / "build"; build.mkdir()
    built = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(build)], cwd=Path(__file__).parents[2], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    ptest_wheel = next(build.glob("ptest_ng-*.whl"))
    import shutil
    shutil.copy2(ptest_wheel, wheelhouse / ptest_wheel.name)
    import json as _json
    with urllib.request.urlopen("https://pypi.org/pypi/psutil/7.2.2/json", timeout=30) as response:
        metadata = _json.loads(response.read())
    psutil = next(item for item in metadata["urls"] if "cp36-abi3-manylinux2010_x86_64" in item["filename"])
    manifest = wheelhouse / "manifest.json"
    platform_tag = "manylinux2010_x86_64"
    manifest.write_text(_json.dumps({"version": 1, "ptest_version": "0.1.0", "python_tag": "cp36", "platform_tag": platform_tag, "wheels": [
        {"filename": ptest_wheel.name, "sha256": hashlib.sha256(ptest_wheel.read_bytes()).hexdigest(), "package": "ptest-ng", "version": "0.1.0"},
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
    shutil.copy2(seeded_wheel, wheelhouse / seeded_wheel.name)
    offline_env = {**env, "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}
    offline = subprocess.run([str(installer), "--dest", str(dest), "--wheelhouse", str(wheelhouse), "--manifest", str(manifest)], cwd=Path(__file__).parents[2], env=offline_env, capture_output=True, text=True, timeout=300)
    assert offline.returncode == 0, offline.stderr
    python = dest / "ptest"; python = python.resolve().parent / "python"
    probe = subprocess.run([str(python), "-c", "import psutil, ptest; print(psutil.__version__)"], capture_output=True, text=True)
    assert probe.returncode == 0 and "7.2.2" in probe.stdout
