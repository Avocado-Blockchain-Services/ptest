#!/usr/bin/env python3
"""Run local security gates; an unavailable or insensitive detector is failure."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import hashlib
import os
import platform
import tarfile
from urllib.request import Request, urlopen
from pathlib import Path

TOOLS = ("bandit", "pip-audit", "gitleaks")
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_CHECKSUM_FILE_SHA256 = "061476c21adaf5441516f96f185c1a4706a83cd6329b9b38762271b3d4a52fae"
GITLEAKS_ARCHIVE_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_BINARY_SHA256 = "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509"


def provision_gitleaks(destination: Path) -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("pinned provisioning currently supports Linux x86_64 only")
    base = f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}"
    archive_name = f"gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz"
    with urlopen(Request(f"{base}/gitleaks_{GITLEAKS_VERSION}_checksums.txt"), timeout=30) as response:  # nosec B310 - fixed GitHub release host
        checksums = response.read()
    if hashlib.sha256(checksums).hexdigest() != GITLEAKS_CHECKSUM_FILE_SHA256:
        raise RuntimeError("official Gitleaks checksum file hash mismatch")
    expected = next((line.split()[0] for line in checksums.decode().splitlines() if line.endswith(archive_name)), None)
    if expected != GITLEAKS_ARCHIVE_SHA256:
        raise RuntimeError("pinned Gitleaks archive checksum mismatch")
    with urlopen(Request(f"{base}/{archive_name}"), timeout=30) as response:  # nosec B310 - fixed GitHub release host
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != expected:
        raise RuntimeError("downloaded Gitleaks archive hash mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gitleaks-", dir=destination.parent) as temp:
        archive_path = Path(temp) / archive_name
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as bundle:
            members = [member for member in bundle.getmembers() if member.name == "gitleaks" and member.isfile()]
            if len(members) != 1:
                raise RuntimeError("Gitleaks archive did not contain exactly one binary")
            bundle.extract(members[0], Path(temp))
            staged = Path(temp) / "gitleaks"
            target = destination / "gitleaks"
            os.replace(staged, target)
            target.chmod(0o755)
            (destination / "gitleaks.sha256").write_text(GITLEAKS_BINARY_SHA256 + "  gitleaks\n", encoding="ascii")
    return destination / "gitleaks"


def _gitleaks_binary(root: Path) -> Path | None:
    candidate = root / ".tools" / "gitleaks" / "gitleaks"
    checksum = candidate.with_name("gitleaks.sha256")
    if not candidate.is_file() or not os.access(candidate, os.X_OK) or not checksum.is_file():
        return None
    expected = checksum.read_text(encoding="ascii").split()[0]
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return candidate if expected == GITLEAKS_BINARY_SHA256 and actual == expected else None


def run_gate(root: Path, tool: str) -> dict:
    executable = str(_gitleaks_binary(root)) if tool == "gitleaks" and _gitleaks_binary(root) else shutil.which(tool) if tool != "gitleaks" else None
    if not executable:
        return {"tool": tool, "status": "unpassed", "reason": "unavailable"}
    if tool == "gitleaks":
        version = subprocess.run([executable, "version"], cwd=root, capture_output=True, text=True)
        if version.returncode != 0 or GITLEAKS_VERSION not in version.stdout:
            return {"tool": tool, "status": "unpassed", "reason": "version-mismatch"}
    with tempfile.TemporaryDirectory(prefix="ptest-security-") as temp:
        fixture = Path(temp) / "sensitivity.py"
        fixture.write_text("password = 'T13SYNTH-ABCDEFGHIJKLMNOPQRSTUVWXYZ'\n")
        negative = Path(temp) / "negative.txt"
        negative.write_text("T13SYNTH-not-a-match\n")
        if tool == "bandit":
            probe = [executable, "-q", "-r", str(fixture)]
            scope = [executable, "-q", "-r", str(root / "src"), str(root / "scripts" / "install.py"), str(root / "scripts" / "security-checks.py"), "-ll", "-ii", "--skip", "B608"]
        elif tool == "pip-audit":
            requirements = Path(temp) / "requirements.txt"
            requirements.write_text("jinja2==2.10\n")
            probe = [executable, "--requirement", str(requirements)]
            locked = Path(temp) / "locked.txt"
            exported = subprocess.run(["uv", "export", "--locked", "--no-emit-project", "--format", "requirements-txt", "--output-file", str(locked)], cwd=root, capture_output=True, text=True)
            if exported.returncode != 0:
                return {"tool": tool, "status": "unpassed", "reason": "lock-export-failed"}
            scope = [executable, "--requirement", str(locked)]
        else:
            config = Path(temp) / "sensitivity.toml"
            config.write_text('''title = "Task 13 sensitivity fixture"\n[[rules]]\nid = "task13-synthetic"\nregex = "T13SYNTH-[A-Z]{26}"\nsecretGroup = 0\n''')
            probe = [executable, "detect", "--source", temp, "--no-git", "--config", str(config), "--no-banner"]
            scope = [executable, "detect", "--source", str(root), "--no-git", "--config", str(root / "scripts" / "gitleaks.toml"), "--no-banner"]
        sensitivity = subprocess.run(probe, cwd=root, capture_output=True, text=True)
        evidence = (getattr(sensitivity, "stdout", "") or "") + (getattr(sensitivity, "stderr", "") or "")
        expected_finding = ("Issue" in evidence if tool == "bandit" else "VULNERABILITY" in evidence or "vulnerabilit" in evidence.lower() if tool == "pip-audit" else "leaks found" in evidence.lower())
        if sensitivity.returncode == 0 or not expected_finding:
            return {"tool": tool, "status": "unpassed", "reason": "sensitivity-failed"}
        if tool == "gitleaks":
            negative_result = subprocess.run([executable, "detect", "--source", str(negative), "--no-git", "--config", str(config), "--no-banner"], cwd=root, capture_output=True, text=True)
            if negative_result.returncode != 0:
                return {"tool": tool, "status": "unpassed", "reason": "negative-sensitivity-failed"}
        result = subprocess.run(scope, cwd=root, capture_output=True, text=True)
        if tool == "gitleaks" and result.returncode == 0:
            history = subprocess.run([executable, "git", "--log-opts=--all", "--config", str(root / "scripts" / "gitleaks.toml"), "--no-banner"], cwd=root, capture_output=True, text=True)
            if history.returncode != 0:
                return {"tool": tool, "status": "unpassed", "reason": "history-finding"}
        return {"tool": tool, "status": "passed" if result.returncode == 0 else "unpassed", "returncode": result.returncode}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provision-gitleaks", action="store_true")
    args = parser.parse_args(argv)
    if args.provision_gitleaks:
        print(provision_gitleaks(args.root / ".tools" / "gitleaks"))
        return 0
    results = [run_gate(args.root, tool) for tool in TOOLS]
    print(json.dumps({"version": 1, "gates": results}, sort_keys=True))
    return 0 if all(item["status"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
