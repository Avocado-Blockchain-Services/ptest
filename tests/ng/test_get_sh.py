"""get.sh, the one-line macOS/Linux installer: platform, prerequisite and
checksum refusals. Tools are PATH shims; downloads use file:// URLs."""
from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

GET_SH = Path(__file__).parents[2] / "get.sh"


def _shim(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


def _env(tmp_path: Path, *, system: str = "Linux", machine: str = "x86_64",
         with_uv: bool = True) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("curl", "tar", "gzip", "awk", "sha256sum", "mktemp", "rm", "cat", "dirname"):
        real = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True,
                              text=True).stdout.strip()
        if real:
            (bin_dir / tool).symlink_to(real)
    _shim(bin_dir, "uname", f'[ "$1" = "-s" ] && echo {system} || echo {machine}')
    if with_uv:
        # A fake interpreter that records it was handed to install.sh.
        _shim(bin_dir, "uv", f'echo {tmp_path}/python')
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": str(bin_dir), "HOME": str(home), "TMPDIR": str(tmp_path),
            "PTEST_VERSION": "9.9.9"}


def _release(tmp_path: Path, *, checksum: str | None = None) -> str:
    out = tmp_path / "release"
    out.mkdir()
    asset = out / "ptest-9.9.9-linux-x86_64.tar.gz"
    script = b"#!/bin/sh\necho \"installer ran with $PTEST_PYTHON\" > \"$HOME/ran\"\n"
    with tarfile.open(asset, "w:gz") as archive:
        info = tarfile.TarInfo("ptest-9.9.9/install.sh")
        info.size = len(script)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(script))
    digest = checksum or hashlib.sha256(asset.read_bytes()).hexdigest()
    (out / f"{asset.name}.sha256").write_text(f"{digest}  {asset.name}\n", encoding="utf-8")
    return f"file://{out}"


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/sh", str(GET_SH)], env=env, capture_output=True,
                          text=True, timeout=30)


def test_verified_bundle_runs_its_installer_with_the_found_python(tmp_path):
    env = _env(tmp_path)
    env["PTEST_BASE_URL"] = _release(tmp_path)

    completed = _run(env)

    assert "downloading ptest 9.9.9 for linux-x86_64" in completed.stderr
    ran = Path(env["HOME"]) / "ran"
    assert ran.read_text() == f"installer ran with {tmp_path}/python\n"


def test_checksum_mismatch_refuses_before_installing(tmp_path):
    env = _env(tmp_path)
    env["PTEST_BASE_URL"] = _release(tmp_path, checksum="0" * 64)

    completed = _run(env)

    assert completed.returncode == 1
    assert "checksum mismatch" in completed.stderr
    assert not (Path(env["HOME"]) / "ran").exists()


@pytest.mark.parametrize("system,machine,message", [
    ("MINGW64_NT-10.0", "x86_64", "unsupported OS"),
    ("Linux", "riscv64", "unsupported CPU"),
])
def test_unsupported_platform_is_refused(tmp_path, system, machine, message):
    env = _env(tmp_path, system=system, machine=machine)
    env["PTEST_BASE_URL"] = _release(tmp_path)

    completed = _run(env)

    assert completed.returncode == 1
    assert message in completed.stderr


def test_missing_uv_names_the_uv_installer(tmp_path):
    env = _env(tmp_path, with_uv=False)

    completed = _run(env)

    assert completed.returncode == 1
    assert "astral.sh/uv/install.sh" in completed.stderr


@pytest.mark.parametrize("system,machine,suffix", [
    ("Darwin", "arm64", "macos-arm64"),
    ("Darwin", "x86_64", "macos-x86_64"),
    ("Linux", "aarch64", "linux-aarch64"),
])
def test_platform_selects_its_asset(tmp_path, system, machine, suffix):
    env = _env(tmp_path, system=system, machine=machine)
    env["PTEST_BASE_URL"] = f"file://{tmp_path}/missing"

    completed = _run(env)

    assert f"ptest-9.9.9-{suffix}.tar.gz" in completed.stderr
