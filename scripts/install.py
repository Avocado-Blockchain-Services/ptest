#!/usr/bin/env python3
"""Build an immutable, local-only ptest bundle and atomically publish it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import zipfile
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PYTHON_MIN, PYTHON_MAX = (3, 11), (3, 15)
MANIFEST_KEYS = {"version", "ptest_version", "python_tag", "platform_tag", "wheels"}
WHEEL_KEYS = {"filename", "sha256", "package", "version"}


def _plain_string(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"invalid manifest {name}")
    return value


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS or manifest.get("version") != 1:
        raise ValueError("manifest must be exact version 1")
    for key in ("ptest_version", "python_tag", "platform_tag"):
        _plain_string(manifest[key], key)
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", manifest["ptest_version"]):
        raise ValueError("ptest_version is unsafe for a bundle id")
    wheels = manifest["wheels"]
    if not isinstance(wheels, list) or len(wheels) != 2:
        raise ValueError("manifest requires exactly one ptest-ng and one psutil")
    packages = []
    for entry in wheels:
        if not isinstance(entry, dict) or set(entry) != WHEEL_KEYS:
            raise ValueError("wheel entry has unexpected fields")
        filename = _plain_string(entry["filename"], "filename")
        if Path(filename).name != filename or not filename.endswith(".whl"):
            raise ValueError("wheel filename must be a local wheel basename")
        digest = entry["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("wheel sha256 must be lowercase hex")
        package = entry["package"]
        if package not in ("ptest-ng", "psutil"):
            raise ValueError("unsupported wheel package")
        _plain_string(entry["version"], "wheel version")
        if package == "psutil" and entry["version"] != "7.2.2":
            raise ValueError("psutil wheel must be exactly version 7.2.2")
        packages.append(package)
    if set(packages) != {"ptest-ng", "psutil"} or len(packages) != 2:
        raise ValueError("manifest requires exactly one ptest-ng and one psutil")
    if manifest["ptest_version"] != next(e["version"] for e in wheels if e["package"] == "ptest-ng"):
        raise ValueError("ptest_version does not match wheel")
    return manifest


def _wheel_metadata(path: Path) -> tuple[str, str, set[str]]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(Path(n).is_absolute() or ".." in Path(n).parts for n in names):
            raise ValueError("wheel contains path escape")
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        wheel_info = [n for n in names if n.endswith(".dist-info/WHEEL")]
        if len(metadata) != 1:
            raise ValueError("wheel metadata is missing or ambiguous")
        fields = {}
        for line in archive.read(metadata[0]).decode("utf-8").splitlines():
            if ": " in line:
                key, value = line.split(": ", 1); fields[key] = value
        tags = set()
        for line in archive.read(wheel_info[0]).decode("utf-8").splitlines() if len(wheel_info) == 1 else ():
            if line.startswith("Tag: "):
                tags.add(line[5:])
        return fields.get("Name", ""), fields.get("Version", ""), tags


def validate_wheel(path: Path, package: str, version: str, python_tag: str, platform_tag: str) -> None:
    if path.name != Path(path.name).name or path.suffix != ".whl":
        raise ValueError("wheel filename must be a wheel filename")
    parts = path.name[:-4].split("-")
    if len(parts) < 5 or parts[-3] != python_tag or parts[-1] != platform_tag:
        raise ValueError("wheel tags do not match manifest")
    actual_name, actual_version, wheel_tags = _wheel_metadata(path)
    if actual_name.replace("_", "-").lower() != package or actual_version != version:
        raise ValueError("wheel metadata does not match manifest")
    if f"{python_tag}-none-{platform_tag}" not in wheel_tags:
        raise ValueError("WHEEL Tag does not match manifest")


def _owned_destination(dest: Path) -> None:
    if not dest.is_absolute() or dest.is_symlink():
        raise ValueError("destination must be an absolute non-symlink directory")
    dest.mkdir(parents=True, exist_ok=True)
    if dest.stat().st_uid != os.getuid():
        raise ValueError("destination is not owned by the current account")


def install_bundle(dest: Path, wheelhouse: Path, manifest_path: Path, *, allow_network=False, fault=None) -> Path:
    _owned_destination(dest)
    if not wheelhouse.is_absolute() or not manifest_path.is_absolute():
        raise ValueError("wheelhouse and manifest must be absolute paths")
    if wheelhouse.is_symlink() or not wheelhouse.is_dir() or manifest_path.is_symlink():
        raise ValueError("wheelhouse must be a real directory")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    py = Path(sys.executable)
    if platform.python_implementation() != "CPython" or not (PYTHON_MIN <= sys.version_info[:2] < PYTHON_MAX):
        raise RuntimeError("installer requires CPython 3.11 through 3.14")
    bundles = dest / ".ptest-bundles"
    if bundles.is_symlink() or (bundles.exists() and not bundles.is_dir()):
        raise ValueError("bundle root is not an owned directory")
    bundles.mkdir(exist_ok=True)
    bundle = bundles / f"{manifest['ptest_version']}-{secrets.token_hex(16)}"
    bundle.mkdir()
    (bundle / "wheels").mkdir()
    published = False
    paths = []
    for entry in manifest["wheels"]:
        path = wheelhouse / entry["filename"]
        if path.is_symlink():
            raise ValueError("manifest wheel path must not be a symlink")
        if path.parent != wheelhouse or not path.is_file():
            if entry["package"] == "psutil" and allow_network:
                metadata_url = f"https://pypi.org/pypi/psutil/{entry['version']}/json"
                with urlopen(Request(metadata_url, headers={"Accept": "application/json"}), timeout=30) as response:
                    metadata = json.loads(response.read())
                candidates = [item for item in metadata.get("urls", []) if item.get("filename") == entry["filename"]]
                if len(candidates) != 1 or urlparse(candidates[0].get("url", "")).netloc != "files.pythonhosted.org":
                    raise ValueError("official psutil wheel was not uniquely resolved")
                with urlopen(Request(candidates[0]["url"], headers={"Accept": "application/octet-stream"}), timeout=30) as response:
                    payload = response.read()
                if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                    raise ValueError("downloaded psutil wheel hash does not match manifest")
                staged = bundle / "wheels" / entry["filename"]
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                path = staged
            else:
                raise FileNotFoundError(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("wheel hash does not match manifest")
        validate_wheel(path, entry["package"], entry["version"], manifest["python_tag"], manifest["platform_tag"])
        paths.append(path)
    try:
        bundled_paths = []
        for path in paths:
            bundled = path if path.parent == bundle / "wheels" else bundle / "wheels" / path.name
            if bundled != path:
                shutil.copy2(path, bundled)
            bundled_paths.append(bundled)
        if fault == "before-symlink-replace":
            raise RuntimeError("before-symlink-replace")
        subprocess.run(["uv", "venv", "--python", str(py), str(bundle / "venv")], check=True, env={**os.environ, "UV_OFFLINE": "true", "UV_PYTHON_DOWNLOADS": "never"}, timeout=300)
        subprocess.run(["uv", "pip", "install", "--python", str(bundle / "venv" / "bin" / "python"), "--no-index", "--no-deps", *(str(p) for p in bundled_paths)], check=True, env={**os.environ, "UV_OFFLINE": "true", "UV_PYTHON_DOWNLOADS": "never"}, timeout=300)
        subprocess.run([str(bundle / "venv" / "bin" / "ptest"), "--version"], check=True, timeout=30)
        subprocess.run([str(bundle / "venv" / "bin" / "ptest"), "guide"], check=True, timeout=30)
        marker = {"version": 1, "bundle_id": bundle.name, "ptest_version": manifest["ptest_version"], "python_version": ".".join(map(str, sys.version_info[:3])), "wheel_sha256s": [e["sha256"] for e in manifest["wheels"]], "entrypoint": "venv/bin/ptest"}
        marker_path = bundle / "complete.json"
        marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        with marker_path.open("rb") as marker_file:
            os.fsync(marker_file.fileno())
        directory_fd = os.open(bundle, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        link = dest / f".ptest-link-{secrets.token_hex(8)}"
        link.symlink_to(Path(".ptest-bundles") / bundle.name / "venv" / "bin" / "ptest")
        public = dest / "ptest"
        if public.exists() and not public.is_symlink():
            raise ValueError("existing destination is not an installer symlink")
        if public.is_symlink():
            target = public.resolve(strict=False)
            if bundles not in target.parents:
                raise ValueError("existing symlink target is outside installer bundles")
        os.replace(link, public)
        published = True
        if fault == "after-swap-before-fsync":
            raise RuntimeError("after-swap-before-fsync")
        parent_fd = os.open(dest, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return public
    except Exception:
        if not published:
            shutil.rmtree(bundle, ignore_errors=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args(argv)
    install_bundle(args.dest, args.wheelhouse, args.manifest, allow_network=args.allow_network)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
