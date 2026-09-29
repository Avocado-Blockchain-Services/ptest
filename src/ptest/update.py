"""Startup update check and `ptest update`.

Latest-version resolution follows the redirect of the GitHub releases page
(no token), the startup check caches once per 24 h in the state area with a
hard 2 s wall-clock bound, and `ptest update` downloads the release bundle
over HTTPS from the fixed GitHub host, verifies SHA-256 before anything
from the bundle runs, extracts safely, and installs side by side through
the bundled ``install.sh`` exactly as ``get.sh`` does.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - argv-only execution, never a shell
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Sequence, TextIO

from . import contracts as C
from . import files
from . import platform as platform_api
from . import uninstall

REPOSITORY = "Avocado-Blockchain-Services/ptest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_URL = RELEASES_URL + "/latest"
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
CHECK_INTERVAL_S = 24 * 3600
CHECK_TIMEOUT_S = 2.0
RESOLVE_TIMEOUT_S = 10.0
DOWNLOAD_TIMEOUT_S = 300.0
INSTALL_TIMEOUT_S = 900
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_SHA_BYTES = 4096
MAX_EXTRACT_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
CACHE_NAME = "update-check.json"
CACHE_MAX_BYTES = 4096
SOURCE_HINT = "installed from source; update it with git pull"

_PHASE = "update"

_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+", re.ASCII)
_TAG_RE = re.compile(
    r"https://github\.com/Avocado-Blockchain-Services/ptest"
    r"/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)", re.ASCII)
_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)

_execv = os.execv


def _problem(code: str, message: str, *, retryable: bool = False) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE,
                     retryable=retryable)


def _unavailable(message: str = "could not reach the ptest releases on GitHub"
               ) -> C.Problem:
    return _problem("update-unavailable", message, retryable=True)


def _failed(message: str) -> C.Problem:
    return _problem("update-failed", message)


@dataclass(frozen=True, slots=True)
class Transport:
    latest_location: Callable[[float], str]
    download: Callable[[str, BinaryIO, int, float], None]


@dataclass(frozen=True, slots=True)
class Layout:
    root: Path
    bundle_id: str


@dataclass(frozen=True, slots=True)
class UpdateResult:
    running_version: str
    target_version: str
    action: str
    check_only: bool
    install_root: Path | None


def valid_version(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    if len(text) > 32 or len(text) == 0:
        return None
    if _VERSION_RE.fullmatch(text) is None:
        return None
    return text


def is_newer(candidate: str, current: str) -> bool:
    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(piece) for piece in version.split("."))
    return parts(candidate) > parts(current)


def platform_suffix() -> str:
    name = os.uname()
    if name.sysname == "Linux":
        os_name = "linux"
    elif name.sysname == "Darwin":
        os_name = "macos"
    else:
        raise _failed(f"unsupported OS {name.sysname}: "
                      "ptest supports macOS and Linux")
    machine = name.machine
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64" if os_name == "macos" else "aarch64"
    else:
        raise _failed(f"unsupported CPU {machine}: "
                      "ptest supports x86_64 and arm64")
    return f"{os_name}-{arch}"


def _url_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        userinfo = (parsed.username is not None
                    or parsed.password is not None)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if userinfo:
        return False
    if host not in ALLOWED_HOSTS:
        return False
    if port is not None and port != 443:
        return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture the redirect target without following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_location(url: str, timeout_s: float) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="HEAD")
    try:
        with opener.open(request, timeout=timeout_s) as response:
            location = response.headers.get("Location")
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        if location is None:
            raise _unavailable() from None
        final_url = url
    except (OSError, ValueError):
        raise _unavailable() from None
    if not location:
        raise _unavailable()
    return urllib.parse.urljoin(final_url, location)


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Follow at most five redirects, each inside the HTTPS allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if getattr(req, "_update_redirects", 0) >= 5:
            return None
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not _url_allowed(target):
            return None
        redirected = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected._update_redirects = (  # type: ignore[attr-defined]
                getattr(req, "_update_redirects", 0) + 1)
        return redirected


def _stream_download(url: str, sink: BinaryIO, max_bytes: int,
                     timeout_s: float) -> None:
    if not _url_allowed(url):
        raise _unavailable()
    opener = urllib.request.build_opener(_AllowlistRedirect)
    request = urllib.request.Request(url, method="GET")
    deadline = time.monotonic() + timeout_s
    try:
        response = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _failed(_missing_release_message(url)) from None
        raise _unavailable() from None
    except (OSError, ValueError):
        raise _unavailable() from None
    try:
        if not _url_allowed(response.geturl()):
            raise _unavailable()
        if getattr(response, "status", 200) in (301, 302, 303, 307, 308):
            # A redirect the handler refused (foreign host or too many):
            # never follow it, never treat its body as the bundle.
            raise _unavailable()
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length.strip()) > max_bytes:
                    raise _failed("download exceeds size bound")
            except (TypeError, ValueError):
                raise _failed("download exceeds size bound") from None
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise _unavailable()
            try:
                chunk = response.read(min(65536, max_bytes - total + 1))
            except OSError:
                raise _unavailable() from None
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _failed("download exceeds size bound")
            sink.write(chunk)
    finally:
        try:
            response.close()
        except OSError:
            pass


def _missing_release_message(url: str) -> str:
    try:
        path = urllib.parse.urlsplit(url).path
        version = path.split("/download/v", 1)[1].split("/", 1)[0]
        asset = path.rsplit("/", 1)[1]
        suffix = asset.split(f"ptest-{version}-", 1)[1].rsplit(".tar.gz", 1)[0]
        if valid_version(version) is not None and suffix:
            return (f"no ptest {version} release for {suffix}; "
                    "the current install is unchanged")
    except (IndexError, ValueError):
        pass
    return ("the ptest release download is missing; "
            "the current install is unchanged")


def default_transport() -> Transport:
    return Transport(latest_location=_read_location,
                     download=_stream_download)


def resolve_latest(transport: Transport | None = None,
                   *, timeout_s: float) -> str:
    active = transport if transport is not None else default_transport()
    try:
        location = active.latest_location(timeout_s)
    except C.Problem:
        raise
    except Exception:
        raise _unavailable() from None
    if not isinstance(location, str):
        raise _unavailable()
    match = _TAG_RE.fullmatch(location.strip())
    if match is None:
        raise _unavailable()
    version = valid_version(match.group(1))
    if version is None:
        raise _unavailable()
    return version


def detect_layout(module_file: str | None = None) -> Layout | None:
    source = module_file if module_file is not None else __file__
    try:
        real = Path(os.path.realpath(source))
    except OSError:
        return None
    parts = real.parts
    if ".ptest-bundles" not in parts:
        from_binary = uninstall._root_from_binary(source)
        if from_binary is None:
            return None
        return _check_bundle_root(from_binary)
    root = Path(*parts[:parts.index(".ptest-bundles")])
    return _check_bundle_root(root)


def _check_bundle_root(root: Path) -> Layout | None:
    bundles = root / ".ptest-bundles"
    try:
        names = sorted(os.listdir(bundles))
    except OSError:
        return None
    public = root / "ptest"
    try:
        resolved = Path(os.path.realpath(public))
    except OSError:
        return None
    for name in names:
        if not uninstall._is_bundle_dir(bundles, name):
            continue
        if resolved == bundles / name or (bundles / name) in resolved.parents:
            return Layout(root=root, bundle_id=name)
    if not uninstall._public_link_in_bundles(root):
        return None
    return None


def _bounded_copy(source: BinaryIO, target: Path, max_bytes: int) -> None:
    """Copy at most ``max_bytes`` into an exclusively created 0600 file.

    A target that already exists is rejected and left untouched: only a
    file this call created is ever unlinked on failure.
    """
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise _failed("cannot stage download") from None
    try:
        total = 0
        while True:
            chunk = source.read(min(65536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _failed("download exceeds size bound")
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                view = view[written:]
    except C.Problem:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _download_to(transport: Transport, url: str, target: Path,
                 max_bytes: int, timeout_s: float) -> None:
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise _failed("cannot stage download") from None
    try:
        with os.fdopen(fd, "wb") as sink:
            try:
                transport.download(url, sink, max_bytes, timeout_s)
            except C.Problem:
                raise
            except Exception:
                raise _unavailable() from None
    except C.Problem:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def _parse_sha256(raw: bytes, asset_name: str) -> str:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise _failed(f"checksum mismatch for {asset_name}; "
                      "the current install is unchanged") from None
    first = text.split("\n", 1)[0].strip().split()
    if not first or _HEX64_RE.fullmatch(first[0]) is None:
        raise _failed(f"checksum mismatch for {asset_name}; "
                      "the current install is unchanged")
    if len(first) > 1 and first[1] not in (asset_name, "*" + asset_name):
        raise _failed(f"checksum mismatch for {asset_name}; "
                      "the current install is unchanged")
    return first[0]


def _check_member(top: str, member: tarfile.TarInfo) -> list[str]:
    name = member.name
    if not name or "\x00" in name:
        raise _failed(f"unsafe archive member {name!r}; "
                      "the current install is unchanged")
    if os.path.isabs(name):
        raise _failed(f"unsafe archive member {name!r}; "
                      "the current install is unchanged")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _failed(f"unsafe archive member {name!r}; "
                      "the current install is unchanged")
    if parts[0] != top:
        raise _failed(f"unsafe archive member {name!r}; "
                      "the current install is unchanged")
    if not (member.isreg() or member.isdir()):
        raise _failed(f"unsafe archive member {name!r}; "
                      "the current install is unchanged")
    return parts


def _safe_extract(archive: Path, dest: Path, version: str) -> Path:
    top = f"ptest-{version}"
    total = 0
    count = 0
    seen: set[str] = set()
    try:
        tar = tarfile.open(archive, mode="r:gz")
    except (tarfile.TarError, OSError):
        raise _failed(f"cannot read ptest {version} bundle; "
                      "the current install is unchanged") from None
    with tar:
        for member in tar:
            count += 1
            if count > MAX_MEMBERS:
                raise _failed(f"unsafe archive with too many members; "
                              "the current install is unchanged")
            parts = _check_member(top, member)
            if member.name in seen:
                raise _failed(f"unsafe duplicate archive member {member.name!r}; "
                              "the current install is unchanged")
            seen.add(member.name)
            total += member.size
            if total > MAX_EXTRACT_BYTES:
                raise _failed("bundle exceeds size bound; "
                              "the current install is unchanged")
            target = dest.joinpath(*parts)
            try:
                resolved = target.resolve()
            except OSError:
                raise _failed(f"unsafe archive member {member.name!r}; "
                              "the current install is unchanged") from None
            if resolved != dest.resolve() and dest.resolve() not in resolved.parents:
                raise _failed(f"unsafe archive member {member.name!r}; "
                              "the current install is unchanged")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, 0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise _failed(f"unsafe archive member {member.name!r}; "
                                  "the current install is unchanged")
                mode = 0o700 if (member.mode & 0o111) else 0o600
                fd = None
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, mode)
                    shutil.copyfileobj(extracted, os.fdopen(fd, "wb"))
                except OSError:
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    raise _failed(f"unsafe duplicate archive member {member.name!r}; "
                                  "the current install is unchanged") from None
    extracted_top = dest / top
    installer = extracted_top / "install.sh"
    try:
        stamp = os.lstat(installer)
    except OSError:
        raise _failed(f"ptest {version} bundle has no installer; "
                      "the current install is unchanged") from None
    if not stat.S_ISREG(stamp.st_mode) or not (stamp.st_mode & 0o111):
        raise _failed(f"ptest {version} bundle has no installer; "
                      "the current install is unchanged")
    return extracted_top


def _find_python() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise _failed("uv is required to install ptest; "
                      "the current install is unchanged")
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, uv from PATH
            [uv, "python", "find", "--system", ">=3.11,<3.15"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=60, check=False,
            text=True)
    except (OSError, subprocess.SubprocessError):
        raise _failed("no usable Python found for the ptest install; "
                      "the current install is unchanged") from None
    candidate = (completed.stdout or "").split("\n", 1)[0].strip()
    if (completed.returncode != 0 or not candidate
            or not os.path.isabs(candidate)):
        raise _failed("no usable Python found for the ptest install; "
                      "the current install is unchanged")
    try:
        stamp = os.lstat(candidate)
    except OSError:
        raise _failed("no usable Python found for the ptest install; "
                      "the current install is unchanged") from None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _failed("no usable Python found for the ptest install; "
                      "the current install is unchanged")
    return candidate


def _run_installer(bundle_dir: Path, root: Path, work: Path,
                   log: TextIO) -> None:
    script = bundle_dir / "install.sh"
    python = _find_python()
    env = {name: value for name, value in os.environ.items()
           if name != "PTEST_INSTALL_FAULT"}
    env["PTEST_PYTHON"] = python
    env["PTEST_NO_UPDATE_CHECK"] = "1"
    try:
        # Fixed argv: the verified bundle's install.sh plus --dest. The
        # bundle's SHA-256 was verified before extraction, and the script
        # path itself is never derived from network or user input.
        completed = subprocess.run(  # nosec B603 - verified bundle script
            [str(script), "--dest", str(root)],
            cwd=str(work), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=INSTALL_TIMEOUT_S, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        raise _failed("the ptest installer could not run; "
                      "the current install is unchanged") from None
    if completed.returncode != 0:
        text = (completed.stdout or b"").decode("utf-8", "replace")[-65536:]
        lines = text.splitlines()[-20:]
        for line in lines:
            log.write(line + "\n")
        log.flush()
        raise _failed("the ptest installer failed; "
                      "the current install is unchanged")


def _post_verify(root: Path, version: str) -> None:
    try:
        resolved = Path(os.path.realpath(root / "ptest"))
    except OSError:
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged") from None
    bundles = root / ".ptest-bundles"
    bundle_id = None
    try:
        relative = resolved.relative_to(bundles)
    except ValueError:
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged") from None
    if not relative.parts:
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged")
    bundle_id = relative.parts[0]
    if not uninstall._is_bundle_dir(bundles, bundle_id):
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged")
    try:
        with open(bundles / bundle_id / "complete.json", "rb") as stream:
            marker = json.loads(stream.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged") from None
    if not isinstance(marker, dict) or marker.get("ptest_version") != version:
        raise _failed(f"the installer did not switch the launcher to {version}; "
                      "the current install is unchanged")


def _release_file_urls(version: str, suffix: str) -> tuple[str, str]:
    base = f"{RELEASES_URL}/download/v{version}"
    name = f"ptest-{version}-{suffix}.tar.gz"
    return f"{base}/{name}", f"{base}/{name}.sha256"


def run_update(*, requested: str | None, check_only: bool,
               transport: Transport | None = None,
               layout: Layout | None = None,
               log: TextIO | None = None) -> UpdateResult:
    """Install ``requested`` (or the latest release) side by side."""
    active_log = sys.stderr if log is None else log
    active_layout = detect_layout() if layout is None else layout
    if active_layout is None:
        raise _problem("not-install-layout", SOURCE_HINT)
    if requested is not None and valid_version(requested) is None:
        raise _problem("invalid-config",
                       "--version must be a release number like 0.3.7")
    suffix = platform_suffix()
    active = transport if transport is not None else default_transport()
    running = C.PTEST_VERSION
    target = requested
    if target is None:
        target = resolve_latest(active, timeout_s=RESOLVE_TIMEOUT_S)
    if not isinstance(target, str):
        raise _unavailable()
    if (requested is None and not is_newer(target, running)
            or requested == running):
        return UpdateResult(running_version=running, target_version=target,
                            action="up-to-date", check_only=check_only,
                            install_root=active_layout.root)
    if check_only:
        return UpdateResult(running_version=running, target_version=target,
                            action="available", check_only=True,
                            install_root=active_layout.root)
    work = Path(tempfile.mkdtemp(prefix="ptest-update-"))
    try:
        stamp = os.lstat(work)
        if (not stat.S_ISDIR(stamp.st_mode)
                or stamp.st_uid != os.getuid()
                or stat.S_IMODE(stamp.st_mode) != 0o700):
            raise _failed("cannot stage the ptest download; "
                          "the current install is unchanged")
        archive_url, sha_url = _release_file_urls(target, suffix)
        archive = work / f"ptest-{target}-{suffix}.tar.gz"
        sha_file = work / f"ptest-{target}-{suffix}.tar.gz.sha256"
        active_log.write(f"ptest: downloading ptest {target} for {suffix}\n")
        active_log.flush()
        _download_to(active, archive_url, archive,
                     MAX_BUNDLE_BYTES, DOWNLOAD_TIMEOUT_S)
        _download_to(active, sha_url, sha_file,
                     MAX_SHA_BYTES, DOWNLOAD_TIMEOUT_S)
        try:
            with open(archive, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            raise _failed(f"checksum mismatch for ptest-{target}-{suffix}.tar.gz; "
                          "the current install is unchanged") from None
        try:
            with open(sha_file, "rb") as stream:
                expected = _parse_sha256(
                    stream.read(), f"ptest-{target}-{suffix}.tar.gz")
        except OSError:
            raise _failed(f"checksum mismatch for ptest-{target}-{suffix}.tar.gz; "
                          "the current install is unchanged") from None
        if not hmac.compare_digest(digest, expected):
            raise _failed(f"checksum mismatch for ptest-{target}-{suffix}.tar.gz; "
                          "the current install is unchanged")
        extracted = _safe_extract(archive, work, target)
        _run_installer(extracted, active_layout.root, work, active_log)
        _post_verify(active_layout.root, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return UpdateResult(running_version=running, target_version=target,
                        action="updated", check_only=False,
                        install_root=active_layout.root)


def render_text(result: UpdateResult) -> str:
    if result.action == "up-to-date":
        return f"ptest is up to date ({result.running_version})\n"
    if result.action == "available":
        return (f"ptest {result.target_version} is available "
                f"(installed {result.running_version}) \u2014 run: ptest update\n")
    return (f"ptest updated to {result.target_version} "
            f"(was {result.running_version})\n")


def document_data(result: UpdateResult) -> dict:
    return {"running_version": result.running_version,
            "target_version": result.target_version,
            "action": result.action,
            "check_only": result.check_only,
            "install_root": (None if result.install_root is None
                             else str(result.install_root))}


def _truthy(value: str | None) -> bool:
    return (value is not None
            and value.strip().lower() not in {"", "0", "false", "no", "off"})


def _read_cache(root: Path) -> dict | None:
    try:
        raw = files.read_regular(root, CACHE_NAME, CACHE_MAX_BYTES + 1)
    except C.Problem:
        return None
    if len(raw) > CACHE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    checked_at = data.get("checked_at")
    if (isinstance(checked_at, bool) or not isinstance(checked_at, (int, float))
            or checked_at != checked_at):
        return None
    latest = data.get("latest")
    if latest is not None and valid_version(latest) is None:
        return None
    declined = data.get("declined")
    if declined is not None and valid_version(declined) is None:
        return None
    declined_at = data.get("declined_at")
    if declined_at is not None and (
            isinstance(declined_at, bool)
            or not isinstance(declined_at, (int, float))):
        return None
    return {"schema": 1, "checked_at": float(checked_at),
            "latest": latest, "declined": declined,
            "declined_at": (None if declined_at is None
                            else float(declined_at))}


def _write_cache(root: Path, payload: dict) -> bool:
    try:
        files.publish_atomic(root, CACHE_NAME,
                             json.dumps(payload).encode("utf-8"))
    except (C.Problem, OSError, TypeError, ValueError):
        return False
    return True


def _fetch_latest_bounded(transport: Transport,
                          timeout_s: float) -> str | None:
    outcome: list = []

    def attempt() -> None:
        try:
            outcome.append(resolve_latest(transport, timeout_s=timeout_s))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive() or not outcome:
        return None
    if isinstance(outcome[0], str):
        return outcome[0]
    return None


def startup_check(argv: Sequence[str], *, quiet: bool, json_output: bool,
                  fixture: bool, transport: Transport | None = None,
                  now: float | None = None) -> None:
    """Best-effort pre-command notice. Never raises, never writes stdout."""
    try:
        _startup_check(argv, quiet=quiet, json_output=json_output,
                       fixture=fixture, transport=transport, now=now)
    except SystemExit:
        raise
    except Exception:
        return


def _startup_check(argv: Sequence[str], *, quiet: bool, json_output: bool,
                   fixture: bool, transport: Transport | None,
                   now: float | None) -> None:
    if fixture or quiet:
        return
    if _truthy(os.environ.get("PTEST_NO_UPDATE_CHECK")):
        return
    if _truthy(os.environ.get("CI")):
        return
    moment = time.time() if now is None else now
    try:
        domain = platform_api.domain_paths(None)
        root = domain.root
    except Exception:
        return
    cached = _read_cache(root)
    if cached is None:
        cached = {"schema": 1, "checked_at": 0.0, "latest": None,
                  "declined": None, "declined_at": None}
    checked_at = cached["checked_at"]
    stale = (checked_at <= 0.0
             or moment - checked_at >= CHECK_INTERVAL_S
             or checked_at > moment + 300)
    latest = cached["latest"]
    if stale:
        claim = {"schema": 1, "checked_at": moment, "latest": latest,
                 "declined": cached["declined"],
                 "declined_at": cached["declined_at"]}
        if not _write_cache(root, claim):
            return
        active = transport if transport is not None else default_transport()
        fetched = _fetch_latest_bounded(active, CHECK_TIMEOUT_S)
        if fetched is not None:
            latest = fetched
            _write_cache(root, {"schema": 1, "checked_at": moment,
                                "latest": latest,
                                "declined": cached["declined"],
                                "declined_at": cached["declined_at"]})
    if latest is None or not is_newer(latest, C.PTEST_VERSION):
        return
    layout = detect_layout()
    if layout is None:
        sys.stderr.write(
            f"ptest: update available: {latest} (installed {C.PTEST_VERSION}) "
            f"\u2014 {SOURCE_HINT}\n")
        sys.stderr.flush()
        return
    interactive = (sys.stdin.isatty() and sys.stderr.isatty()
                   and not json_output)
    if not interactive:
        sys.stderr.write(
            f"ptest: update available: {latest} (installed {C.PTEST_VERSION}) "
            f"\u2014 run: ptest update\n")
        sys.stderr.flush()
        return
    declined = cached["declined"]
    declined_at = cached["declined_at"]
    if (declined == latest and declined_at is not None
            and moment - declined_at < CHECK_INTERVAL_S):
        return
    sys.stderr.write(f"ptest {latest} is available "
                     f"(you have {C.PTEST_VERSION}). Update now? [Y/n] ")
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        sys.stderr.flush()
        raise SystemExit(130)
    if not answer or answer.strip().lower() not in ("", "y", "yes"):
        _write_cache(root, {"schema": 1, "checked_at": cached["checked_at"]
                            if not stale else moment,
                            "latest": latest, "declined": latest,
                            "declined_at": moment})
        return
    try:
        result = run_update(requested=latest, check_only=False,
                            transport=transport, layout=layout,
                            log=sys.stderr)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        sys.stderr.flush()
        raise SystemExit(130)
    except Exception as exc:
        message = exc.message if isinstance(exc, C.Problem) else str(exc) or "failed"
        sys.stderr.write(f"ptest: update failed: {message}; "
                         f"continuing with {C.PTEST_VERSION}\n")
        sys.stderr.flush()
        _write_cache(root, {"schema": 1, "checked_at": cached["checked_at"]
                            if not stale else moment,
                            "latest": latest, "declined": latest,
                            "declined_at": moment})
        return
    sys.stderr.write(f"ptest: updated to {result.target_version} "
                     f"(was {result.running_version})\n")
    sys.stdout.flush()
    sys.stderr.flush()
    launcher = str(layout.root / "ptest")
    try:
        _execv(launcher, [launcher, *argv])
    except OSError:
        sys.stderr.write(f"ptest: updated to {result.target_version}; "
                         "the update takes effect on the next run\n")
        sys.stderr.flush()
        return
