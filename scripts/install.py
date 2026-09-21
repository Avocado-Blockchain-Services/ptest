#!/usr/bin/env python3
"""Build an immutable, local-only ptest bundle and atomically publish it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import stat
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
    if len(parts) < 5 or (parts[-3] != python_tag and not (package == "ptest-ng" and parts[-3] == "py3")) or (platform_tag not in path.name[:-4] and not (package == "ptest-ng" and parts[-1] == "any")):
        raise ValueError("wheel tags do not match manifest")
    actual_name, actual_version, wheel_tags = _wheel_metadata(path)
    if actual_name.replace("_", "-").lower() != package or actual_version != version:
        raise ValueError("wheel metadata does not match manifest")
    compatible = any(tag.startswith(f"{python_tag}-") and platform_tag in tag for tag in wheel_tags)
    if not compatible and not (package == "ptest-ng" and "py3-none-any" in wheel_tags):
        raise ValueError("WHEEL Tag does not match manifest")


def _private_directory(path: Path, label: str, *, strict: bool = True) -> None:
    try:
        stamp = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode)
            or stamp.st_uid != os.getuid()
            or stamp.st_mode & (0o077 if strict else (stat.S_IWGRP | stat.S_IWOTH))):
        raise ValueError(f"{label} must be a private owned directory")


def _owned_destination(dest: Path) -> None:
    if not dest.is_absolute() or dest.is_symlink():
        raise ValueError("destination must be an absolute non-symlink directory")
    _validate_ancestor_chain(dest.parent)
    try:
        if not dest.exists():
            dest.mkdir(mode=0o700)
            os.chmod(dest, 0o700)
    except OSError as exc:
        raise ValueError("destination is unavailable") from exc
    _private_directory(dest, "destination")


def _validate_ancestor_chain(path: Path) -> None:
    """Reject redirectable ancestors while allowing the system temp anchor."""
    current = path
    while True:
        try:
            stamp = os.lstat(current)
        except OSError as exc:
            raise ValueError("destination ancestor is unavailable") from exc
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            raise ValueError("destination ancestor is unsafe")
        writable = stamp.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        # A root-owned sticky directory such as /tmp is the explicit safe
        # handoff point for a private destination; descendants are checked.
        if writable and not (stamp.st_mode & stat.S_ISVTX and stamp.st_uid == 0):
            raise ValueError("destination ancestor must be private")
        if current.parent == current or (writable and stamp.st_mode & stat.S_ISVTX):
            return
        current = current.parent


def _open_directory(path: Path, label: str, *, private: bool = True) -> int:
    _private_directory(path, label, strict=private)
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc


def _copy_authenticated_wheel(wheelhouse: Path, filename: str, staged_dir: Path,
                              expected_hash: str) -> Path:
    """Copy one source wheel through no-follow descriptors, then authenticate it."""
    wheel_fd = _open_directory(wheelhouse, "wheelhouse", private=False)
    staged_fd = _open_directory(staged_dir, "staged wheel directory")
    source_fd = output_fd = None
    staged = staged_dir / filename
    try:
        source_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW,
                            dir_fd=wheel_fd)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("wheel source is not a regular file")
        output_fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=staged_fd)
        digest = hashlib.sha256()
        while True:
            piece = os.read(source_fd, 1024 * 1024)
            if not piece:
                break
            digest.update(piece)
            view = memoryview(piece)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise OSError("wheel copy made no progress")
                view = view[written:]
        os.fsync(output_fd)
        after = os.fstat(source_fd)
        current = os.stat(filename, dir_fd=wheel_fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns) or \
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 before.st_ctime_ns) != (current.st_dev, current.st_ino,
                 current.st_size, current.st_mtime_ns, current.st_ctime_ns):
            raise ValueError("wheel source was replaced while copying")
        if digest.hexdigest() != expected_hash:
            raise ValueError("wheel hash does not match manifest")
        return staged
    except Exception:
        if output_fd is not None:
            try:
                os.unlink(filename, dir_fd=staged_fd)
            except OSError:
                pass
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)
        os.close(staged_fd)
        os.close(wheel_fd)


def _write_authenticated_bytes(staged_dir: Path, filename: str, payload: bytes,
                               expected_hash: str) -> Path:
    staged_fd = _open_directory(staged_dir, "staged wheel directory")
    fd = None
    try:
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                      os.O_NOFOLLOW, 0o600, dir_fd=staged_fd)
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError("downloaded wheel hash does not match manifest")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("wheel write made no progress")
            view = view[written:]
        os.fsync(fd)
        return staged_dir / filename
    except Exception:
        try:
            os.unlink(filename, dir_fd=staged_fd)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(staged_fd)


def _write_private_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    parent_fd = _open_directory(path.parent, "bundle")
    fd = None
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("marker write made no progress")
            view = view[written:]
        os.fsync(fd)
    except Exception:
        try:
            os.unlink(path.name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


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
    if bundles.exists() or bundles.is_symlink():
        _private_directory(bundles, "bundle root")
    else:
        bundles.mkdir(mode=0o700)
        _private_directory(bundles, "bundle root")
    bundle = bundles / f"{manifest['ptest_version']}-{secrets.token_hex(16)}"
    bundle.mkdir(mode=0o700)
    _private_directory(bundle, "bundle")
    wheels_dir = bundle / "wheels"
    wheels_dir.mkdir(mode=0o700)
    _private_directory(wheels_dir, "staged wheel directory")
    published = False
    link = None
    paths = []
    try:
      for entry in manifest["wheels"]:
        path = wheelhouse / entry["filename"]
        if path.parent != wheelhouse:
            raise ValueError("manifest wheel path must stay in wheelhouse")
        if path.is_symlink():
            raise ValueError("manifest wheel path must not be a symlink")
        if not path.is_file():
            if entry["package"] == "psutil" and allow_network:
                metadata_url = f"https://pypi.org/pypi/psutil/{entry['version']}/json"
                with urlopen(Request(metadata_url, headers={"Accept": "application/json"}), timeout=30) as response:  # nosec B310 - fixed HTTPS host
                    metadata = json.loads(response.read())
                candidates = [item for item in metadata.get("urls", []) if item.get("filename") == entry["filename"]]
                if len(candidates) != 1 or urlparse(candidates[0].get("url", "")).netloc != "files.pythonhosted.org":
                    raise ValueError("official psutil wheel was not uniquely resolved")
                with urlopen(Request(candidates[0]["url"], headers={"Accept": "application/octet-stream"}), timeout=30) as response:  # nosec B310 - verified files.pythonhosted.org host
                    payload = response.read()
                if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                    raise ValueError("downloaded psutil wheel hash does not match manifest")
                path = _write_authenticated_bytes(wheels_dir, entry["filename"],
                                                   payload, entry["sha256"])
            else:
                raise FileNotFoundError(path)
        else:
            path = _copy_authenticated_wheel(
                wheelhouse, entry["filename"], wheels_dir, entry["sha256"])
        validate_wheel(path, entry["package"], entry["version"],
                       manifest["python_tag"], manifest["platform_tag"])
        paths.append(path)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        if bundles.exists() and not any(bundles.iterdir()):
            bundles.rmdir()
        raise
    try:
        bundled_paths = list(paths)
        if fault == "after-one-wheel":
            raise RuntimeError("after-one-wheel")
        subprocess.run(["uv", "venv", "--python", str(py), str(bundle / "venv")], check=True, env={**os.environ, "UV_OFFLINE": "true", "UV_PYTHON_DOWNLOADS": "never"}, timeout=300)
        if fault == "after-venv":
            raise RuntimeError("after-venv")
        subprocess.run(["uv", "pip", "install", "--python", str(bundle / "venv" / "bin" / "python"), "--no-index", "--no-deps", *(str(p) for p in bundled_paths)], check=True, env={**os.environ, "UV_OFFLINE": "true", "UV_PYTHON_DOWNLOADS": "never"}, timeout=300)
        subprocess.run([str(bundle / "venv" / "bin" / "ptest"), "--version"], check=True, timeout=30)
        subprocess.run([str(bundle / "venv" / "bin" / "ptest"), "guide"], check=True, timeout=30)
        subprocess.run([str(bundle / "venv" / "bin" / "python"), "-c", "from importlib.resources import files; p=files('ptest'); assert p.joinpath('runtime/vitest_bridge.mjs').is_file(); assert p.joinpath('runtime/protocol-v1.json').is_file(); assert p.joinpath('resources/agent-guide.md').is_file(); assert any(p.joinpath('resources/recipes').iterdir())"], check=True, timeout=30)
        marker = {"version": 1, "bundle_id": bundle.name, "ptest_version": manifest["ptest_version"], "python_version": ".".join(map(str, sys.version_info[:3])), "wheel_sha256s": [e["sha256"] for e in manifest["wheels"]], "entrypoint": "venv/bin/ptest"}
        marker_path = bundle / "complete.json"
        if fault == "before-complete":
            raise RuntimeError("before-complete")
        _write_private_json(marker_path, marker)
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
        if fault == "before-symlink-replace":
            raise RuntimeError("before-symlink-replace")
        os.replace(link, public)
        published = True
        if fault == "after-swap-before-fsync":
            raise RuntimeError("after-swap-before-fsync")
        parent_fd = os.open(dest, os.O_RDONLY)
        try:
            if fault == "parent-fsync":
                raise OSError("parent-fsync")
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return public
    except Exception:
        if link is not None and link.is_symlink():
            link.unlink()
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
    install_bundle(args.dest, args.wheelhouse, args.manifest, allow_network=args.allow_network, fault=os.environ.get("PTEST_INSTALL_FAULT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
